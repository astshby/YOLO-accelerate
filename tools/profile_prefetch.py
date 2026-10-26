#!/usr/bin/env python3
"""Execute every recorded Tiny tile with serial or overlapped activation loads."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=Path('build/prefetch/profile'))
    parser.add_argument('--executable', type=Path, default=Path('build/prefetch/npu/prefetch/Vnpu_conv128'))
    args = parser.parse_args()
    build = (ROOT/args.build_dir).resolve()
    if not build.is_relative_to(ROOT/'build'):
        parser.error('use a repository build directory')
    build.mkdir(parents=True, exist_ok=True)
    executable = (ROOT/args.executable).resolve()
    suite_path = executable.parent.parent/'summary.json'
    suite = json.loads(suite_path.read_text())
    for name, expected in suite['source_sha256'].items():
        assert digest(ROOT/name) == expected, f'stale simulator: {name}'
    fixture = ROOT/'reports/image-cosim-validation.json'
    graphs = [r for r in json.loads(fixture.read_text())['image_runs'] if r['model']=='YOLOX-Tiny']
    lock = json.loads((ROOT/'config/yolox-tiny.lock.json').read_text())
    workloads=[]
    for graph in graphs:
        assert graph['upstream_commit']==lock['commit'] and len(graph['layers'])==83
        for mode in (0,1,2):
            workloads.append(f'R {graph["input_size"]} {mode}\n')
            for layer in graph['layers']:
                ci,_,_=layer['input_chw']; co,oh,ow=layer['output_chw']; k,s=layer['kernel'],layer['stride']
                assert layer['useful_macs']==ci*co*oh*ow*k*k
                side=(16-k)//s+1
                for oc0 in range(0,co,16):
                    tiles=[]
                    for y in range(0,oh,side):
                        for x in range(0,ow,side):
                            th,tw=min(side,oh-y),min(side,ow-x)
                            tiles.append(f'{(tw-1)*s+k} {(th-1)*s+k} {ci} {min(16,co-oc0)} {k} {s} 0\n')
                    workloads.append(f'B {len(tiles)}\n'); workloads.extend(tiles)
            workloads.append('E\n')
    workfile=build/'workloads.txt'; workfile.write_text(''.join(workloads))
    subprocess.run([sys.executable,str(ROOT/'tools/run_guarded.py'),'--seconds','600',
                    '--log',str(build/'run.log'),'--metrics',str(build/'run.metrics.json'),
                    '--',str(executable),str(workfile)],cwd=ROOT,check=True)
    measured=[json.loads(row) for row in (build/'run.log').read_text().splitlines()]
    assert len(measured)==12
    baseline_path=ROOT/'reports/npu128-throughput-profile.json'
    prior=json.loads(baseline_path.read_text())
    assert prior['fixture_sha256']==digest(fixture)
    comparisons=[]
    for previous in prior['comparisons']:
        size,mode=previous['size'],previous['mode']
        serial,overlap=[next(r for r in measured if (r['size'],r['mode'],r['overlap'])==(size,mode,b)) for b in (0,1)]
        assert serial['cycles']==previous['after']['sequential_local_port_cycles'],(size,mode,'serial cycles')
        assert serial['active_cycles']==previous['after']['total_cycles']
        assert serial['act_writes']+serial['weight_writes']==previous['after']['port_write_cycles']
        for name in ('active_cycles','conv_cycles','mac_steps','post_stalls','act_writes','weight_writes'):
            assert serial[name]==overlap[name],(size,mode,name)
        assert overlap['overlap_writes']>0 and overlap['cycles']<serial['cycles']
        comparisons.append({'size':size,'mode':mode,'serial':serial,'prefetch':overlap,
            'cycle_reduction_fraction':1-overlap['cycles']/serial['cycles'],
            'speedup':serial['cycles']/overlap['cycles'],
            'serial_ms_at_100mhz':serial['cycles']/100000,
            'prefetch_ms_at_100mhz':overlap['cycles']/100000})
    report={'schema':'npu128-prefetch-profile-v1','status':'PASS','frequency_mhz':100,'macs':128,
            'fixture_sha256':digest(fixture),'baseline_sha256':digest(baseline_path),
            'source_sha256':suite['source_sha256'],'profile_driver_sha256':digest(Path(__file__)),
            'binary_sha256':digest(executable),'workloads_sha256':digest(workfile),
            'comparisons':comparisons,'metrics':json.loads((build/'run.metrics.json').read_text()),
            'scope':'all tiles of the recorded 83-layer Tiny shapes executed through real RTL ports; zero data, no model accuracy rerun',
            'assumptions':['100 MHz cycle conversion, always-ready final output, preconfigured quantization/identity LUT.',
                'One 64-bit activation word or one 1024-bit weight word per ready clock on independent local ports.',
                'Weights reused per output-channel block. Only activation loading overlaps; no cross-block prefetch.',
                'Excludes DDR/DMA/CPU/non-convolution operators, communication and coefficient configuration.']}
    (build/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
    for row in comparisons:
        print({key:row[key] for key in ('size','mode','cycle_reduction_fraction','speedup','prefetch_ms_at_100mhz')})


if __name__=='__main__':
    main()
