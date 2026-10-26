#!/usr/bin/env python3
"""Create a small golden byte trace using the verified real-pin RTL simulator."""
import argparse
import json
from pathlib import Path
import struct
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from npu_uart import (SimulatorWire, Client, request, reply, ID, STATUS, ERRORS,
                      CONTROL, SHAPE, OPTIONS, BIAS, WEIGHT_DATA, ACT_DATA, RAW, CFG_INDEX, CFG_DATA, CFG_COMMIT, weight_request)


class TraceWire(SimulatorWire):
    def __init__(self, executable, clock_hz=27000000):
        super().__init__(executable, clock_hz)
        self.trace = []

    def exchange(self, frame, **kwargs):
        value = super().exchange(frame, **kwargs)
        self.trace.append((bytes(frame), value))
        return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--simulator', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--clock-hz', type=int, choices=[27000000, 100000000], default=27000000)
    parser.add_argument('--bulk-weights', action='store_true')
    args = parser.parse_args()
    wire = TraceWire(args.simulator, args.clock_hz)
    c = Client(wire, bulk_weights=args.bulk_weights)
    try:
        c.sync(0x1357a55a); assert c.read(ID) == 0x4e505531
        c.write(CONTROL, 2)
        for address in (0x1060, 0xffff):
            try:
                c.read(address)
                raise AssertionError('invalid address accepted')
            except RuntimeError as exc:
                assert 'UART status 1' in str(exc)
        assert c.read(ERRORS) == 1
        c.write(ERRORS, 31)
        activation = [-7, 3, 127, -128, 9, 1, -2, 4]
        c.activation(1, 0, int.from_bytes(bytes(v & 255 for v in activation), 'little'))
        weights = [(oc*3+ic) % 17-8 for oc in range(16) for ic in range(8)]
        c.weight(0, bytes(v & 255 for v in weights))
        if args.bulk_weights:
            sequence = (c.sequence-1) & 255
            frame = weight_request(sequence, 0, bytes(v & 255 for v in weights))
            assert reply(wire.exchange(frame), sequence) == (0, 34)
            conflict = weight_request(sequence, 0, bytes([0x5a])*128)
            assert reply(wire.exchange(conflict), sequence) == (4, 0)
        assert c.read(WEIGHT_DATA+124) == int.from_bytes(bytes(v & 255 for v in weights[124:]), 'little')
        biases = [oc*11-9 for oc in range(15)]+[0x7fffffff]
        for oc, value in enumerate(biases): c.write(BIAS+oc*4, value)
        assert c.read(BIAS+60) == 0x7fffffff
        c.write(SHAPE, 1 | (1 << 8) | (8 << 16))
        c.write(OPTIONS, 16 | (1 << 8) | (1 << 10))
        c.write(CONTROL, 1)
        seq = (c.sequence-1) & 255
        assert reply(wire.exchange(request(seq, 1, CONTROL, 1, 15)), seq) == (0, 0)
        output = c.output()
        assert output is not None and output['meta'] == 0xffff and output['flags'] == 13
        raw = [(biases[oc]+sum(activation[ic]*weights[oc*8+ic] for ic in range(8))) & 0xffffffff for oc in range(16)]
        assert output['raw'] == raw
        signed = [v if v < 0x80000000 else v-0x100000000 for v in raw]
        assert output['quantized'] == [min(127, max(-128, v)) & 255 for v in signed]
        seq = (c.sequence-1) & 255  # Cached POP must succeed even though the slot is now empty.
        assert reply(wire.exchange(request(seq, 1, 68, 1, 15)), seq) == (0, 0)
        assert c.read(STATUS) == 4
        bad = bytearray(request(c.sequence, 1, ACT_DATA, 0, 15)); bad[-1] ^= 1
        assert reply(wire.exchange(bad), c.sequence)[0] == 3
        assert c.read(ACT_DATA) == int.from_bytes(bytes(v & 255 for v in activation[:4]), 'little')
        try:
            c.read(RAW)
            raise AssertionError('empty result accepted')
        except RuntimeError as exc:
            assert 'UART status 1' in str(exc)
        assert c.read(ERRORS) == 2
        c.write(ERRORS, 31)
        c.write(CFG_INDEX, 3 << 8); c.write(CFG_DATA, 0); c.write(CFG_COMMIT, 1)
        assert c.read(ERRORS) == 16
        c.write(ERRORS, 31)
        cycles = wire.close()
        if args.bulk_weights:
            args.output.write_text(''.join(f'{len(a)} {int.from_bytes(a,"little"):0284x} {int.from_bytes(b,"little"):024x}\n' for a, b in wire.trace))
        else:
            args.output.write_text(''.join(f'{int.from_bytes(a,"little"):028x} {int.from_bytes(b,"little"):024x}\n' for a, b in wire.trace))
        args.output.with_suffix('.json').write_text(json.dumps({'status': 'PASS', 'frames': len(wire.trace),
            'public_pin_simulation_clocks': cycles, 'output': output}, indent=2)+'\n')
        print(f'PASS generated {len(wire.trace)} scalar-checked public-pin golden frames')
    finally:
        if wire.process.poll() is None:
            wire.process.terminate(); wire.process.wait(timeout=10)


if __name__ == '__main__':
    main()
