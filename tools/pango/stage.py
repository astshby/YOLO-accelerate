#!/usr/bin/env python3
"""Stage one independent diagnostic NPU synthesis in a fresh native Windows folder."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
RTL = ['rtl/adam/adam_mul_pair.sv', 'rtl/adam/adam_sdp_ram.sv',
       'rtl/adam/adam_mac128.sv', 'rtl/adam/adam_conv128.sv',
       'rtl/npu/npu_postprocess.sv', 'rtl/npu/npu_conv128.sv', 'rtl/npu/npu_measure_top.sv']


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root', type=Path, default=os.environ.get('PDS_WORK_ROOT'))
    parser.add_argument('--name', required=True, help='fresh subdirectory under work/')
    args = parser.parse_args()
    if not args.work_root or not args.work_root.is_dir():
        parser.error('set PDS_WORK_ROOT or pass an existing --work-root')
    if not args.name or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in args.name):
        parser.error('name must contain only lower-case ASCII letters, digits, hyphen or underscore')
    work_root = args.work_root.resolve()
    target = work_root / 'work' / args.name
    native = str(target) if os.name == 'nt' else subprocess.check_output(
        ['wslpath', '-w', str(target)], text=True).strip()
    if not PureWindowsPath(native).drive or native.startswith('\\\\') or not native.isascii():
        parser.error('work root must be on a native Windows drive with an ASCII path')
    if any(c in native for c in '{}"\n\r') or target.exists():
        parser.error('use a safe fresh directory; existing results are never overwritten')
    if target == ROOT or ROOT in target.parents or target in ROOT.parents:
        parser.error('PDS output must be outside the source repository')
    fdc = ['# Diagnostic 100 MHz external clock; not a board clock or pin assignment.',
           'create_clock -name {clk} -period 10 -waveform {0 5} [get_ports {clk}]',
           'set_clock_uncertainty -setup 0.2 [get_clocks {clk}]',
           'set_clock_uncertainty -hold 0.05 [get_clocks {clk}]']
    for port in ('rst', 'scan_shift', 'scan_update', 'scan_capture', 'scan_din'):
        for bound, delay in (('min', 0), ('max', 1)):
            fdc.append(f'set_input_delay -clock [get_clocks {{clk}}] -{bound} {delay} [get_ports {{{port}}}]')
    for bound, delay in (('min', 0), ('max', 1)):
        fdc.append(f'set_output_delay -clock [get_clocks {{clk}}] -{bound} {delay} [get_ports {{scan_dout}}]')
    tcl = ['# Standalone NPU diagnostic. Synthesis only; no hgb or reference source mutations.',
           'set_arch -family Logos2 -device PG2L200H -package FBB676 -speedgrade -6',
           'set_option max_threads 1', *(f'add_design {{{name}}}' for name in RTL),
           'add_constraint {measurement.fdc}',
           'compile -top_module npu_measure_top -verilog_standard SystemVerilog -loop_limit 65536',
           'synthesize -ads -selected_syn_tool_opt 2 -max_path 100 -nworst 3 '
           '-report_logic_level_distribution -report_exception TRUE']
    target.mkdir(parents=True)
    files = {}
    for name in [*RTL, 'tools/pango/stage.py', 'tools/pango/run_synthesis.ps1', 'tools/pango/GuardedJob.cs']:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
        files[name] = sha256(destination)
    for name, lines in [('measurement.fdc', fdc), ('synthesize.tcl', tcl)]:
        (target / name).write_text('\n'.join(lines)+'\n')
        files[name] = sha256(target / name)
    manifest = {'schema': 'npu128-pango-synthesis-v1', 'top': 'npu_measure_top',
                'device': 'PG2L200H-6IFBB676', 'clock_period_ns': 10, 'threads': 1,
                'parameters': {'MAX_SIDE': 16, 'MAX_CIN': 768, 'MAC': 128, 'post_lanes': 4,
                               'activation_banks': 2, 'activation_words_per_bank': 24576,
                               'measurement_input_bits': 1716, 'activation_segments_per_bank': 3,
                               'activation_segment_words': 8192},
                'scope': 'diagnostic scan harness; no CPU, DDR, AXI, PLL, pinout or board claim',
                'baseline_tag': 'baseline/adam128-image-cosim-20261023',
                'source_base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'source_branch': subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip(),
                'source_status': subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).splitlines(),
                'project': f'$PDS_WORK_ROOT/work/{args.name}', 'rtl': RTL, 'files': files}
    (target/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(f'PASS staged {len(files)} hashed inputs: {native}')


if __name__ == '__main__':
    main()
