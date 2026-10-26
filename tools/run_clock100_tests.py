#!/usr/bin/env python3
"""Guarded timed simulation of the complete board with the unmodified vendor GPLL."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from run_uart_tests import ROOT, UART_RTL, digest

CLOCK_RTL = UART_RTL + ['rtl/board/npu_clock100.sv', 'rtl/board/npu_uart_board100.sv']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vendor-model', type=Path, required=True)
    parser.add_argument('--build-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build = (ROOT/args.build_dir).resolve()
    if not build.is_relative_to(ROOT/'build') or build.exists() or args.output.exists():
        parser.error('use a fresh build/ directory and output path')
    vendor = args.vendor_model.resolve()
    if not vendor.is_file() or vendor.name != 'GTP_GPLL.v':
        parser.error('supply the installed, unmodified PDS GTP_GPLL.v simulation model')
    sources = CLOCK_RTL + ['tb/tb_npu_clock100.sv', 'tools/run_clock100_tests.py',
                           'tools/run_guarded.py', 'config/uart-board100.json']
    hashes = {name: digest(ROOT/name) for name in sources}
    vendor_hash = digest(vendor)
    build.mkdir(parents=True)
    metrics = []

    def guarded(name, command):
        subprocess.run([sys.executable, str(ROOT/'tools/run_guarded.py'), '--rss-mib', '768',
            '--as-mib', '1536', '--min-available-mib', '6144', '--seconds', '600',
            '--log', str(build/f'{name}.log'), '--metrics', str(build/f'{name}.metrics.json'),
            '--', *command], cwd=ROOT, check=True)
        metrics.append(json.loads((build/f'{name}.metrics.json').read_text()))

    # The vendor behavioral model uses real-valued fractional delays. Its warnings
    # are retained in the log. No vendor file is patched or copied into the repo.
    guarded('build', ['verilator', '--binary', '--timing', '--x-initial-edge', '-Wno-fatal', '-j', '1',
        '--threads', '1', '--top-module', 'tb_npu_clock100', '--Mdir', str(build/'obj'),
        *CLOCK_RTL, 'tb/tb_npu_clock100.sv', str(vendor)])
    binary = build/'obj/Vtb_npu_clock100'
    guarded('run', [str(binary)])
    result = (build/'run.log').read_text().strip()
    assert 'PASS real GTP_GPLL board model: 200 ' in result
    assert digest(vendor) == vendor_hash
    for name, expected in hashes.items():
        assert digest(ROOT/name) == expected, name
    report = {'schema': 'npu128-clock100-functional-v1', 'status': 'PASS',
        'source_sha256': hashes, 'vendor_model_sha256': vendor_hash,
        'vendor_model': '$PDS_HOME/arch/vendor/pango/verilog/simulation/GTP_GPLL.v',
        'source_base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'reference_hz': 27000000, 'output_hz': 100000000, 'checked_periods': 200,
        'period_ns_bounds': [9.99, 10.01], 'high_width_ns_bounds': [4.99, 5.01],
        'checks': ['PLL acquisition', 'system held reset until lock', 'three-clock reset release',
                   'UART TX idle', 'KEY0 asynchronous assertion', 'PLL reset and relock'],
        'result': result, 'binary_sha256': digest(binary), 'metrics': metrics,
        'verilator': subprocess.check_output(['verilator', '--version'], text=True).strip(),
        'limitations': ['Timed two-state behavioral simulation with explicit initial asynchronous reset edges (--x-initial-edge), not analog PLL characterization or physical STA.',
            'UART byte traffic is verified separately on the complete digital body at the actual 100 MHz divider.',
            'Icarus 11 loops at zero time in this unmodified vendor model; its failed attempts remain in build/clock100-a and build/clock100-b.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(result)


if __name__ == '__main__':
    main()
