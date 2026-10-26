#!/usr/bin/env python3
"""Versioned, CRC-protected stop-and-wait client for the NPU board diagnostic."""
import argparse
import binascii
import json
from pathlib import Path
import secrets
import struct
import subprocess
import time

ID, ABI, CONTROL, STATUS, ERRORS = 0, 4, 8, 12, 16
SHAPE, OPTIONS, ACT_INDEX, WEIGHT_INDEX, CFG_INDEX, CFG_DATA = 32, 36, 40, 44, 48, 52
ACT_COMMIT, WEIGHT_COMMIT, CFG_COMMIT, POP = 56, 60, 64, 68
PERF_MAC, META, FLAGS, ACT_DATA, BIAS, WEIGHT_DATA, RAW, QUANT = 72, 88, 92, 96, 128, 256, 512, 576
BUSY, OUTPUT_VALID, DONE, ERROR = 1, 2, 4, 8


def crc16(data):
    return binascii.crc_hqx(data, 0xffff)


def request(sequence, op, address=0, data=0, strobes=0):
    if not 0 <= sequence < 256 or not 0 <= op < 256 or not 0 <= address < 65536 or not 0 <= strobes < 256:
        raise ValueError('request field outside wire width')
    if not 0 <= data <= 0xffffffff:
        raise ValueError('data must be a 32-bit bit pattern')
    body = struct.pack('<BBBBHI', 1, sequence, op, strobes, address, data)
    return b'\xa5\x5a'+body+struct.pack('<H', crc16(body))


def weight_request(sequence, index, data):
    if not 0 <= sequence < 256 or not 0 <= index < 864 or len(data) != 128:
        raise ValueError('bulk weight frame shape/index')
    body = struct.pack('<BBBBHI', 1, sequence, 3, 0, WEIGHT_DATA, index)+bytes(data)
    return b'\xa5\x5a'+body+struct.pack('<H', crc16(body))


def reply(frame, sequence):
    if len(frame) != 12 or frame[:3] != b'\x5a\xa5\x01' or frame[3] != sequence or frame[5] != 0:
        raise ValueError('reply header, length or sequence mismatch')
    if crc16(frame[2:10]) != struct.unpack('<H', frame[10:])[0]:
        raise ValueError('reply CRC mismatch')
    return frame[4], struct.unpack('<I', frame[6:10])[0]


class SimulatorWire:
    """Only public UART/reset pins, at the real default divider and timeout."""
    def __init__(self, executable, clock_hz=27000000):
        self.process = subprocess.Popen([str(Path(executable).resolve())], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, text=True, bufsize=1)
        self.clock_hz = clock_hz
        self.divisor, self.interbyte_timeout = (clock_hz+57600)//115200, clock_hz//10
        if self.process.stdout.readline().strip() != f'READY {self.divisor} {self.interbyte_timeout}':
            raise RuntimeError('unexpected simulator configuration')
        self.frames = 0
        self.drop_replies = self.corrupt_replies = 0

    def command(self, text):
        self.process.stdin.write(text+'\n')
        self.process.stdin.flush()
        line = self.process.stdout.readline().strip()
        if not line:
            raise RuntimeError('UART simulator exited')
        return line

    def exchange(self, frame, delta=0, bad_stop=-1):
        self.frames += 1
        line = self.command(f'X {frame.hex()} {delta} {bad_stop}')
        if line == 'NONE':
            raise TimeoutError('no UART response')
        result = bytes.fromhex(line)
        if self.drop_replies:
            self.drop_replies -= 1
            raise TimeoutError('injected lost reply after the real RTL executed')
        if self.corrupt_replies:
            self.corrupt_replies -= 1
            result = result[:-1]+bytes([result[-1]^0x40])
        return result

    def close(self):
        cycles = int(self.command('Q').split()[1])
        self.process.wait(timeout=10)
        if self.process.returncode:
            raise RuntimeError('simulator failed')
        return cycles


class SerialWire:
    def __init__(self, port, baud, timeout):
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError('Physical serial operation requires pyserial in this Python environment.') from exc
        self.port = serial.Serial(port, baudrate=baud, timeout=0.02, write_timeout=timeout)
        self.port.reset_input_buffer()
        self.timeout = timeout

    def exchange(self, frame):
        if self.port.write(frame) != len(frame):
            raise OSError('short serial write')
        self.port.flush()
        data = bytearray()
        deadline = time.monotonic()+self.timeout
        while time.monotonic() < deadline:
            # A reply is exactly 12 bytes; read(64) waits for its timeout even
            # after the full reply arrived. After resync request the missing tail.
            data.extend(self.port.read(max(1, 12-len(data))))
            while len(data) >= 12:
                if data[:2] != b'\x5a\xa5':
                    del data[0]
                    continue
                candidate = bytes(data[:12])
                try:
                    reply(candidate, frame[3])
                    return candidate
                except ValueError:
                    del data[0]
        raise TimeoutError('serial reply timeout; press KEY0 after configuration and check port/baud')

    def close(self):
        self.port.close()


