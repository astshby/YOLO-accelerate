#!/usr/bin/env python3
"""Low-memory tests for NPU postprocessing and convolution integration."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ADAM = ['rtl/adam/adam_mul_pair.sv', 'rtl/adam/adam_sdp_ram.sv',
        'rtl/adam/adam_mac128.sv', 'rtl/adam/adam_conv128.sv']
NPU = ['rtl/npu/npu_postprocess.sv', 'rtl/npu/npu_conv128.sv']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verilator', default='verilator')
    parser.add_argument('--build-dir', type=Path, default=Path('build/npu128'))
    args = parser.parse_args()
    build = (ROOT / args.build_dir).resolve()
    if not build.is_relative_to(ROOT / 'build'):
        parser.error('build directory must be inside repository build/')
    build.mkdir(parents=True, exist_ok=True)
    summary = {'source_base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
               'branch': subprocess.check_output(['git', 'branch', '--show-current'], text=True).strip(),
               'verilator': subprocess.check_output([args.verilator, '--version'], text=True).strip(),
               'source_sha256': {}, 'results': []}
    cases = [('post', 'npu_postprocess', [ADAM[1], NPU[0]], 'tb/npu_postprocess.cpp'),
             ('conv', 'npu_conv128', ADAM + NPU, 'tb/npu_conv128.cpp'),
             ('prefetch', 'npu_conv128', ADAM + NPU, 'tb/npu_prefetch.cpp'),
             ('measure', 'npu_measure_top', ADAM + NPU + ['rtl/npu/npu_measure_top.sv'],
              'tb/npu_measure_top.cpp')]

    def guarded(name, command):
        subprocess.run([sys.executable, str(ROOT / 'tools/run_guarded.py'), '--log', str(build/f'{name}.log'),
                        '--metrics', str(build/f'{name}.metrics.json'), '--', *command], cwd=ROOT, check=True)
        metrics = json.loads((build/f'{name}.metrics.json').read_text())
        metrics['command'] = [arg.replace(str(ROOT)+'/', '') for arg in metrics['command']]
        return metrics

    for name, top, rtl, cpp in cases:
        for path in [*rtl, cpp, 'tb/npu_reference.h', 'tools/run_npu_tests.py', 'tools/run_guarded.py']:
            summary['source_sha256'][path] = hashlib.sha256((ROOT/path).read_bytes()).hexdigest()
        command = [args.verilator, '--cc', '--exe', '--build', '-Wall', '-j', '1', '--threads', '1',
                   '--output-split', '5000', '--output-split-cfuncs', '100', '-CFLAGS', '-O1 -g0',
                   '--top-module', top, '--Mdir', str(build/name), *rtl, str(ROOT/cpp)]
        compilation = guarded(f'{name}-build', command)
        run = guarded(f'{name}-run', [str(build/name/f'V{top}')])
        log = (build/f'{name}-run.log').read_text().strip()
        marker = 'npu_prefetch' if name == 'prefetch' else top
        if not log.startswith(f'PASS {marker}:'):
            raise RuntimeError(f'missing PASS marker: {name}')
        print(log)
        summary['results'].append({'test': name, 'result': log, 'build': compilation, 'run': run})
    (build/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')


if __name__ == '__main__':
    main()
