"""A damaged bulk payload must not become a nested standalone write."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from npu_uart import Client, SimulatorWire, ACT_DATA, request, weight_request
p=argparse.ArgumentParser();p.add_argument('--simulator',type=Path,required=True);p.add_argument('--clock-hz',type=int,default=100000000);a=p.parse_args()
wire=SimulatorWire(a.simulator,a.clock_hz)
try:
    c=Client(wire);c.sync(0x43524351);c.write(ACT_DATA,0x11223344)
    data=bytearray(128)
    nested=request(100,1,ACT_DATA,0xdeadbeef,15)
    data[64:64+len(nested)]=nested
    try:
        unexpected=wire.exchange(weight_request(c.sequence,0,data),bad_stop=32)
        print('Unexpected nested reply:',unexpected.hex(),flush=True)
    except TimeoutError:
        pass
    value=c.read(ACT_DATA)
    assert value==0x11223344,f'framing-damaged bulk executed nested payload: ACT_DATA={value:08x}'
    print('PASS damaged bulk cannot execute embedded legacy frame')
finally:
    wire.close()