class Client:
    def __init__(self, wire, retries=2, bulk_weights=False):
        self.wire, self.retries = wire, retries
        self.bulk_weights = bulk_weights
        self.sequence = 0
        self.transactions = self.retries_used = 0

    def transact(self, op, address=0, data=0, strobes=0):
        frame = request(self.sequence, op, address, data, strobes)
        return self._exchange(frame, address)

    def _exchange(self, frame, address):
        for attempt in range(self.retries+1):
            try:
                status, value = reply(self.wire.exchange(frame), self.sequence)
                if status == 3:
                    raise ValueError('FPGA rejected request CRC')
            except (TimeoutError, ValueError):
                if attempt == self.retries:
                    raise
                self.retries_used += 1
                continue  # Same sequence AND payload; never repeat START/POP with a new sequence.
            self.sequence = (self.sequence+1) & 255
            self.transactions += 1
            if status:
                raise RuntimeError(f'UART status {status}, address 0x{address:04x}; check ERRORS before continuing')
            return value
        raise AssertionError('unreachable')

    def sync(self, nonce=None):
        nonce = secrets.randbits(32) if nonce is None else nonce
        if self.transact(2, data=nonce) != nonce:
            raise RuntimeError('link synchronization nonce mismatch')

    def read(self, address):
        return self.transact(0, address)

    def write(self, address, value, strobes=15):
        return self.transact(1, address, value & 0xffffffff, strobes)

    def configure(self, kind, address, value):
        self.write(CFG_INDEX, (kind << 8)|address)
        self.write(CFG_DATA, value)
        self.write(CFG_COMMIT, 1)
        if self.read(ERRORS):
            raise RuntimeError('device configuration error')

    def activation(self, bank, index, word):
        if bank not in (0, 1) or not 0 <= index < 24576:
            raise ValueError('activation index')
        self.write(ACT_INDEX, (bank << 31)|index)
        self.write(ACT_DATA, word & 0xffffffff)
        self.write(ACT_DATA+4, word >> 32)
        self.write(ACT_COMMIT, 1)

    def weight(self, index, data):
        if not 0 <= index < 864 or len(data) != 128:
            raise ValueError('weight shape')
        if self.bulk_weights:
            if self._exchange(weight_request(self.sequence, index, data), WEIGHT_DATA) != 34:
                raise RuntimeError('bulk weight acknowledgement count')
            return
        self.write(WEIGHT_INDEX, index)
        for i, word in enumerate(struct.unpack('<32I', data)):
            self.write(WEIGHT_DATA+i*4, word)
        self.write(WEIGHT_COMMIT, 1)

    def output(self):
        if not self.read(STATUS) & OUTPUT_VALID:
            return None
        meta, flags = self.read(META), self.read(FLAGS)
        raw = [self.read(RAW+i*4) for i in range(16)]
        quantized = b''.join(struct.pack('<I', self.read(QUANT+i*4)) for i in range(4))
        self.write(POP, 1)
        return {'meta': meta, 'flags': flags, 'raw': raw, 'quantized': list(quantized)}


