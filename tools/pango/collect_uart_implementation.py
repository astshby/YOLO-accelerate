#!/usr/bin/env python3
"""Collect real-pin board STA separately from RTL and mapped-functional proof."""
import argparse
import json
from pathlib import Path
import re
from collect_report import ROOT, digest, table
from collect_implementation import command_text, final_unrouted_count, activation_memory_mapping


def flattened_cells(text, top):
    """Resolve scalar hierarchy ports for IO/reset auditing; never infer aliases by name."""
    clean = re.sub(r'/\*.*?\*/|//[^\n]*', '', text, flags=re.S)
    modules = {m[1]: m[2] for m in re.finditer(r'\bmodule\s+(\w+)\s*\(.*?\)\s*;(.*?)\bendmodule', clean, re.S)}
    types = '|'.join(re.escape(n) for n in modules)
    pattern = rf'\b(GTP_[A-Z0-9_]+|{types})\s*(?:#\s*\((.*?)\)\s*)?(\S+)\s*\((.*?)\)\s*;'
    parsed = {}
    for name, body in modules.items():
        parsed[name] = [{'type': m[1], 'name': m[3].lstrip('\\'),
            'ports': {p.lstrip('\\'): re.sub(r'\s+', '', n).lstrip('\\') for p, n in
                re.findall(r'\.(\\\S+|\w+)\s*\(\s*([^()]*)\s*\)', m[4])}}
            for m in re.finditer(pattern, body, re.S)]
    result = []

    def descend(module, prefix, bindings):
        def resolve(net):
            if not net or re.fullmatch(r"\d+'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ_]+", net):
                return net
            if net in bindings:
                return bindings[net]
            return prefix+net
        for cell in parsed[module]:
            ports = {p: resolve(n) for p, n in cell['ports'].items()}
            name = prefix+cell['name']
            if cell['type'].startswith('GTP_'):
                result.append({'type': cell['type'], 'name': name, 'ports': ports})
            else:
                descend(cell['type'], name+'/', ports)
    descend(top, '', {})
    return result


