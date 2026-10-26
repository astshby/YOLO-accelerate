#!/usr/bin/env python3
"""Stage a fresh PDS physical flow for RTL with a verified synthesis baseline."""
import argparse
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess

from stage import ROOT, sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--synthesis-project', type=Path, required=True)
    parser.add_argument('--work-root', type=Path, default=os.environ.get('PDS_WORK_ROOT'))
    parser.add_argument('--netlist-validation', type=Path,
                        default=ROOT/'reports/npu128-timing-netlist-validation.json',
                        help='mapped-functional PASS JSON for this exact synthesis netlist')
    parser.add_argument('--rtl-validation', type=Path,
                        help='current RTL regression summary; requires fresh gate verification before acceptance')
    parser.add_argument('--name', required=True)
    parser.add_argument('--routing-recovery', action='store_true',
                        help='enable documented recovery timing analysis during routing')
    parser.add_argument('--max-hold-paths', type=int,
                        help='hold repair path-count limit when routing recovery is enabled')
    parser.add_argument('--planned-placement', action='store_true',
                        help='plan module locations before normal placement')
    args = parser.parse_args()
    if args.max_hold_paths is not None and (not args.routing_recovery or args.max_hold_paths < 1):
        parser.error('--max-hold-paths requires --routing-recovery and a positive count')
    source = args.synthesis_project.resolve()
    prior = json.loads((source/'manifest.json').read_text())
    guard = json.loads((source/'guard.json').read_text())
    if prior['schema'] != 'npu128-pango-synthesis-v1' or guard['status'] != 'PASS':
        parser.error('a successful guarded NPU synthesis is required')
    for name, expected in prior['files'].items():
        if sha256(source/name) != expected:
            parser.error(f'synthesis input changed: {name}')
    regression = json.loads(args.rtl_validation.read_text()) if args.rtl_validation else None
    if regression and (not regression['results'] or
                       not all(row['result'].startswith('PASS ') for row in regression['results'])):
        parser.error('all RTL regression cases must pass')
    for name in prior['rtl']:
        expected = regression['source_sha256'].get(name) if regression else prior['files'][name]
        if sha256(ROOT/name) != expected:
            parser.error(f'current RTL differs from validated input: {name}')
    netlist = source/'prj_tasks/syn_1/synthesize/npu_measure_top_syn.vm'
    checked = json.loads(args.netlist_validation.read_text())
    if checked['status'] != 'PASS' or checked['netlist_sha256'] != sha256(netlist):
        parser.error('the source netlist must match the saved mapped-functional PASS')
    if not args.work_root or not args.work_root.is_dir():
        parser.error('pass an existing --work-root or set PDS_WORK_ROOT')
    if not args.name or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in args.name):
        parser.error('use a lower-case ASCII project name')
    target = args.work_root.resolve()/'work'/args.name
    native = str(target) if os.name == 'nt' else subprocess.check_output(
        ['wslpath', '-w', str(target)], text=True).strip()
    if not PureWindowsPath(native).drive or native.startswith('\\\\') or not native.isascii():
        parser.error('use a native Windows drive with an ASCII path')
    if any(c in native for c in '{}"\n\r') or target.exists():
        parser.error('use a safe fresh directory; previous evidence is never overwritten')
    if target == ROOT or ROOT in target.parents or target in ROOT.parents:
        parser.error('PDS output must be outside the source repository')
    target.mkdir(parents=True)
    files = {}

    def copy(src, name):
        destination = target/name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, destination)
        files[name] = sha256(destination)

    copy(netlist, 'input/reference_syn.vm')
    copy(source/'synthesize.tcl', 'input/baseline-synthesize.tcl')
    copy(source/'measurement.fdc', 'measurement.fdc')
    copy(source/'manifest.json', 'input/synthesis-manifest.json')
    copy(source/'guard.json', 'input/synthesis-guard.json')
    copy(args.netlist_validation, 'input/netlist-validation.json')
    if args.rtl_validation:
        copy(args.rtl_validation, 'input/rtl-validation.json')
    for name in [*prior['rtl'], 'tools/pango/stage_implementation.py',
                 'tools/pango/run_synthesis.ps1', 'tools/pango/GuardedJob.cs']:
        copy(ROOT/name, name)
    # ADS associates FDC with its synthesis flow. A bare VM backend rejects FDC
    # (Flow-0150), so retain the exact successful ADS commands and constraints.
    route = 'route -number_of_router_thread 1 -check_clk_net_route_by_srb TRUE'
    hold = 'route_optimize -fix_hold_violation_in_route TRUE'
    place = 'place'
    if args.planned_placement:
        # Installed 2025.2 reports an enum here, unlike the bundled PDF's
        # Boolean spelling. Keep the tool-confirmed value in the staged Tcl.
        place += ' -mode normal -planning_before_place Enable'
    if args.routing_recovery:
        route += ' -optimize_routing_recovery TRUE'
    if args.max_hold_paths is not None:
        hold += f' -max_hold_violated_paths_num {args.max_hold_paths}'
    lines = ['# Same checked RTL and ADS/FDC setup, extended through routing; no bitstream.',
             *(source/'synthesize.tcl').read_text().splitlines(),
        'dev_map',
        place,
        route,
        hold,
        'report_timing -max_path 100 -nworst 3 -report_exception TRUE -report_logic_level_distribution',
    ]
    (target/'implement.tcl').write_text('\n'.join(lines)+'\n')
    files['implement.tcl'] = sha256(target/'implement.tcl')
    manifest = {
        'schema': 'npu128-pango-implementation-v1', 'top': prior['top'],
        'device': prior['device'], 'clock_period_ns': prior['clock_period_ns'],
        'threads': 1, 'parameters': {**prior['parameters'], 'activation_banks': 2,
                                    'activation_words_per_bank': 24576, 'measurement_input_bits': 1716,
                                    'activation_segments_per_bank': 3, 'activation_segment_words': 8192},
        'source_base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'source_branch': subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip(),
        'source_status': subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).splitlines(),
        'source_synthesis': prior['project'], 'source_netlist_sha256': sha256(netlist),
        'project': f'$PDS_WORK_ROOT/work/{args.name}', 'rtl': prior['rtl'], 'files': files,
        'scope': 'standalone scan harness with automatic diagnostic IO placement; no board pinout, CPU, DDR or PLL',
        'flow': 'same ADS synthesis commands followed by physical implementation',
        'deferred_gate_validation': bool(args.rtl_validation),
        'constraints_unchanged': True, 'hold_fix_enabled': True, 'bitstream_requested': False,
        'routing_recovery': args.routing_recovery, 'max_hold_paths': args.max_hold_paths,
        'planned_placement': args.planned_placement,
    }
    (target/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(f'PASS staged original-constraint physical implementation: {native}')


if __name__ == '__main__':
    main()
