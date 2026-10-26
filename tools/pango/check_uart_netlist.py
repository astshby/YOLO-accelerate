#!/usr/bin/env python3
"""Four-state mapped byte-boundary replay; UART PHY remains covered by RTL pins."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from stage import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--pds-home', type=Path, required=True)
    parser.add_argument('--simulator', type=Path, required=True)
    parser.add_argument('--validation', type=Path, required=True)
    parser.add_argument('--build-dir', type=Path, required=True)
    args = parser.parse_args()
    build = (ROOT/args.build_dir).resolve()
    if not build.is_relative_to(ROOT/'build') or build.exists():
        parser.error('use a fresh build/ directory')
    build.mkdir(parents=True, exist_ok=True)
    digest = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
    functional = json.loads(args.validation.read_text())
    assert functional['status'] == 'PASS' and functional['binary_sha256'] == digest(args.simulator)
    for name, expected in functional['source_sha256'].items(): assert digest(ROOT/name) == expected, name
    manifest = json.loads((args.project/'manifest.json').read_text())
    pll = manifest['schema'] == 'npu128-pango-uart100-implementation-v1'
    top = 'npu_uart_board100' if pll else 'npu_uart_board'
    assert manifest['top'] == top
    assert functional['default_parameters'] == manifest['parameters']
    netlist = args.project/f'prj_tasks/syn_1/synthesize/{top}_syn.vm'
    assert digest(args.validation) == digest(args.project/'input/rtl-validation.json')
    bench = ROOT/('tb/tb_uart100_byte_replay.sv' if pll else 'tb/tb_uart_byte_replay.sv')
    vendor = args.pds_home/'arch/vendor/pango/verilog/simulation'
    ivl = ROOT/'build/tools/iverilog/root/usr/lib/x86_64-linux-gnu/ivl'
    trace = build/'frames.hex'
    metrics = []

    def guarded(name, command):
        subprocess.run([sys.executable, str(ROOT/'tools/run_guarded.py'), '--rss-mib', '2048',
            '--as-mib', '3072', '--min-available-mib', '6144', '--seconds', '1200',
            '--log', str(build/f'{name}.log'), '--metrics', str(build/f'{name}.metrics.json'), '--', *map(str, command)],
            cwd=ROOT, check=True)
        metrics.append(json.loads((build/f'{name}.metrics.json').read_text()))

    guarded('trace', [sys.executable, ROOT/'tools/pango/uart_gate_trace.py', '--simulator', args.simulator,
        '--output', trace, '--clock-hz', functional['default_parameters']['CLOCK_HZ'],
        *(['--bulk-weights'] if functional.get('bulk_weights') else [])])
    extra = []
    if pll:
        # Digital byte replay deliberately replaces only analog clock generation.
        # Generate a zero-behavior declaration from this exact netlist's interface;
        # do not load/patch the vendor GPLL model (separate timed PLL proof).
        clean = re.sub(r'/\*.*?\*/|//[^\n]*', '', netlist.read_text(), flags=re.S)
        matches = list(re.finditer(r'\bGTP_GPLL\s*#\s*\((.*?)\)\s*\S+\s*\((.*?)\)\s*;', clean, re.S))
        assert len(matches) == 1
        params = re.findall(r'\.(\w+)\s*\(', matches[0][1])
        ports = re.findall(r'\.(\w+)\s*\(', matches[0][2])
        outputs = {p for p in ports if p.startswith('CLKOUT') and not p.endswith('_SYN')} | {'LOCK', 'APB_RDATA', 'APB_READY', 'DPS_DONE'}
        widths = {'APB_ADDR': '[4:0] ', 'APB_RDATA': '[15:0] ', 'APB_WDATA': '[15:0] '}
        stub = build/'byte_replay_gpll_stub.sv'
        stub.write_text('// Test-only declaration: CLKOUT0/LOCK are forced at the board boundary.\n'
            +'module GTP_GPLL #(\n'+',\n'.join('parameter '+p+'=0' for p in params)+'\n)(\n'
            +',\n'.join(('output wire ' if p in outputs else 'input wire ')+widths.get(p, '')+p for p in ports)
            +'\n);\nendmodule\n')
        extra = [stub]
    guarded('build', [ROOT/'build/tools/iverilog/root/usr/bin/iverilog', '-B', ivl, '-g2012', '-y', vendor,
        '-s', 'tb_uart_byte_replay', '-o', build/'mapped.vvp', netlist, bench, *extra])
    guarded('run', [ROOT/'build/tools/iverilog/root/usr/bin/vvp', '-M', ivl, build/'mapped.vvp', f'+TRACE={trace}',
        *(['+VARIABLE'] if functional.get('bulk_weights') else [])])
    result = (build/'run.log').read_text().strip()
    assert 'PASS mapped UART byte replay:' in result and not any(x in result for x in ('FAIL', 'ERROR', 'FATAL'))
    models = {}
    with (build/'mapped.vvp').open() as source:
        for line in source:
            m = re.match(r'\s*"([^"]+\.v)";', line)
            if m:
                p = Path(m[1]).resolve()
                if p.is_relative_to(vendor.resolve()) and p.is_file(): models[str(p.relative_to(vendor.resolve()))] = digest(p)
    report = {'schema': 'npu128-uart-mapped-byte-v1', 'status': 'PASS', 'netlist_sha256': digest(netlist),
        'scope': 'GTP four-state byte bridge/MMIO/NPU replay; UART PHY serialization bypassed, no SDF; GPLL clock/lock bypassed' if pll else 'GTP four-state byte bridge/MMIO/NPU replay; UART PHY serialization bypassed, no SDF',
        'result': result, 'metrics': metrics, 'trace_sha256': digest(trace),
        'test_sha256': digest(bench), 'gtp_model_sha256': models,
        'clock_hz': functional['default_parameters']['CLOCK_HZ'],
        'gpll_stub_sha256': digest(extra[0]) if pll else None,
        'trace_driver_sha256': digest(ROOT/'tools/pango/uart_gate_trace.py'),
        'rtl_reference_sha256': digest(args.validation), 'board_run': False,
        'forced_boundary': ['RX valid/data/error', 'PHY transmitter busy=0']+(['100 MHz clock and PLL LOCK'] if pll else []),
        'not_forced': 'bridge, MMIO, NPU, SRAM/APM, command/state/data outputs'}
    (build/'summary.json').write_text(json.dumps(report, indent=2)+'\n')
    print(result)


if __name__ == '__main__':
    main()
