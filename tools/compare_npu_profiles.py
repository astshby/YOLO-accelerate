#!/usr/bin/env python3
"""Compare identical recorded Tiny workloads and retain measured tile evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', type=Path, required=True)
    parser.add_argument('--after', type=Path, required=True)
    parser.add_argument('--baseline-commit', required=True,
                        help='commit supplying before RTL; profile base_commit identifies the driver checkout')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    baseline_commit = subprocess.check_output(['git', 'rev-parse', args.baseline_commit], cwd=ROOT, text=True).strip()
    if args.output.exists():
        parser.error('use a fresh output path')
    before, after = [json.loads(p.read_text()) for p in (args.before, args.after)]
    for key in ('fixture_sha256', 'test_sha256', 'driver_sha256', 'frequency_mhz',
                'macs', 'measured_tile_modes', 'assumptions'):
        assert before[key] == after[key], key
    assert before['frequency_mhz'] == 100 and before['macs'] == 128
    assert after['test_sha256'] == digest(ROOT/'tb/npu_profile.cpp')
    assert after['driver_sha256'] == digest(ROOT/'tools/profile_npu.py')
    fixture = ROOT/'reports/image-cosim-validation.json'
    assert digest(fixture) == before['fixture_sha256']
    graphs = {r['input_size']: r for r in json.loads(fixture.read_text())['image_runs']
              if r['model'] == 'YOLOX-Tiny'}
    for path, expected in before['source_sha256'].items():
        data = subprocess.check_output(['git', 'show', baseline_commit+':'+path], cwd=ROOT)
        assert hashlib.sha256(data).hexdigest() == expected, path
    for path, expected in after['source_sha256'].items():
        assert digest(ROOT/path) == expected, path
    assert (args.before.parent/'workloads.txt').read_bytes() == (args.after.parent/'workloads.txt').read_bytes()
    invariant = ('mac_steps', 'output_vectors', 'port_write_cycles', 'command_cycles', 'useful_macs')
    comparisons = []
    assert len(before['profiles']) == len(after['profiles']) == 6
    for old, new in zip(before['profiles'], after['profiles']):
        assert (old['size'], old['mode']) == (new['size'], new['mode'])
        rows = []
        assert len(old['layers']) == len(new['layers']) == len(graphs[old['size']]['layers']) == 83
        for a, b, archived in zip(old['layers'], new['layers'], graphs[old['size']]['layers']):
            assert a['name'] == b['name'] == archived['name']
            for key in invariant:
                assert a[key] == b[key], (a['name'], key)
            for key in ('input_chw', 'output_chw', 'kernel', 'stride', 'padding'):
                assert a[key] == b[key] == archived[key]
            assert a['mac_steps'] == archived['rtl_mac_steps']
            assert a['command_cycles'] == archived['tile_commands']
            assert a['port_write_cycles']+a['command_cycles'] == archived['rtl_clock_cycles']-archived['rtl_active_cycles']
            row = {'name': a['name'], 'input_chw': a['input_chw'], 'output_chw': a['output_chw'],
                   'kernel': a['kernel'], 'stride': a['stride'],
                   'before_cycles': a['total_cycles'], 'after_cycles': b['total_cycles'],
                   'saved_cycles': a['total_cycles']-b['total_cycles'],
                   **{key: b[key] for key in invariant}}
            rows.append(row)
        a, b = old['totals'], new['totals']
        comparisons.append({'size': old['size'], 'mode': old['mode'],
                            'before': a, 'after': b,
                            'compute_cycle_reduction_fraction': 1-b['total_cycles']/a['total_cycles'],
                            'compute_speedup': a['total_cycles']/b['total_cycles'],
                            'local_port_cycle_reduction_fraction': 1-b['sequential_local_port_cycles']/a['sequential_local_port_cycles'],
                            'local_port_speedup': a['sequential_local_port_cycles']/b['sequential_local_port_cycles'],
                            'layers': rows})
    evidence = []
    for label, path, profile in [('before', args.before, before), ('after', args.after, after)]:
        tiles = [list(map(int, row.split())) for row in (path.parent/'workloads.txt').read_text().splitlines()]
        measured = [list(map(int, row.split())) for row in (path.parent/'run.log').read_text().splitlines()]
        assert len(tiles) == len(measured) == profile['measured_tile_modes']
        assert [r[0] for r in tiles] == [r[0] for r in measured] == list(range(len(tiles)))
        evidence.append({'label': label, 'summary_sha256': digest(path),
                         'source_sha256': profile['source_sha256'],
                         'tile_descriptor_columns': ['id', 'width', 'height', 'cin', 'cout', 'kernel', 'stride', 'pad', 'mode'],
                         'measurement_columns': ['id', 'conv_cycles', 'total_cycles', 'post_stalls', 'mac_steps', 'output_vectors'],
                         'tile_descriptors': tiles, 'tile_measurements': measured,
                         'guard_metrics': profile['metrics']})
    report = {'schema': 'npu128-throughput-comparison-v1', 'status': 'PASS',
              'baseline_commit': baseline_commit, 'frequency_mhz': 100, 'macs': 128,
              'fixture_sha256': before['fixture_sha256'], 'test_sha256': before['test_sha256'],
              'profile_driver_sha256': before['driver_sha256'], 'comparison_driver_sha256': digest(Path(__file__)),
              'scope': after['scope'], 'assumptions': after['assumptions'],
              'archived_consistency': 'All 166 layer shapes, MAC steps, tile counts and port-load costs match the prior actual image-run records.',
              'comparisons': comparisons, 'tile_evidence': evidence}
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    for row in comparisons:
        print({key: row[key] for key in ('size', 'mode', 'compute_speedup', 'local_port_speedup')})


if __name__ == '__main__':
    main()
