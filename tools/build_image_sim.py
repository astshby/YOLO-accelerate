#!/usr/bin/env python3
"""Build the image convolution transport, with the same low-memory RTL settings."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ['rtl/adam/adam_mul_pair.sv', 'rtl/adam/adam_sdp_ram.sv',
           'rtl/adam/adam_mac128.sv', 'rtl/adam/adam_conv128.sv', 'sim/adam_image_server.cpp']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verilator', default='verilator')
    parser.add_argument('--build-dir', type=Path, default=Path('build/image-cosim'))
    args = parser.parse_args()
    build = (ROOT / args.build_dir).resolve()
    if not build.is_relative_to(ROOT / 'build'):
        parser.error('build directory must be inside repository build/')
    build.mkdir(parents=True, exist_ok=True)
    binary = build / 'obj/Vadam_conv128'
    command = [args.verilator, '--cc', '--exe', '--build', '-Wall', '-j', '1', '--threads', '1',
               '--output-split', '5000', '--output-split-cfuncs', '100', '-CFLAGS', '-O1 -g0',
               '--top-module', 'adam_conv128', '--Mdir', str(binary.parent),
               *SOURCES[:-1], str(ROOT / SOURCES[-1])]
    identity = {
        'verilator': subprocess.check_output([args.verilator, '--version'], text=True).strip(),
        'source_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in [*SOURCES, 'software/adam128_cycle_model.h']},
        'build_command': command,
        'build_driver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    manifest_path = build / 'manifest.json'
    if manifest_path.exists() and binary.exists():
        previous = json.loads(manifest_path.read_text())
        if (all(previous.get(key) == value for key, value in identity.items()) and
                previous.get('binary_sha256') == hashlib.sha256(binary.read_bytes()).hexdigest()):
            print(f'PASS image simulator already matches sources -> {binary.relative_to(ROOT)}')
            return  # Keep the actual build log and peak-memory measurement.
    subprocess.run([sys.executable, str(ROOT / 'tools/run_guarded.py'),
                    '--log', str(build / 'build.log'), '--metrics', str(build / 'build.metrics.json'),
                    '--', *command], cwd=ROOT, check=True)
    manifest = {
        **identity,
        'source_base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'build_metrics': json.loads((build / 'build.metrics.json').read_text()),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'PASS image simulator -> {binary.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
