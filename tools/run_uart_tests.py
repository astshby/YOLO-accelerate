#!/usr/bin/env python3
"""Guarded byte-bridge and complete public-pin UART board verification."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from run_npu_tests import ADAM, NPU, ROOT

UART_RTL = ADAM+NPU+['rtl/npu/npu_mmio128.sv', 'rtl/io/uart_8n1.sv',
                    'rtl/io/uart_mmio_bridge.sv', 'rtl/board/npu_uart_board.sv']


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=Path('build/uart-test'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--clock-hz', type=int, choices=[27000000, 100000000], default=27000000)
    parser.add_argument('--bulk-weights', action='store_true')
    parser.add_argument('--mmio-validation', type=Path, default=Path('reports/npu128-mmio-timing-final-validation.json'))
    args = parser.parse_args()
    build = (ROOT/args.build_dir).resolve()
    if not build.is_relative_to(ROOT/'build') or (args.output and args.output.exists()):
        parser.error('use build/ and a fresh output path')
    build.mkdir(parents=True, exist_ok=True)
    sources = UART_RTL+['sim/npu_uart_server.cpp', 'tb/uart_mmio_bridge.cpp', 'tb/check_uart.py',
        'tools/npu_uart.py', 'tb/check_uart_bulk_framing.py', 'tb/check_serial_wire.py', 'tools/run_uart_tests.py', 'tools/run_npu_tests.py', 'tools/run_guarded.py',
        'config/uart-board.json', 'constraints/npu_uart_board.fdc']
    hashes = {name: digest(ROOT/name) for name in sources}
    mmio_path = ROOT/args.mmio_validation
    mmio = json.loads(mmio_path.read_text())
    assert mmio['status'] == 'PASS'
    for name in ADAM+NPU+['rtl/npu/npu_mmio128.sv']:
        assert hashes[name] == mmio['source_sha256'][name], name
    metrics = []

    def guarded(name, command):
        subprocess.run([sys.executable, str(ROOT/'tools/run_guarded.py'), '--rss-mib', '768',
            '--as-mib', '1536', '--min-available-mib', '6144', '--seconds', '600',
            '--log', str(build/f'{name}.log'), '--metrics', str(build/f'{name}.metrics.json'),
            '--', *command], cwd=ROOT, check=True)
        metrics.append(json.loads((build/f'{name}.metrics.json').read_text()))

    def compile(top, rtl, cpp, name, extra=()):
        guarded(name+'-build', ['verilator', '--cc', '--exe', '--build', '-Wall', '-j', '1',
            '--threads', '1', '--output-split', '5000', '--output-split-cfuncs', '100',
            '-CFLAGS', f'-O2 -g0 -DUART_CLOCK_HZ={args.clock_hz}', '--top-module', top, '--Mdir', str(build/name),
            *extra, *rtl, str(ROOT/cpp)])
        return str(build/name/f'V{top}')

    guarded('serial-unit', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tb', '-p', 'check_serial_wire.py', '-v'])
    unit = compile('uart_mmio_bridge', ['rtl/io/uart_mmio_bridge.sv'], 'tb/uart_mmio_bridge.cpp',
                   'bridge', ['-GINTERBYTE_TIMEOUT=32'])
    guarded('bridge-run', [unit])
    unit_result = (build/'bridge-run.log').read_text().strip()
    assert unit_result.startswith('PASS uart_mmio_bridge:'), unit_result
    board = compile('npu_uart_board', UART_RTL, 'sim/npu_uart_server.cpp', 'board',
                    [f'-GCLOCK_HZ={args.clock_hz}', f'-GINTERBYTE_TIMEOUT={args.clock_hz//10}'])
    guarded('board-run', [sys.executable, str(ROOT/'tb/check_uart.py'), '--simulator', board,
                          '--output', str(build/'board-results.json'), '--clock-hz', str(args.clock_hz),
                          *(['--bulk-weights'] if args.bulk_weights else [])])
    if args.bulk_weights:
        guarded('framing-isolation', [sys.executable, str(ROOT/'tb/check_uart_bulk_framing.py'), '--simulator', board, '--clock-hz', str(args.clock_hz)])
    result = (build/'board-run.log').read_text().strip()
    assert result.startswith('PASS npu_uart_board:'), result
    board_result = json.loads((build/'board-results.json').read_text())
    assert board_result['status'] == 'PASS' and board_result['uart_clocks_per_bit'] == (args.clock_hz+57600)//115200
    for name, expected in hashes.items():
        assert digest(ROOT/name) == expected, f'source changed during verification: {name}'
    third = json.loads((ROOT/'third_party/adamgallas/manifest.json').read_text())
    for name, row in third['files'].items():
        assert digest(ROOT/'third_party/adamgallas'/name) == row['sha256']
    report = {'schema': 'npu128-uart-functional-v1', 'status': 'PASS', 'date': '2026-10-26',
        'source_branch': subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip(),
        'source_base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'source_sha256': hashes, 'byte_bridge_result': unit_result, 'board_result': board_result,
        'result': result, 'metrics': metrics, 'binary_sha256': digest(board),
        'bulk_weights': args.bulk_weights, 'framing_isolation_result': (build/'framing-isolation.log').read_text().strip() if args.bulk_weights else None,
        'default_parameters': {'CLOCK_HZ': args.clock_hz, 'BAUD': 115200, 'INTERBYTE_TIMEOUT': args.clock_hz//10},
        'mmio_reference_sha256': digest(mmio_path),
        'verilator': subprocess.check_output(['verilator', '--version'], text=True).strip(),
        'upstream_byte_integrity': {'status': 'PASS', 'files': len(third['files'])},
        'board_run': False, 'gate_simulation_run': False, 'physical_implementation_run': False,
        'limitations': ['Public-pin RTL simulation at default divider/timeout, not a physical serial port test.',
            'No bitstream, PLL, CPU, DDR, DMA or full-model accuracy result.',
            'Exactly-once retry applies to the last completed request in one stop-and-wait session; external reset clears the cache.']}
    text = json.dumps(report, indent=2)+'\n'
    (build/'summary.json').write_text(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(unit_result)
    print(result)


if __name__ == '__main__':
    main()
