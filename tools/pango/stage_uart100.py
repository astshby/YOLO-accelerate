#!/usr/bin/env python3
"""Stage a real 27-to-100-MHz GPLL board after separate PLL and pin UART proofs."""
import argparse
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
from stage import ROOT, sha256
sys.path.insert(0, str(ROOT/'tools'))
from run_clock100_tests import CLOCK_RTL


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root', type=Path, required=True)
    parser.add_argument('--validation', type=Path, required=True)
    parser.add_argument('--clock-validation', type=Path, required=True)
    parser.add_argument('--name', required=True)
    args = parser.parse_args()
    functional = json.loads(args.validation.read_text())
    clock = json.loads(args.clock_validation.read_text())
    assert functional['status'] == clock['status'] == 'PASS'
    assert functional['schema'] == 'npu128-uart-functional-v1'
    assert clock['schema'] == 'npu128-clock100-functional-v1'
    for evidence in (functional, clock):
        for name, expected in evidence['source_sha256'].items():
            assert sha256(ROOT/name) == expected, f'stale functional evidence: {name}'
    assert set(CLOCK_RTL) <= set(clock['source_sha256'])
    meta = json.loads((ROOT/'config/uart-board100.json').read_text())
    parameters = {'CLOCK_HZ': meta['clock_hz'], 'BAUD': meta['requested_baud'],
                  'INTERBYTE_TIMEOUT': meta['interbyte_timeout_clocks']}
    assert functional['default_parameters'] == parameters
    assert clock['reference_hz'] == meta['reference_clock_hz'] == 27000000
    assert clock['output_hz'] == meta['clock_hz'] == 100000000
    if not args.name or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in args.name):
        parser.error('use a lower-case ASCII name')
    if not args.work_root.is_dir():
        parser.error('work root must exist')
    target = args.work_root.resolve()/'work'/args.name
    native = str(target) if os.name == 'nt' else subprocess.check_output(['wslpath', '-w', str(target)], text=True).strip()
    if not PureWindowsPath(native).drive or native.startswith('\\\\') or not native.isascii() or any(c in native for c in '{}"\n\r'):
        parser.error('use a safe native Windows directory')
    if target.exists() or target == ROOT or ROOT in target.parents or target in ROOT.parents:
        parser.error('use a fresh directory outside the repository')
    target.mkdir(parents=True)
    files = {}

    def copy(source, name):
        out = target/name
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, out)
        files[name] = sha256(out)

    for name in CLOCK_RTL+['config/uart-board100.json', 'tools/pango/stage_uart100.py',
                          'tools/pango/run_synthesis.ps1', 'tools/pango/GuardedJob.cs']:
        copy(ROOT/name, name)
    copy(args.validation, 'input/rtl-validation.json')
    copy(args.clock_validation, 'input/clock-validation.json')
    copy(ROOT/'constraints/npu_uart_board100.fdc', 'board.fdc')
    tcl = ['# Real PLL 100 MHz board. No bitstream or download.',
        'set_arch -family Logos2 -device PG2L200H -package FBB676 -speedgrade -6',
        'set_option max_threads 1', *(f'add_design {{{name}}}' for name in CLOCK_RTL),
        'add_constraint {board.fdc}',
        'compile -top_module npu_uart_board100 -verilog_standard SystemVerilog -loop_limit 65536',
        'synthesize -ads -selected_syn_tool_opt 2 -max_path 100 -nworst 3 -report_exception TRUE -report_logic_level_distribution',
        'dev_map', 'place -mode normal -planning_before_place Enable',
        'route -number_of_router_thread 1 -check_clk_net_route_by_srb TRUE -optimize_routing_recovery TRUE',
        'route_optimize -fix_hold_violation_in_route TRUE -max_hold_violated_paths_num 100000',
        'report_timing -max_path 100 -nworst 3 -report_exception TRUE -report_logic_level_distribution']
    (target/'implement.tcl').write_text('\n'.join(tcl)+'\n')
    files['implement.tcl'] = sha256(target/'implement.tcl')
    manifest = {'schema': 'npu128-pango-uart100-implementation-v1', 'top': 'npu_uart_board100',
        'project_name': 'npu128_uart100', 'device': meta['device'], 'clock_period_ns': meta['clock_period_ns'],
        'clock_hz': meta['clock_hz'], 'reference_clock_hz': meta['reference_clock_hz'],
        'threads': 1, 'parameters': parameters,
        'source_base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'source_branch': subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip(),
        'source_status': subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).splitlines(),
        'project': f'$PDS_WORK_ROOT/work/{args.name}', 'rtl': CLOCK_RTL, 'files': files,
        'scope': 'real-pin 27 MHz reference / 100 MHz GPLL UART board, 115200 baud; no CPU, DDR or DMA',
        'async_exceptions': {'uart_rxd': 'raw RX only drives first synchronizer FF',
            'board_reset_n': 'raw KEY0 only drives GPLL RST and reset-release asynchronous pins'},
        'bitstream_requested': False, 'board_run': False, 'mapped_functional_run': False}
    (target/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(f'PASS staged real PLL 100 MHz board implementation: {native}')


if __name__ == '__main__':
    main()
