#!/usr/bin/env python3
"""Collect hashed synthesis evidence; keep routed timing and board status explicit."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def table(path, recursive=False):
    root = ET.parse(path).getroot().find('table')
    headers = [(d.text or '').strip() for d in root.find('column_headers')]
    rows = root.iter('row') if recursive else root.findall('row')
    return [dict(zip(headers, [(d.text or '').strip() for d in row.findall('data')])) for row in rows]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rejected-project', type=Path)
    args = parser.parse_args()
    project = args.project.resolve()
    manifest = json.loads((project/'manifest.json').read_text())
    guard = json.loads((project/'guard.json').read_text())
    if manifest['schema'] != 'npu128-pango-synthesis-v1' or guard['status'] != 'PASS':
        raise RuntimeError('missing successful guarded synthesis')
    for name, expected in manifest['files'].items():
        if digest(project/name) != expected:
            raise RuntimeError(f'staged input changed: {name}')
    for name in manifest['rtl']:
        if digest(ROOT/name) != manifest['files'][name]:
            raise RuntimeError(f'current RTL differs from synthesized bytes: {name}')
    syn = project/'prj_tasks/syn_1/synthesize'
    db = syn/'report_db'
    netlist = syn/'npu_measure_top_syn.vm'
    text = netlist.read_text()
    if '.Q (out_mask[15])' not in text:
        raise RuntimeError('channel-16 mask register missing; inspect synthesis trimming')
    usage = table(db/'synthesize_resource_utilization.db', recursive=True)
    instances = ['npu_measure_top', 'u_npu(npu_conv128)', 'u_conv(adam_conv128)',
                 'u_post(npu_postprocess)', 'u_mac(adam_mac128)',
                 'u_activation_ram(adam_sdp_ram)', 'u_weight_ram(adam_sdp_ram_1)']
    resources = {}
    for name in instances:
        row = next(r for r in usage if r['Module Inst Name'] == name)
        resources[name] = {key: float(row[key]) for key in ('LUT', 'FF', 'APM', 'DRM')}
    if resources['u_mac(adam_mac128)']['APM'] != 64:
        raise RuntimeError('unexpected MAC APM count')
    resources['scan_harness_difference'] = {
        key: resources['npu_measure_top'][key]-resources['u_npu(npu_conv128)'][key]
        for key in ('LUT', 'FF', 'APM', 'DRM')}
    paths = table(db/'synthesize_report_timing_path_setup.db')
    path_xml = ET.parse(db/'synthesize_report_timing_path_setup.db').getroot().find('table/row')
    path_cells = [(row.findall('data')[-1].text or '') for row in path_xml.findall(
        './general_container/table_container/table[@id="arrival"]/row')]
    files = ['manifest.json', 'guard.json', 'guard-samples.jsonl', 'synthesis.log',
             'prj_tasks/syn_1/synthesize/npu_measure_top_syn.vm',
             'prj_tasks/syn_1/synthesize/npu_measure_top.snr']
    for stem in ['resource_utilization', 'resource_usage', 'report_timing_fmax', 'report_timing_setup',
                 'report_timing_hold', 'report_timing_path_setup', 'check_timing_summary', 'settings']:
        files.append(f'prj_tasks/syn_1/synthesize/report_db/synthesize_{stem}.db')
    log = (project/'synthesis.log').read_text(errors='replace')
    version = re.search(r'version ([^)]+)', log).group(1)
    guard['command'] = 'pds_shell.exe -work_dir "$PROJECT" -project_name npu128_post -file "$PROJECT/synthesize.tcl"'
    setup = table(db/'synthesize_report_timing_setup.db')
    setup_passes = all(float(row['WNS(ns)']) >= 0 for row in setup) and bool(setup)
    frequency_mhz = 1000 / manifest['clock_period_ns']
    report = {'schema': 'npu128-synthesis-assessment-v1', 'date': guard['before']['timestamp'][:10],
              'source_branch': manifest['source_branch'], 'source_base_commit': manifest['source_base_commit'],
              'baseline_tag': manifest['baseline_tag'], 'source_sha256': manifest['files'],
              'project': manifest['project'], 'device': manifest['device'], 'tool_version': version,
              'parameters': manifest['parameters'], 'clock_period_ns': manifest['clock_period_ns'],
              'scope': manifest['scope'],
              'status': f'synthesis completed; estimated setup {"passes" if setup_passes else "fails"} at {frequency_mhz:g} MHz',
              'resources': resources, 'cell_usage': table(db/'synthesize_resource_usage.db'),
              'estimated_fmax': table(db/'synthesize_report_timing_fmax.db'),
              'estimated_setup': setup,
              'estimated_hold': table(db/'synthesize_report_timing_hold.db'),
              'check_timing': table(db/'synthesize_check_timing_summary.db'),
              'worst_setup_path': paths[0], 'worst_path_resources': [v for v in path_cells if v],
              'reported_setup_paths': len(paths),
              'all_reported_setup_paths_end_in_activation_ram': all('/u_activation_ram/' in r['End Point'] for r in paths),
              'memory_guard': guard, 'raw_evidence_sha256': {name: digest(project/name) for name in files},
              'route_run': False, 'routed_fmax_mhz': None, 'routed_wns_ns': None,
              'unrouted_nets': None, 'board_run': False,
              'note': 'Synthesis estimates do not establish placed/routed or CPU/DDR-integrated performance.'}
    if args.rejected_project:
        prior = json.loads((args.rejected_project/'manifest.json').read_text())
        report['initial_rejected_project'] = prior['project']
        report['initial_rejection'] = 'ADS sign extension of narrow int casts trimmed channel 16; explicitly zero-extended casts in current RTL'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(f'PASS collected synthesis evidence: {args.output}')


if __name__ == '__main__':
    main()
