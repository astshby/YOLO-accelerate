#!/usr/bin/env python3
"""Collect routed evidence, distinguishing tool completion from timing closure."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from collect_report import ROOT, digest, table


IGNORED_COMMENTS = ('// Generated', '// SDC constraint :')


def canonical_netlist(path):
    # Only generation metadata and the FDC source-location comments may differ.
    return '\n'.join(line for line in path.read_text().splitlines()
                     if not line.lstrip().startswith(IGNORED_COMMENTS))


def functional_report(path, netlist):
    report = json.loads(path.read_text())
    if report.get('status') != 'PASS' or report.get('netlist_sha256') != digest(netlist):
        raise RuntimeError('functional PASS must match the exact implemented netlist')
    return report


def activation_memory_mapping(path):
    configurations = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            columns = [part.strip() for part in line.split('|')]
            if len(columns) > 7 and 'g_activation_bank[' in columns[1] and '/mem_' in columns[1]:
                key = (columns[1].rsplit('/mem_', 1)[0], columns[2], columns[3], columns[7])
                configurations[key] = configurations.get(key, 0) + 1
    return [{'instance': key[0], 'write_configuration': key[1], 'read_configuration': key[2],
             'primitive': key[3], 'count': count} for key, count in sorted(configurations.items())]


def command_text(text):
    return ' '.join(text.replace('{', '').replace('}', '').split())


def select_db(directory, *tokens, exclude=()):
    found = [p for p in directory.glob('*.db')
             if all(t in p.stem for t in tokens) and not any(t in p.stem for t in exclude)]
    if len(found) != 1:
        raise RuntimeError(f'expected one {tokens} database in {directory}: {found}')
    return found[0]


def final_unrouted_count(text):
    patterns = [
        (r'(?im)\b(?:number of\s+)?un[- ]?routed\s+(?:nets?|networks?)\s*[:=|]\s*([\d,]+)', None),
        (r'(?im)^Unrouted nets at iteration [^\r\n]*:\s*([\d,]+)\s*$', None),
        (r'(?im)^(?:I:\s*)?All (?:nets|networks) (?:have been |are )?routed successfully[^\r\n]*$', 0),
    ]
    observations = []
    for pattern, fixed in patterns:
        for match in re.finditer(pattern, text):
            value = int(match.group(1).replace(',', '')) if fixed is None else fixed
            observations.append((match.start(), value, match.group(0).strip()))
    if not observations:
        raise RuntimeError('explicit final unrouted-net count not found; inspect native reports')
    _, count, evidence = max(observations)
    return count, evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rejected-project', type=Path, action='append', default=[])
    parser.add_argument('--netlist-validation', type=Path,
                        help='fresh mapped-functional PASS, required for changed-RTL physical flows')
    args = parser.parse_args()
    project = args.project.resolve()
    manifest = json.loads((project/'manifest.json').read_text())
    guard = json.loads((project/'guard.json').read_text())
    if manifest['schema'] != 'npu128-pango-implementation-v1' or guard['status'] != 'PASS':
        raise RuntimeError('successful guarded physical flow required')
    if digest(project/'manifest.json') != guard['manifest_sha256']:
        raise RuntimeError('manifest differs from the one verified at execution')
    for name, expected in manifest['files'].items():
        if digest(project/name) != expected:
            raise RuntimeError(f'staged input changed: {name}')
    for name in manifest['rtl']:
        if digest(ROOT/name) != manifest['files'][name]:
            raise RuntimeError(f'current RTL differs from physically implemented input: {name}')
    prior = json.loads((project/'input/synthesis-manifest.json').read_text())
    baseline = project/'input/reference_syn.vm'
    regenerated = project/'prj_tasks/syn_1/synthesize/npu_measure_top_syn.vm'
    reference_checked = functional_report(project/'input/netlist-validation.json', baseline)
    checked = reference_checked
    original_text, new_text = canonical_netlist(baseline), canonical_netlist(regenerated)
    deferred = manifest.get('deferred_gate_validation', False)
    if deferred:
        if not args.netlist_validation:
            parser.error('fresh --netlist-validation is required for this changed-RTL flow')
        checked = functional_report(args.netlist_validation, regenerated)
    elif original_text != new_text:
        raise RuntimeError('regenerated synthesis differs beyond permitted metadata comments')
    if digest(project/'measurement.fdc') != prior['files']['measurement.fdc']:
        raise RuntimeError('clock or IO constraints changed from the synthesis baseline')
    pnr = project/'prj_tasks/pnr_1'
    map_db = pnr/'device_map/report_db'
    imported = table(map_db/'device_map_timing_constraint.db')
    expected_commands = [command_text(line) for line in (project/'measurement.fdc').read_text().splitlines()
                         if line.strip() and not line.lstrip().startswith('#')]
    actual_commands = [command_text(row['Command']) for row in imported]
    if expected_commands != actual_commands:
        raise RuntimeError('actual imported timing constraints differ from the FDC')
    timing_dirs = [p for p in pnr.glob('*/report_db') if 'timing' in p.parent.name]
    if len(timing_dirs) != 1:
        raise RuntimeError(f'expected one final post-PNR timing directory: {timing_dirs}')
    db = timing_dirs[0]
    fmax_file = select_db(db, 'fmax')
    check_file = select_db(db, 'check', 'summary')
    def corners(kind):
        return [dict(row, corner=corner) for corner in ('fast', 'slow')
                for row in table(db/f'report_timing_{corner}_{kind}.db')]

    setup, hold, mpw = [corners(kind) for kind in ('setup', 'hold', 'mpw')]
    fmax, checks = [table(p) for p in (fmax_file, check_file)]
    if not setup or not hold:
        raise RuntimeError('both setup and hold summaries are required')
    setup_wns = min(float(row['WNS(ns)']) for row in setup)
    hold_whs = min(float(row['WHS(ns)']) for row in hold)
    check_values = {r['Check']: int(r['Number of Issues']) for r in checks}
    required_checks = {'no_clock', 'no_input_delay', 'no_output_delay'}
    if not required_checks <= check_values.keys():
        raise RuntimeError('mandatory constraint-coverage checks missing')
    coverage_ok = all(n == 0 for name, n in check_values.items() if name != 'virtual_clock')
    setup_paths = sorted(corners('path_setup'), key=lambda row: float(row['Slack']))
    hold_paths = sorted(corners('path_hold'), key=lambda row: float(row['Slack']))
    logs = {p.name: p.read_text(errors='replace') for p in (pnr/'place_route').glob('run_*.log')}
    route_text = logs.get('run_route.log', '') + '\n' + logs.get('run_route_optimize.log', '')
    # Never substitute successful tool exit for an explicit routing-completeness result.
    unrouted, unrouted_evidence = final_unrouted_count(route_text)
    resource_files = list((pnr/'place_route/report_db').glob('*resource_utilization.db'))
    resource_files.sort(key=lambda p: ('route_opt' in p.name, p.name.startswith('route_'), p.name))
    if not resource_files:
        raise RuntimeError('physical resource report missing')
    resource_file = resource_files[-1]
    resource_rows = table(resource_file, recursive=True)
    resources = {}
    for name in ('npu_measure_top', 'u_npu(npu_conv128)', 'u_conv(adam_conv128)', 'u_post(npu_postprocess)'):
        row = next((r for r in resource_rows if r['Module Inst Name'] == name), None)
        if row:
            resources[name] = {k: float(row[k]) for k in ('LUT', 'FF', 'APM', 'DRM')}
    setup_endpoints_ok = all(int(r['Failing Endpoints']) == 0 for r in setup)
    hold_endpoints_ok = all(int(r['Failing Endpoints']) == 0 for r in hold)
    mpw_ok = bool(mpw) and all(float(r['WPWS']) >= 0 and int(r['Failing End Point']) == 0 for r in mpw)
    clock_srb_warning = any('Route-2036' in s for s in logs.values())
    closed = (setup_wns >= 0 and hold_whs >= 0 and setup_endpoints_ok and hold_endpoints_ok
              and unrouted == 0 and coverage_ok and mpw_ok and not clock_srb_warning)
    log = (project/'implementation.log').read_text(errors='replace')
    files = [project/n for n in ('manifest.json', 'guard.json', 'guard-samples.jsonl',
                                 'implementation.log', 'npu128_impl.pds')]
    files += [regenerated, *pnr.rglob('*.adf'), *pnr.rglob('*.log'), *pnr.rglob('report_db/*.db')]
    memory_mapping = project/'prj_tasks/syn_1/synthesize/apm_drm_hrm_lutram_utilization.log'
    if memory_mapping.is_file():
        files.append(memory_mapping)
    internal = [r for r in setup_paths if 'u_npu/' in r['Start Point'] and 'u_npu/' in r['End Point']]
    report = {
        'schema': 'npu128-routed-assessment-v1', 'date': guard['before']['timestamp'][:10],
        'status': 'PASS' if closed else 'TIMING_OR_COVERAGE_FAIL',
        'scope': manifest['scope'], 'source_branch': manifest['source_branch'],
        'source_base_commit': manifest['source_base_commit'], 'project': manifest['project'],
        'device': manifest['device'], 'parameters': manifest['parameters'],
        'tool_version': re.search(r'version ([^)]+)', log).group(1),
        'clock_period_ns': manifest['clock_period_ns'], 'source_sha256': manifest['files'],
        'implementation_options': {key: manifest[key] for key in
            ('hold_fix_enabled', 'routing_recovery', 'max_hold_paths', 'planned_placement') if key in manifest},
        'netlist_comparison': {'status': 'PASS', 'ignored_comment_prefixes': IGNORED_COMMENTS,
            'method': 'fresh mapped-functional verification' if deferred else 'canonical comparison to checked baseline',
            'identical_to_reference': original_text == new_text,
            'baseline_sha256': digest(baseline), 'regenerated_sha256': digest(regenerated),
            'canonical_sha256': hashlib.sha256(new_text.encode()).hexdigest(),
            'reference_functional_result': reference_checked['test_result']},
        'functional_validation': checked,
        'constraints': {'byte_identical_to_baseline': True, 'sha256': digest(project/'measurement.fdc'),
                        'imported_commands': imported, 'clock_signals': table(map_db/'device_map_clock_signal.db')},
        'routed_fmax': fmax, 'routed_setup': setup, 'routed_hold': hold,
        'minimum_pulse_width': mpw, 'minimum_pulse_width_passes': mpw_ok,
        'routed_wns_ns': setup_wns, 'routed_whs_ns': hold_whs, 'unrouted_nets': unrouted,
        'unrouted_count_evidence': unrouted_evidence,
        'check_timing': checks, 'coverage_checks_pass': coverage_ok,
        'clock_routed_over_srb_warning': clock_srb_warning,
        'timing_report_directory': str(db.relative_to(project)),
        'resources': resources, 'resource_report': str(resource_file.relative_to(project)),
        'activation_ram_instances': [{k: r[k] for k in ('Module Inst Name', 'LUT', 'FF', 'APM', 'DRM')}
                                     for r in resource_rows if 'g_activation_bank[' in r['Module Inst Name']],
        'activation_memory_mapping': activation_memory_mapping(memory_mapping),
        'worst_setup_path': setup_paths[0] if setup_paths else None,
        'worst_hold_path': hold_paths[0] if hold_paths else None,
        'worst_internal_setup_among_reported': internal[0] if internal else None,
        'reported_setup_paths': len(setup_paths), 'reported_hold_paths': len(hold_paths),
        'memory_guard': guard, 'route_run': True, 'board_run': False, 'bitstream_requested': False,
        'raw_evidence_sha256': {str(p.relative_to(project)): digest(p) for p in sorted(set(files))},
        'limitations': ['Standalone automatically placed diagnostic IO; no board pin assignment.',
                        'No CPU/DDR/PLL integration, board test, or post-route SDF simulation.',
                        'Fmax is a tool timing estimate for this constrained harness, not a measured board frequency.'],
        'rejected_attempts': [],
    }
    for rejected in args.rejected_project:
        receipt = json.loads((rejected/'guard.json').read_text())
        old = json.loads((rejected/'manifest.json').read_text())
        old_log = (rejected/'implementation.log').read_text(errors='replace')
        report['rejected_attempts'].append({'project': old['project'], 'guard': receipt,
            'errors': re.findall(r'(?m)^E:.*$', old_log),
            'sha256': {name: digest(rejected/name) for name in ('manifest.json', 'guard.json', 'implementation.log')}})
    if args.output.exists():
        parser.error('use a new output file; existing evidence is never overwritten')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(f'Collected physical evidence: status={report["status"]}, setup={setup_wns:g} ns, hold={hold_whs:g} ns, unrouted={unrouted}')


if __name__ == '__main__':
    main()
