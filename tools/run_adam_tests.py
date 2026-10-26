#!/usr/bin/env python3
"""Sequential Verilator regression of the imported primitives and 128-MAC port."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--verilator', default='verilator')
    p.add_argument('--only', choices=['all', 'primitives', 'mac', 'conv'], default='all')
    p.add_argument('--build-dir', type=Path, default=Path('build/adam128'))
    args = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    build = (root / args.build_dir).resolve()
    if not build.is_relative_to(root / 'build'):
        p.error('build directory must be inside this repository build/')
    build.mkdir(parents=True, exist_ok=True)
    rtl = ['rtl/adam/adam_mul_pair.sv', 'rtl/adam/adam_sdp_ram.sv',
           'rtl/adam/adam_mac128.sv', 'rtl/adam/adam_conv128.sv']
    upstream = ['third_party/adamgallas/source/cal_mul_int8_x2.v',
                'third_party/adamgallas/source/cal_mul_int8_x2_dsp.v',
                'third_party/adamgallas/source/com_simple_dual_port_ram.v']
    cases = [('primitives', 'tb_adam_primitives',
              ['tb/adam_upstream.vlt', 'tb/tb_adam_primitives.sv', *upstream, *rtl[:2]],
              'tb/adam_primitives.cpp', 'PASS adam primitives:'),
             ('mac', 'adam_mac128', [rtl[0], rtl[2]], 'tb/adam_mac128.cpp', 'PASS adam_mac128:'),
             ('conv', 'adam_conv128', rtl, 'tb/adam_conv128.cpp', 'PASS adam_conv128:')]
    summary = {'branch': subprocess.check_output(['git', 'branch', '--show-current'], cwd=root, text=True).strip(),
               'source_base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
               'tool': subprocess.check_output([args.verilator, '--version'], text=True).strip(),
               'source_sha256': {}, 'results': [], 'pds_run': False, 'board_run': False}

    def guarded(name, command):
        subprocess.run([sys.executable, str(root/'tools/run_guarded.py'),
                        '--log', str(build/f'{name}.log'), '--metrics', str(build/f'{name}.metrics.json'),
                        '--', *command], cwd=root, check=True)
        metrics = json.loads((build/f'{name}.metrics.json').read_text())
        metrics['command'] = [x.replace(str(root)+'/', '') for x in metrics['command']]
        return metrics

    for name, top, sources, cpp, marker in cases:
        if args.only != 'all' and args.only != name:
            continue
        out = build/name
        dependencies = ['software/adam128_cycle_model.h'] if name == 'conv' else []
        for source in [*sources, cpp, *dependencies, 'tools/run_guarded.py', 'tools/run_adam_tests.py']:
            summary['source_sha256'][source] = hashlib.sha256((root/source).read_bytes()).hexdigest()
        command = [args.verilator, '--cc', '--exe', '--build', '-Wall', '-j', '1', '--threads', '1',
                   '--output-split', '5000', '--output-split-cfuncs', '100',
                   '-CFLAGS', '-O1 -g0', '--top-module', top, '--Mdir', str(out),
                   *sources, str(root/cpp)]
        compilation = guarded(f'{name}-build', command)
        execution = guarded(f'{name}-run', [str(out/f'V{top}')])
        log = (build/f'{name}-run.log').read_text()
        if marker not in log:
            raise RuntimeError(f'missing PASS marker: {name}')
        print(log.strip())
        summary['results'].append({'name': name, 'status': 'PASS', 'result': log.strip(),
                                   'build': compilation, 'run': execution,
                                   'log': str((build/f'{name}-run.log').relative_to(root))})
    if args.only == 'all':
        structure = guarded('structure', [args.verilator, '--json-only', '-Wall',
                             '--top-module', 'adam_conv128', '--json-only-output', str(build/'structure.json'),
                             '--json-only-meta-output', str(build/'structure.meta.json'), *rtl])
        ast = json.loads((build/'structure.json').read_text())

        def walk(node):
            if isinstance(node, dict):
                yield node
                for value in node.values():
                    yield from walk(value)
            elif isinstance(node, list):
                for value in node:
                    yield from walk(value)

        modules = {m['name']: m for m in ast['modulesp'] if m['type'] == 'MODULE'}
        pairs = [n for n in walk(modules['adam_mac128'])
                 if n.get('type') == 'CELL' and n.get('modName') == 'adam_mul_pair']
        multipliers = [n for n in walk(modules['adam_mul_pair']) if n.get('type') == 'MULS']
        if len(pairs) != 64 or len(multipliers) != 2:
            raise RuntimeError('expected 64 paired units with two signed products each')
        summary['structure'] = {'paired_units': len(pairs), 'products_per_unit': len(multipliers),
                                'parallel_int8_products': len(pairs)*len(multipliers),
                                'scope': 'MAC datapath only; not a Pango DSP/resource report',
                                'metrics': structure}
        print('PASS elaborated MAC structure: 64 pairs x 2 signed products = 128')
    (build/f'summary-{args.only}.json').write_text(json.dumps(summary, indent=2)+'\n')


if __name__ == '__main__':
    main()
