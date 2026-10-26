#!/usr/bin/env python3
"""Public-pin board test using the real host codec and physical default divider."""
import argparse
import json
from pathlib import Path
import struct
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from npu_uart import (Client, SimulatorWire, request, reply, crc16, self_test,
                      ID, ABI, STATUS, ERRORS, ACT_DATA, CONTROL, PERF_MAC, WEIGHT_DATA, WEIGHT_INDEX, weight_request)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--simulator', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--clock-hz', type=int, choices=[27000000, 100000000], default=27000000)
    parser.add_argument('--bulk-weights', action='store_true')
    args = parser.parse_args()
    assert crc16(b'123456789') == 0x29b1
    wire = SimulatorWire(args.simulator, args.clock_hz)
    client = Client(wire, bulk_weights=args.bulk_weights)
    negatives = 0
    try:
        client.sync(0x1234a55a)
        assert client.read(ID) == 0x4e505531 and client.read(ABI) == 0x10000
        client.write(ACT_DATA, 0x11223344)
        client.write(ACT_DATA, 0xaabbccdd, 5)
        assert client.read(ACT_DATA) == 0x11bb33dd
        last = (client.sequence-1) & 255
        assert reply(wire.exchange(request(last, 0, ACT_DATA)), last) == (0, 0x11bb33dd)
        assert reply(wire.exchange(request(last, 1, ACT_DATA, 0, 15)), last)[0] == 4
        negatives += 1
        assert client.read(ACT_DATA) == 0x11bb33dd
        bad = bytearray(request(client.sequence, 1, ACT_DATA, 0xdeadbeef, 15)); bad[8] ^= 1
        assert reply(wire.exchange(bad), client.sequence)[0] == 3
        negatives += 1
        for frame in (request(client.sequence, 9), request(client.sequence, 1, ACT_DATA, 0, 128),
                      request(client.sequence, 0, ACT_DATA, 1), request(client.sequence, 2, 4, 1)):
            assert reply(wire.exchange(frame), client.sequence)[0] == 2
            negatives += 1
        frame = bytearray(request(client.sequence, 1, ACT_DATA, 0, 15)); frame[2] = 2
        frame[-2:] = struct.pack('<H', crc16(frame[2:12]))
        assert reply(wire.exchange(frame), client.sequence)[0] == 2
        negatives += 1
        assert client.read(ACT_DATA) == 0x11bb33dd and client.read(ERRORS) == 0
        for frame, stop in ((request(client.sequence, 1, ACT_DATA, 0x33333333, 15)[:7], -1),
                            (request(client.sequence, 1, ACT_DATA, 0x33333333, 15), 3)):
            try:
                wire.exchange(frame, bad_stop=stop)
                raise AssertionError('partial/framing-error request received a reply')
            except TimeoutError:
                negatives += 1
            assert client.read(ACT_DATA) == 0x11bb33dd
        deviation = (wire.divisor*17+500)//1000
        for delta in (-deviation, deviation):
            assert reply(wire.exchange(request(client.sequence, 0, ID), delta=delta), client.sequence)[1] == 0x4e505531
            assert client.read(ID) == 0x4e505531
        wire.command('G')
        wire.corrupt_replies = 1
        assert client.read(ID) == 0x4e505531
        try:
            client.read(0xffff)
            raise AssertionError('invalid MMIO read accepted')
        except RuntimeError as exc:
            assert 'UART status 1' in str(exc)
        assert client.read(ERRORS) == 1
        client.write(ERRORS, 31)
        bulk_negatives = 0
        if args.bulk_weights:
            data = bytes((i*7+19) & 255 for i in range(128))
            # Legacy loading remains compatible with the new bridge.
            legacy = Client(wire); legacy.sequence = client.sequence
            legacy.weight(0, data); client.sequence = legacy.sequence
            assert client.read(WEIGHT_DATA) == int.from_bytes(data[:4], 'little')
            wire.drop_replies = 1
            client.weight(863, data)
            last = (client.sequence-1) & 255
            full = weight_request(last, 863, data)
            assert reply(wire.exchange(full), last) == (0, 34)
            changed = bytearray(full); changed[12] ^= 1
            changed[-2:] = struct.pack('<H', crc16(changed[2:-2]))
            assert reply(wire.exchange(changed), last)[0] == 4
            bulk_negatives += 1
            # Same header AND same CRC16 must still compare all 128 payload bytes.
            collision = bytearray(full); collision[12] ^= 1
            target = crc16(full[2:-2]); prefix_crc = crc16(collision[2:-4])
            import binascii
            for suffix in range(65536):
                tail = struct.pack('<H', suffix)
                if binascii.crc_hqx(tail, prefix_crc) == target:
                    collision[-4:-2] = tail
                    break
            else:
                raise AssertionError('CRC collision fixture missing')
            assert collision != full and crc16(collision[2:-2]) == target
            assert reply(wire.exchange(collision), last)[0] == 4
            bulk_negatives += 1
            bad = bytearray(weight_request(client.sequence, 0, bytes(128))); bad[80] ^= 1
            assert reply(wire.exchange(bad), client.sequence)[0] == 3
            assert reply(wire.exchange(full), last) == (0, 34)
            bulk_negatives += 1
            for offset, value in [(2, 2), (5, 1), (6, 4), (8, 0x60)]:
                malformed = bytearray(weight_request(client.sequence, 863, data))
                malformed[offset] = value
                if offset == 8: malformed[9] = 3  # index 864
                malformed[-2:] = struct.pack('<H', crc16(malformed[2:-2]))
                assert reply(wire.exchange(malformed), client.sequence)[0] == 2
                bulk_negatives += 1
            assert client.read(WEIGHT_INDEX) == 863
            assert client.read(WEIGHT_DATA+124) == int.from_bytes(data[-4:], 'little')
            for truncated, stop in [(weight_request(client.sequence, 0, bytes(128))[:70], -1),
                                    (weight_request(client.sequence, 0, bytes(128)), 32)]:
                try:
                    wire.exchange(truncated, bad_stop=stop)
                    raise AssertionError('partial/framing-error bulk received a reply')
                except TimeoutError:
                    bulk_negatives += 1
                assert client.read(WEIGHT_INDEX) == 863
                assert client.read(WEIGHT_DATA+124) == int.from_bytes(data[-4:], 'little')
        result = self_test(client)
        assert client.transactions > 256 and client.retries_used == (4 if args.bulk_weights else 3)
        if args.bulk_weights:
            # The output slot holds this task active while the long bulk frame arrives.
            client.write(CONTROL, 1)
            poisoned = bytes([0x5a])*128
            try:
                client.weight(0, poisoned)
                raise AssertionError('busy weight commit accepted')
            except RuntimeError as exc:
                assert 'UART status 1' in str(exc)
            failed_seq = (client.sequence-1) & 255
            assert reply(wire.exchange(weight_request(failed_seq, 0, poisoned)), failed_seq) == (1, 33)
            assert client.read(ERRORS) == 2
            drained = []
            for expected in result['cases'][-1]['outputs']:
                output = client.output()
                assert output == expected, 'busy bulk changed live weight RAM'
                drained.append(output)
            client.write(ERRORS, 31)
            assert client.read(PERF_MAC) == 72
            result['bulk_busy_recovery_outputs'] = drained
            bulk_negatives += 1
        result.update(bulk_weights=args.bulk_weights, bulk_negative_cases=bulk_negatives,
                      bulk_completed_writes=34 if args.bulk_weights else None)
        client.write(CONTROL, 1)  # Abort a real running command via the external KEY0 path.
        assert wire.command('R') == 'OK'
        client.sync(0xa5a55a5a)
        assert client.read(STATUS) == 0 and client.read(ERRORS) == 0 and client.read(PERF_MAC) == 0
        frames, cycles = wire.frames, wire.close()
        result.update(protocol_negative_cases=negatives, client_transactions=client.transactions,
            retransmissions=client.retries_used, wire_frames=frames, simulated_clocks=cycles,
            uart_clocks_per_bit=wire.divisor, interbyte_timeout_clocks=wire.interbyte_timeout,
            tested_receive_divisors=[wire.divisor-deviation, wire.divisor, wire.divisor+deviation], external_reset_recovery=True)
        args.output.write_text(json.dumps(result, indent=2)+'\n')
        print(f'PASS npu_uart_board: {len(result["cases"])} scalar-checked convolution tasks, '
              f'{client.transactions} host transactions, {negatives} protocol rejection/recovery cases, '
              f'{client.retries_used} safe retransmissions, {cycles} real-divisor clocks')
    finally:
        if wire.process.poll() is None:
            wire.process.terminate()
            wire.process.wait(timeout=10)


if __name__ == '__main__':
    main()