def async_boundary_audit(text, pll=False):
    text = re.sub(r'/\*.*?\*/|//[^\n]*', '', text, flags=re.S)
    cells = []
    pattern = r'\b(GTP_[A-Z0-9_]+)\s*(?:#\s*\((.*?)\)\s*)?(\S+)\s*\((.*?)\)\s*;'
    for match in re.finditer(pattern, text, re.S):
        ports = {p: re.sub(r'\s+', '', n).lstrip('\\') for p, n in
                 re.findall(r'\.(\w+)\s*\(\s*([^()]*)\s*\)', match[4])}
        cells.append({'type': match[1], 'name': match[3].lstrip('\\'), 'ports': ports})
    if pll:
        cells = flattened_cells(text, 'npu_uart_board100')

    def loads(net, driver):
        return [(cell, pin) for cell in cells for pin, wire in cell['ports'].items()
                if wire == net and not (cell is driver and pin in ('Q', 'O', 'Z', 'LOCK', 'CLKOUT0'))]

    def input_buffer(port):
        found = [c for c in cells if c['type'] == 'GTP_INBUF' and c['ports'].get('I') == port]
        if len(found) != 1:
            raise RuntimeError(f'expected one input buffer for {port}')
        return found[0]

    rx_buffer = input_buffer('uart_rxd')
    rx_loads = loads(rx_buffer['ports']['O'], rx_buffer)
    if len(rx_loads) != 1:
        raise RuntimeError('raw UART RX has fanout outside its first synchronizer FF')
    first, pin = rx_loads[0]
    if not first['type'].startswith('GTP_DFF') or pin != 'D' or 'rx_meta_q' not in first['name']:
        raise RuntimeError('raw RX does not terminate at the expected first synchronizer D pin')
    second_loads = loads(first['ports']['Q'], first)
    if len(second_loads) != 1:
        raise RuntimeError('first RX synchronizer FF has additional consumers')
    second, pin = second_loads[0]
    if not second['type'].startswith('GTP_DFF') or pin != 'D' or 'rx_sync_q' not in second['name']:
        raise RuntimeError('second RX synchronizer FF missing')
    reset_buffer = input_buffer('board_reset_n')
    plls = [c for c in cells if c['type'] == 'GTP_GPLL']
    if pll and len(plls) != 1:
        raise RuntimeError('expected exactly one real GPLL')
    boundary_nets = {reset_buffer['ports']['O']}
    if pll:
        boundary_nets.add(plls[0]['ports']['LOCK'])
        # Only a bounded inverter/NAND reset cone may occur before async pins.
        # No core state, address, command or data signal may enter this cone.
        for _ in range(4):
            for cell in cells:
                inputs = [v for k, v in cell['ports'].items() if k in ('I', 'I0', 'I1')]
                if (cell['type'] in ('GTP_INV', 'GTP_LUT1', 'GTP_LUT2') and inputs
                        and all(v in boundary_nets or v in ("1'b0", "1'b1") for v in inputs)):
                    boundary_nets.add(cell['ports'].get('O', cell['ports'].get('Z')))
    frontier = [(reset_buffer['ports']['O'], reset_buffer)]
    seen, endpoints, pll_reset_endpoints = set(), [], []
    while frontier:
        net, driver = frontier.pop()
        if net in seen:
            raise RuntimeError('loop in asynchronous reset boundary')
        seen.add(net)
        consumers = loads(net, driver)
        if not consumers:
            raise RuntimeError('unconnected reset boundary')
        for cell, pin in consumers:
            if cell['type'] == 'GTP_DFF_P' and 'reset_release_q' in cell['name'] and pin == 'P':
                endpoints.append({'instance': cell['name'], 'pin': pin, 'primitive': cell['type']})
            elif pll and cell is plls[0] and pin == 'RST':
                pll_reset_endpoints.append({'instance': cell['name'], 'pin': pin, 'primitive': cell['type']})
            elif (cell['type'] in (('GTP_INV', 'GTP_LUT1', 'GTP_LUT2') if pll else ('GTP_INV', 'GTP_LUT1'))
                  and pin in ('I', 'I0', 'I1') and len(seen) < 5):
                output = cell['ports'].get('O', cell['ports'].get('Z'))
                if output is None or (pll and output not in boundary_nets):
                    raise RuntimeError('reset inverter output pin missing')
                frontier.append((output, cell))
            else:
                raise RuntimeError(f'raw KEY0 escapes reset-release asynchronous pins: {cell["name"]}/{pin}')
    if len(endpoints) != 3 or len({x['instance'] for x in endpoints}) != 3:
        raise RuntimeError('expected exactly three reset-release FF asynchronous endpoints')
    lock_endpoints = []
    if pll:
        if len(pll_reset_endpoints) != 1:
            raise RuntimeError('KEY0 must reach exactly the GPLL RST pin')
        frontier = [(plls[0]['ports']['LOCK'], plls[0])]
        seen = set()
        while frontier:
            net, driver = frontier.pop()
            if net in seen:
                raise RuntimeError('loop in PLL LOCK reset cone')
            seen.add(net)
            consumers = loads(net, driver)
            if not consumers:
                raise RuntimeError('unconnected PLL LOCK boundary')
            for cell, pin in consumers:
                if cell['type'] == 'GTP_DFF_P' and 'reset_release_q' in cell['name'] and pin == 'P':
                    lock_endpoints.append({'instance': cell['name'], 'pin': pin, 'primitive': cell['type']})
                elif cell['type'] in ('GTP_INV', 'GTP_LUT1', 'GTP_LUT2') and pin in ('I', 'I0', 'I1') and len(seen) < 5:
                    output = cell['ports'].get('O', cell['ports'].get('Z'))
                    if output not in boundary_nets:
                        raise RuntimeError('PLL LOCK cone mixes with core state or data')
                    frontier.append((output, cell))
                else:
                    raise RuntimeError(f'PLL LOCK escapes reset-release async pins: {cell["name"]}/{pin}')
        if sorted(x['instance'] for x in lock_endpoints) != sorted(x['instance'] for x in endpoints):
            raise RuntimeError('PLL LOCK and KEY0 must gate the same three release FFs')
    return {'status': 'PASS', 'scope': 'mapped synthesis IO connectivity; all downstream register paths remain timed',
            'uart_first_ff': first['name'], 'uart_second_ff': second['name'], 'reset_endpoints': endpoints,
            'pll_reset_endpoints': pll_reset_endpoints, 'pll_lock_async_endpoints': lock_endpoints}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mapped-validation', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('preserve existing evidence; use a fresh output path')
    p = args.project.resolve()
    manifest = json.loads((p/'manifest.json').read_text())
    guard = json.loads((p/'guard.json').read_text())
    pll = manifest['schema'] == 'npu128-pango-uart100-implementation-v1'
    assert manifest['schema'] in ('npu128-pango-uart-implementation-v1', 'npu128-pango-uart100-implementation-v1') and guard['status'] == 'PASS'
    top = 'npu_uart_board100' if pll else 'npu_uart_board'
    assert manifest['top'] == top
    profile = 'uart-board100' if pll else 'uart-board'
    assert digest(p/'manifest.json') == guard['manifest_sha256']
    for name, expected in manifest['files'].items():
        assert digest(p/name) == expected, name
    for name in manifest['rtl']:
        assert digest(ROOT/name) == manifest['files'][name], name
    functional = json.loads((p/'input/rtl-validation.json').read_text())
    assert functional['schema'] == 'npu128-uart-functional-v1' and functional['status'] == 'PASS'
    for name, expected in functional['source_sha256'].items():
        assert digest(ROOT/name) == expected, name
    meta = json.loads((p/f'config/{profile}.json').read_text())
    assert manifest['clock_hz'] == meta['clock_hz'] == (100000000 if pll else 27000000)
    if pll:
        clock = json.loads((p/'input/clock-validation.json').read_text())
        assert clock['status'] == 'PASS' and clock['schema'] == 'npu128-clock100-functional-v1'
        for name, expected in clock['source_sha256'].items():
            assert digest(ROOT/name) == expected, name
    assert manifest['parameters'] == functional['default_parameters']
    assert digest(ROOT/f'constraints/{top}.fdc') == digest(p/'board.fdc')
    syn = p/'prj_tasks/syn_1/synthesize'
    netlist = syn/f'{top}_syn.vm'
    audit = async_boundary_audit(netlist.read_text(), pll=pll)
    mapped = json.loads(args.mapped_validation.read_text())
    assert mapped['schema'] == 'npu128-uart-mapped-byte-v1' and mapped['status'] == 'PASS'
    assert mapped['netlist_sha256'] == digest(netlist)
    assert mapped['rtl_reference_sha256'] == digest(p/'input/rtl-validation.json')
    assert mapped['test_sha256'] == digest(ROOT/('tb/tb_uart100_byte_replay.sv' if pll else 'tb/tb_uart_byte_replay.sv'))
    assert mapped['trace_driver_sha256'] == digest(ROOT/'tools/pango/uart_gate_trace.py')
    pnr = p/'prj_tasks/pnr_1'
    db = pnr/'report_timing/report_db'
    imported = table(pnr/'device_map/report_db/device_map_timing_constraint.db')
    expected = [command_text(line) for line in (p/'board.fdc').read_text().splitlines()
                if line.startswith(('create_clock ', 'create_generated_clock ', 'set_'))]
    assert expected == [command_text(row['Command']) for row in imported]
    exceptions = [line for line in expected if line.startswith('set_false_path')]
    assert exceptions == ['set_false_path -from [get_ports uart_rxd]', 'set_false_path -from [get_ports board_reset_n]']
    pr = pnr/'place_route'
    io_path = pr/'report_db/route_opt_io_report.db'
    io = table(io_path)
    assert len(io) == 4 and {r['PORT'] for r in io} == set(meta['pins'])
    for row in io:
        assert row['LOC'] == meta['pins'][row['PORT']] and row['CONSTRAINT'] == 'YES', row
        assert float(row['VCCIO']) == meta['vccio'] and row['IOSTANDARD'] == meta['io_standard'], row
        assert row['DIRECTION'] == ('output' if row['PORT'] == 'uart_txd' else 'input')
    pinout = [row for row in table(pr/'report_db/route_opt_pinout_report.db') if row['Design Instance Name']]
    assert {r['Design Instance Name']: r['Pin Name'] for r in pinout} == meta['pins']
    setup, hold, mpw = [[dict(row, corner=corner) for corner in ('fast', 'slow')
        for row in table(db/f'report_timing_{corner}_{kind}.db')] for kind in ('setup', 'hold', 'mpw')]
    assert len(setup) == len(hold) == 2
    assert len(mpw) >= 2 and {r['corner'] for r in mpw} == {'fast', 'slow'}
    clock_summary = table(db/'report_timing_clock_summary.db', recursive=True)
    # The source has no FF loads; the actual register paths use the generated domain.
    if pll:
        clocks = {r['Clock Name']: r for r in clock_summary}
        assert set(clocks) == {'clk_27m', 'clk_100m'}
        assert abs(float(clocks['clk_27m']['Period'])-37.037037) < 0.001
        assert clocks['clk_100m']['Type'] == 'Generated' and float(clocks['clk_100m']['Period']) == 10
        assert clocks['clk_100m']['Source'] == 'clk_27m'
        assert command_text(clocks['clk_100m']['Targets']) == 'u_clock/u_gpll/gpll_inst/CLKOUT0'
        assert int(clocks['clk_100m']['Clock Load']) > 9000 and int(clocks['clk_27m']['Clock Load']) == 0
        assert all(r['Launch Clock'] == r['Capture Clock'] == 'clk_100m' for r in setup+hold)
    setup_paths, hold_paths = [sorted([dict(row, corner=corner) for corner in ('fast', 'slow')
        for row in table(db/f'report_timing_{corner}_path_{kind}.db')], key=lambda r: float(r['Slack']))
        for kind in ('setup', 'hold')]
    checks = table(db/'check_timing_summary.db')
    assert {'no_clock', 'no_input_delay', 'no_output_delay'} <= {r['Check'] for r in checks}
    coverage = all(int(r['Number of Issues']) == 0 for r in checks if r['Check'] != 'virtual_clock')
    wns = min(float(r['WNS(ns)']) for r in setup)
    whs = min(float(r['WHS(ns)']) for r in hold)
    pulse = all(float(r['WPWS']) >= 0 and int(r['Failing End Point']) == 0 for r in mpw)
    logs = [f.read_text(errors='replace') for f in pr.glob('run_*.log')]
    text = (pr/'run_route.log').read_text()+'\n'+(pr/'run_route_optimize.log').read_text()
    unrouted, unrouted_evidence = final_unrouted_count(text)
    no_clock_warning = not any('Route-2036' in s for s in logs)
    usage = table(pr/'report_db/route_opt_resource_utilization.db', recursive=True)
    resources = {r['Module Inst Name']: {k: float(r[k]) for k in ('LUT', 'FF', 'APM', 'DRM')}
        for r in usage if r['Module Inst Name'] in (top, 'u_system(npu_uart_board)', 'u_device(npu_mmio128)',
            'u_npu(npu_conv128)', 'u_conv(adam_conv128)', 'u_post(npu_postprocess)',
            'u_uart(uart_8n1)', 'u_bridge(uart_mmio_bridge)')}
    assert resources['u_npu(npu_conv128)']['DRM'] == 126.5
    assert resources['u_npu(npu_conv128)']['APM'] == 82
    assert resources[top]['DRM'] <= 160
    mapping_path = syn/'apm_drm_hrm_lutram_utilization.log'
    mapping = activation_memory_mapping(mapping_path)
    assert len(mapping) == 6 and all(r['count'] == 16 and r['write_configuration'] == r['read_configuration'] == '8 K x 4' for r in mapping)
    closed = wns >= 0 and whs >= 0 and unrouted == 0 and coverage and pulse and no_clock_warning
    closed &= all(int(r['Failing Endpoints']) == 0 for r in setup+hold)
    raw = [p/n for n in ('manifest.json', 'guard.json', 'guard-samples.jsonl', 'implementation.log', manifest['project_name']+'.pds')]
    raw += [netlist, mapping_path, *pnr.rglob('*.log'), *pnr.rglob('report_db/*.db'), *pnr.rglob('*.adf')]
    report = {'schema': 'npu128-uart100-routed-assessment-v1' if pll else 'npu128-uart-routed-assessment-v1', 'status': 'PASS' if closed else 'TIMING_OR_COVERAGE_FAIL',
        'scope': manifest['scope'], 'source_branch': manifest['source_branch'], 'source_base_commit': manifest['source_base_commit'],
        'project': manifest['project'], 'device': manifest['device'], 'top': top,
        'clock_hz': manifest['clock_hz'], 'clock_period_ns': manifest['clock_period_ns'],
        'clock_validation': clock if pll else None,
        'source_sha256': manifest['files'], 'functional_validation': functional,
        'constraints': {'sha256': digest(p/'board.fdc'), 'imported_commands': imported, 'async_boundary_audit': audit},
        'io': io, 'pinout': pinout, 'routed_setup': setup, 'routed_hold': hold, 'minimum_pulse_width': mpw,
        'worst_setup_paths': setup_paths[:10], 'worst_hold_paths': hold_paths[:10],
        'routed_wns_ns': wns, 'routed_whs_ns': whs, 'minimum_pulse_width_passes': pulse,
        'unrouted_nets': unrouted, 'unrouted_count_evidence': unrouted_evidence,
        'routed_fmax': table(db/'report_timing_fmax.db'), 'clock_summary': clock_summary,
        'check_timing': checks, 'coverage_checks_pass': coverage,
        'clock_routed_over_srb_warning': not no_clock_warning, 'resources': resources,
        'activation_memory_mapping': mapping, 'mapped_netlist_sha256': digest(netlist),
        'memory_guard': guard, 'route_run': True, 'board_run': False, 'bitstream_requested': False,
        'mapped_functional_run': True, 'mapped_byte_validation': mapped,
        'mapped_validation_sha256': digest(args.mapped_validation),
        'collector_sha256': digest(Path(__file__)),
        'raw_evidence_sha256': {str(f.relative_to(p)): digest(f) for f in sorted(set(raw))},
        'limitations': ['Real PLL 100 MHz board implementation.' if pll else 'Native 27 MHz diagnostic, not the core-only 100 MHz constraint or a PLL implementation.',
            'Public-pin RTL simulation covers UART/MMIO/convolution. Mapped byte replay bypasses UART serialization; no SDF simulation.',
            'No bitstream or physical board/USB-UART run. Pin assignments are from the verified manual, not physical board inspection.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(f'Collected board physical evidence: {report["status"]}, setup={wns:g} ns, hold={whs:g} ns, unrouted={unrouted}')


if __name__ == '__main__':
    main()
