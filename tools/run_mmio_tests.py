#!/usr/bin/env python3
"""Guarded public-bus tests for the standalone NPU MMIO endpoint."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from run_npu_tests import ADAM, NPU, ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verilator', default='verilator')
    parser.add_argument('--cc', default='cc')
    parser.add_argument('--build-dir', type=Path, default=Path('build/mmio'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    build = (ROOT/args.build_dir).resolve()
    if not build.is_relative_to(ROOT/'build'):
        parser.error('use a build directory inside build/')
    if args.output and args.output.exists():
        parser.error('preserve existing evidence; use a fresh output path')
    build.mkdir(parents=True, exist_ok=True)
    rtl = ADAM+NPU+['rtl/npu/npu_mmio128.sv']
    sources = [*rtl, 'tb/npu_mmio128.cpp', 'tb/npu_reference.h', 'tb/npu_mmio_header.c',
               'software/npu128_mmio.h', 'tools/run_mmio_tests.py', 'tools/run_npu_tests.py', 'tools/run_guarded.py']
    digest = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
    reference_path = ROOT/'reports/npu128-ramcompact-implementation.json'
    reference = json.loads(reference_path.read_text())
    assert reference['status'] == 'PASS'
    for name in ADAM+NPU:
        assert digest(ROOT/name) == reference['source_sha256'][name], f'core changed: {name}'
    source_hashes = {name: digest(ROOT/name) for name in sources}
    metrics = []

    def guarded(name, command):
        subprocess.run([sys.executable, str(ROOT/'tools/run_guarded.py'), '--rss-mib', '768',
            '--as-mib', '1536', '--min-available-mib', '6144', '--seconds', '600',
            '--log', str(build/f'{name}.log'), '--metrics', str(build/f'{name}.metrics.json'),
            '--', *command], cwd=ROOT, check=True)
        metrics.append(json.loads((build/f'{name}.metrics.json').read_text()))

    guarded('header', [args.cc, '-std=c11', '-Wall', '-Wextra', '-Werror', '-pedantic',
                       '-fsyntax-only', 'tb/npu_mmio_header.c'])
    guarded('build', [args.verilator, '--cc', '--exe', '--build', '-Wall', '-j', '1', '--threads', '1',
        '--output-split', '5000', '--output-split-cfuncs', '100', '-CFLAGS', '-O1 -g0',
        '--top-module', 'npu_mmio128', '--Mdir', str(build/'obj'), *rtl, str(ROOT/'tb/npu_mmio128.cpp')])
    guarded('run', [str(build/'obj/Vnpu_mmio128')])
    result = (build/'run.log').read_text().strip()
    assert result.startswith('PASS npu_mmio128:'), result
    for name, expected in source_hashes.items():
        assert digest(ROOT/name) == expected, f'source changed during test: {name}'
    third = json.loads((ROOT/'third_party/adamgallas/manifest.json').read_text())
    for name, row in third['files'].items():
        assert digest(ROOT/'third_party/adamgallas'/name) == row['sha256']
    report = {'schema': 'npu128-mmio-functional-v1', 'status': 'PASS', 'date': '2026-10-24',
        'source_branch': subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip(),
        'source_base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'source_sha256': source_hashes, 'result': result, 'metrics': metrics,
        'verilator': subprocess.check_output([args.verilator, '--version'], text=True).strip(),
        'binary_sha256': digest(build/'obj/Vnpu_mmio128'),
        'unchanged_core_reference': {'status': 'PASS', 'path': str(reference_path.relative_to(ROOT)),
                                     'sha256': digest(reference_path)},
        'upstream_byte_integrity': {'status': 'PASS', 'files': len(third['files'])},
        'limitations': ['Local single-clock request/response bus; no AXI, UART, CPU, DMA or CDC integration.',
            'No new-wrapper PDS synthesis, place/route, board test or bitstream. Core-only 100 MHz closure does not establish wrapper timing.',
            'RAM contents survive reset; software must load valid input/weights before use. No loaded-word coverage tracking.',
            'One software owner serializes staging/commit and output-read/pop sequences.']}
    text = json.dumps(report, indent=2)+'\n'
    (build/'summary.json').write_text(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(result)


if __name__ == '__main__':
    main()
