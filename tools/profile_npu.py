#!/usr/bin/env python3
"""Measure representative RTL tiles and weight them by the recorded Tiny graph."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RTL = ['rtl/adam/adam_mul_pair.sv', 'rtl/adam/adam_sdp_ram.sv', 'rtl/adam/adam_mac128.sv',
       'rtl/adam/adam_conv128.sv', 'rtl/npu/npu_postprocess.sv', 'rtl/npu/npu_conv128.sv']


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rtl-root', type=Path, default=ROOT)
    parser.add_argument('--build-dir', type=Path, required=True)
    parser.add_argument('--verilator', default='verilator')
    args = parser.parse_args()
    build=(ROOT/args.build_dir).resolve()
    if not build.is_relative_to(ROOT/'build'):
        parser.error('use a build directory inside repository build/')
    build.mkdir(parents=True,exist_ok=True)
    fixture=ROOT/'reports/image-cosim-validation.json'
    saved=json.loads(fixture.read_text())
    lock=json.loads((ROOT/'config/yolox-tiny.lock.json').read_text())
    graphs=[]; workloads={}
    for graph in saved['image_runs']:
        if graph['model']!='YOLOX-Tiny':
            continue
        assert graph['upstream_commit']==lock['commit'] and len(graph['layers'])==83
        layers=[]
        for row in graph['layers']:
            ci,ih,iw=row['input_chw']; co,oh,ow=row['output_chw']
            k,s=row['kernel'],row['stride']; groups=(ci+7)//8; blocks=(co+15)//16
            assert ci*co*k*k*oh*ow==row['useful_macs']
            side=(16-k)//s+1
            tile_counts=Counter()
            for oc0 in range(0,co,16):
                for y in range(0,oh,side):
                    for x in range(0,ow,side):
                        th,tw=min(side,oh-y),min(side,ow-x)
                        key=((tw-1)*s+k,(th-1)*s+k,ci,min(16,co-oc0),k,s,0)
                        tile_counts[key]+=1
                        for mode in (0,1,2): workloads.setdefault((*key,mode),len(workloads))
            layer={'name':row['name'],'input_chw':row['input_chw'],'output_chw':row['output_chw'],
                   'kernel':k,'stride':s,'padding':row['padding'],'useful_macs':row['useful_macs'],
                   'tiles':tile_counts,'port_write_cycles':blocks*groups*k*k+
                   sum(n*key[0]*key[1]*groups for key,n in tile_counts.items()),
                   'command_cycles':sum(tile_counts.values())}
            layers.append(layer)
        graphs.append((graph['input_size'],layers))
    workfile=build/'workloads.txt'
    workfile.write_text(''.join(' '.join(map(str,(ident,*key)))+'\n' for key,ident in workloads.items()))
    metrics={}
    def guarded(name,command):
        subprocess.run([sys.executable,str(ROOT/'tools/run_guarded.py'),'--log',str(build/f'{name}.log'),
                        '--metrics',str(build/f'{name}.metrics.json'),'--',*command],cwd=ROOT,check=True)
        metrics[name]=json.loads((build/f'{name}.metrics.json').read_text())
    sources=[args.rtl_root.resolve()/name for name in RTL]
    guarded('build',[args.verilator,'--cc','--exe','--build','-Wall','-j','1','--threads','1',
                     '--output-split','5000','--output-split-cfuncs','100','-CFLAGS','-O1 -g0',
                     '--top-module','npu_conv128','--Mdir',str(build/'obj'),
                     *map(str,sources),str(ROOT/'tb/npu_profile.cpp')])
    guarded('run',[str(build/'obj/Vnpu_conv128'),str(workfile)])
    measured={}
    for line in (build/'run.log').read_text().splitlines():
        ident,conv,total,stalls,steps,pixels=map(int,line.split())
        measured[ident]={'conv_cycles':conv,'total_cycles':total,'post_stalls':stalls,
                         'mac_steps':steps,'output_vectors':pixels}
    assert len(measured)==len(workloads)
    reports=[]
    for size,layers in graphs:
        for mode in (0,1,2):
            result=[]
            for layer in layers:
                record={key:value for key,value in layer.items() if key!='tiles'}
                for metric in ('conv_cycles','total_cycles','post_stalls','mac_steps','output_vectors'):
                    record[metric]=sum(n*measured[workloads[(*key,mode)]][metric] for key,n in layer['tiles'].items())
                record['sequential_local_port_cycles']=record['total_cycles']+record['port_write_cycles']+record['command_cycles']
                result.append(record)
            totals={key:sum(r[key] for r in result) for key in ('conv_cycles','total_cycles','post_stalls',
                    'mac_steps','output_vectors','port_write_cycles','command_cycles','sequential_local_port_cycles','useful_macs')}
            totals['compute_ms_at_100mhz']=totals['total_cycles']/100000
            totals['local_port_ms_at_100mhz']=totals['sequential_local_port_cycles']/100000
            totals['useful_mac_utilization']=totals['useful_macs']/(128*totals['total_cycles'])
            reports.append({'size':size,'mode':mode,'totals':totals,'layers':result})
    report={'schema':'npu128-layer-cycle-profile-v1','base_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'fixture_sha256':digest(fixture),'source_sha256':{name:digest(path) for name,path in zip(RTL,sources)},
            'test_sha256':digest(ROOT/'tb/npu_profile.cpp'),'driver_sha256':digest(Path(__file__)),
            'frequency_mhz':100,'macs':128,'measured_tile_modes':len(workloads),'profiles':reports,'metrics':metrics,
            'scope':'measured representative tile counters weighted by all recorded Tiny convolution shapes; zero tensors, no model accuracy rerun',
            'assumptions':['Exact current image backend tiling; software forms halo, hardware pad=0.',
                           'Always-ready output; quantization/LUT preconfigured; modes 1/2 are timing scenarios, not calibrated model execution.',
                           'Local port model: one activation/weight word per clock, weights once per output-channel block, inputs reloaded per tile.',
                           'Excludes DDR/DMA/CPU/non-convolution operators/communication and coefficient configuration.']}
    (build/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
    for item in reports:
        print(json.dumps({'size':item['size'],'mode':item['mode'],**item['totals']}))


if __name__=='__main__':
    main()
