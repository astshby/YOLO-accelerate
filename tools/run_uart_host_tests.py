#!/usr/bin/env python3
"""Verify serial receive behavior and unchanged protocol/scalar logic against firmware-era client."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def unchanged_logic(baseline, candidate):
    def normalized(source):
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == 'SerialWire':
                node.body = [n for n in node.body if not isinstance(n, ast.FunctionDef) or n.name != 'exchange']
        return ast.dump(tree, include_attributes=False)
    assert normalized(baseline) == normalized(candidate), 'only SerialWire.exchange may change'
    return hashlib.sha256(normalized(candidate).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-client', type=Path, required=True)
    parser.add_argument('--firmware-validation', type=Path, default=ROOT/'reports/npu128-uart100-functional.json')
    parser.add_argument('--build-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build = (ROOT/args.build_dir).resolve()
    if not build.is_relative_to(ROOT/'build') or build.exists() or args.output.exists():
        parser.error('use fresh build/ and output paths')
    functional = json.loads(args.firmware_validation.read_text())
    assert functional['status'] == 'PASS'
    baseline_hash = digest(args.baseline_client)
    assert baseline_hash == functional['source_sha256']['tools/npu_uart.py']
    logic_hash = unchanged_logic(args.baseline_client.read_text(), (ROOT/'tools/npu_uart.py').read_text())
    sources = ['tools/npu_uart.py', 'tools/run_uart_board_test.py', 'tb/check_serial_wire.py',
               'tools/run_uart_host_tests.py', 'tools/run_guarded.py']
    hashes = {name: digest(ROOT/name) for name in sources}
    build.mkdir(parents=True)
    subprocess.run([sys.executable, str(ROOT/'tools/run_guarded.py'), '--rss-mib', '256',
        '--as-mib', '512', '--min-available-mib', '6144', '--seconds', '60',
        '--log', str(build/'tests.log'), '--metrics', str(build/'tests.metrics.json'), '--',
        sys.executable, '-m', 'unittest', 'discover', '-s', 'tb', '-p', 'check_serial_wire.py', '-v'],
        cwd=ROOT, check=True)
    result = (build/'tests.log').read_text()
    assert 'Ran 9 tests' in result and result.rstrip().endswith('OK')
    for name, expected in hashes.items(): assert digest(ROOT/name) == expected, name
    report = {'schema':'npu128-uart-host-functional-v1', 'status':'PASS',
        'source_sha256':hashes, 'baseline_client_sha256':baseline_hash,
        'firmware_validation_sha256':digest(args.firmware_validation),
        'unchanged_logic_ast_sha256':logic_hash, 'result':result,
        'metrics':json.loads((build/'tests.metrics.json').read_text()),
        'scope':'Physical serial reception only; codec, Client, scalar reference and simulator API unchanged by AST comparison.',
        'limitations':['Timed serial-port model, not a physical-port performance result.',
            'Firmware-era functional report remains immutable; no new RTL/PDS claim.']}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print('PASS host receive/retry tests and unchanged protocol/scalar logic')


if __name__ == '__main__':
    main()