def self_test(client):
    """Small synthetic integer convolutions, not YOLO inference or calibration."""
    if client.read(ID) != 0x4e505531 or client.read(ABI) != 0x10000:
        raise RuntimeError('device identity mismatch')
    client.write(CONTROL, 2)
    table = [(i ^ 0xa5) for i in range(256)]
    for oc in range(3):
        client.configure(0, oc, (oc+1)*13)
        client.configure(1, oc, 7+oc)
    for i, value in enumerate(table):
        client.configure(2, i, value)
    cases = []
    for mode, k, size, ci in [(0, 1, 2, 8), (1, 3, 2, 9), (2, 3, 2, 9)]:
        bank, co, pad = mode % 2, 3, k//2
        groups = (ci+7)//8
        image = [((i*7+mode*3) % 29)-14 for i in range(size*size*ci)]
        weights = [((i*5+11) % 23)-11 for i in range(co*ci*k*k)]
        biases = [0x7fffffff, -7, 321]+[0]*13
        for pixel in range(size*size):
            for group in range(groups):
                lanes = [image[pixel*ci+group*8+lane] & 255 if group*8+lane < ci else 0xa7 for lane in range(8)]
                client.activation(bank, pixel*groups+group, int.from_bytes(bytes(lanes), 'little'))
        for ky in range(k):
            for kx in range(k):
                for group in range(groups):
                    data = bytearray()
                    for oc in range(16):
                        for lane in range(8):
                            ic = group*8+lane
                            data.append(weights[((oc*ci+ic)*k+ky)*k+kx] & 255 if oc < co and ic < ci else 0x5a)
                    client.weight((ky*k+kx)*groups+group, data)
        client.write(SHAPE, size | (size << 8) | (ci << 16))
        client.write(OPTIONS, co | ((k == 3) << 5) | ((pad != 0) << 7) | (mode << 8) | (bank << 10))
        for oc, bias in enumerate(biases):
            client.write(BIAS+oc*4, bias)
        if mode == 0 and isinstance(client.wire, SimulatorWire):
            client.wire.drop_replies = 1  # START already executed: its retry must use the cached reply.
        client.write(CONTROL, 1)
        outputs = []
        for pixel in range(size*size):
            deadline = time.monotonic()+10
            while not client.read(STATUS) & OUTPUT_VALID:
                if client.read(ERRORS) or time.monotonic() > deadline:
                    raise RuntimeError('output error or timeout')
            if mode == 0 and pixel == 0 and isinstance(client.wire, SimulatorWire):
                # Read the full vector first, then lose only the POP response.
                class LostPop:
                    def __init__(self, original): self.original = original
                    def __call__(self, address, value, strobes=15):
                        if address == POP: client.wire.drop_replies = 1
                        return self.original(address, value, strobes)
                original = client.write
                client.write = LostPop(original)
                try:
                    output = client.output()
                finally:
                    client.write = original
            else:
                output = client.output()
            if output is None:
                raise RuntimeError('lost output vector')
            expected = []
            for oc in range(16):
                total = biases[oc] if oc < co else 0
                if oc < co:
                    for ic in range(ci):
                        for ky in range(k):
                            for kx in range(k):
                                y, x = pixel//size+ky-pad, pixel % size+kx-pad
                                if 0 <= y < size and 0 <= x < size:
                                    total += image[(y*size+x)*ci+ic]*weights[((oc*ci+ic)*k+ky)*k+kx]
                expected.append(total & 0xffffffff)
            if output['raw'] != expected or output['meta'] != ((1 << co)-1 | ((pixel % size) << 16) | ((pixel//size) << 24)):
                raise RuntimeError('scalar convolution/metadata mismatch')
            if output['flags'] != (8 | mode | (4 if pixel+1 == size*size else 0)):
                raise RuntimeError('output mode/last mismatch')
            quantized = []
            for oc, raw in enumerate(expected):
                value = raw if raw < 0x80000000 else raw-0x100000000
                product = value*(oc+1)*13
                rounded = (abs(product)+(1 << (6+oc))) >> (7+oc)
                q = min(127, max(-128, -rounded if product < 0 else rounded)) & 255
                quantized.append(0 if mode == 0 or oc >= co else table[q] if mode == 2 else q)
            if output['quantized'] != quantized:
                raise RuntimeError('requantization/LUT mismatch')
            outputs.append(output)
        status = client.read(STATUS)
        if status & (BUSY | ERROR | OUTPUT_VALID) or not status & DONE or client.read(ERRORS):
            raise RuntimeError('final device status')
        expected_mac = size*size*groups*k*k
        if client.read(PERF_MAC) != expected_mac:
            raise RuntimeError('MAC counter mismatch')
        cases.append({'mode': mode, 'kernel': k, 'cin': ci, 'bank': bank, 'outputs': outputs, 'mac_steps': expected_mac})
    return {'status': 'PASS', 'scope': 'synthetic single-tile UART convolution self-test', 'cases': cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--port')
    source.add_argument('--simulator', type=Path)
    parser.add_argument('--baud', type=int, default=115200)
    parser.add_argument('--clock-hz', type=int, choices=[27000000, 100000000], default=27000000,
                        help='simulator component clock; physical UART still uses --baud')
    parser.add_argument('--timeout', type=float, default=1)
    parser.add_argument('--bulk-weights', action='store_true', help='requires op3-capable bitstream')
    parser.add_argument('--self-test', action='store_true', help='resets the diagnostic NPU and replaces test input/weights/configuration')
    args = parser.parse_args()
    wire = SimulatorWire(args.simulator, args.clock_hz) if args.simulator else SerialWire(args.port, args.baud, args.timeout)
    try:
        client = Client(wire, bulk_weights=args.bulk_weights)
        client.sync()
        result = self_test(client) if args.self_test else {'id': hex(client.read(ID)), 'abi': hex(client.read(ABI)), 'status': client.read(STATUS)}
        print(json.dumps(result, indent=2))
    finally:
        wire.close()


if __name__ == '__main__':
    main()
