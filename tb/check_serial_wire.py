"""Physical-port framing and safe retry using a timed serial-port model."""
import struct
import sys
from pathlib import Path
import types
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
import npu_uart as uart


def response(sequence, value=0x4e505531, status=0):
    body = struct.pack('<BBBBI', 1, sequence, status, 0, value)
    return b'\x5a\xa5'+body+struct.pack('<H', uart.crc16(body))


class TimedPort:
    """Read(n) completes on n bytes; a shorter reply consumes its read timeout."""
    def __init__(self, plans, fragment=None, short_write=False):
        self.plans = iter(plans)
        self.fragment, self.short_write = fragment, short_write
        self.stream = bytearray()
        self.now = 0.0
        self.timeout = 0.02
        self.writes, self.read_sizes = [], []
        self.closed = False
        self.resets = 0

    def reset_input_buffer(self):
        self.resets += 1
        self.stream.clear()

    def write(self, data):
        self.writes.append(bytes(data))
        self.stream.extend(next(self.plans))
        return len(data)-int(self.short_write)

    def flush(self):
        pass

    def read(self, count):
        self.read_sizes.append(count)
        length = min(count, len(self.stream))
        if self.fragment:
            length = min(length, self.fragment)
        data = bytes(self.stream[:length])
        del self.stream[:length]
        self.now += 0.0001*length if length == count else self.timeout
        return data

    def close(self):
        self.closed = True


class SerialTests(unittest.TestCase):
    def wire(self, plans, **kwargs):
        port = TimedPort(plans, **kwargs)
        module = types.SimpleNamespace(Serial=lambda *a, **k: port)
        serial_patch = patch.dict(sys.modules, {'serial': module})
        time_patch = patch.object(uart.time, 'monotonic', side_effect=lambda: port.now)
        serial_patch.start(); time_patch.start()
        self.addCleanup(serial_patch.stop); self.addCleanup(time_patch.stop)
        wire = uart.SerialWire('TEST', 115200, 0.2)
        self.addCleanup(wire.close)
        return wire, port

    def test_complete_frame_returns_before_idle_timeout(self):
        wire, port = self.wire([response(7)])
        self.assertEqual(wire.exchange(uart.request(7, 0)), response(7))
        self.assertLess(port.now, port.timeout)

    def test_split_frame(self):
        wire, port = self.wire([response(8)], fragment=3)
        self.assertEqual(uart.reply(wire.exchange(uart.request(8, 0)), 8), (0, 0x4e505531))
        self.assertEqual(port.resets, 1)

    def test_noise_and_header_split(self):
        wire, _ = self.wire([b'\x00\x5a\x00\xa5\x5a'+response(9)], fragment=5)
        self.assertEqual(wire.exchange(uart.request(9, 0)), response(9))

    def test_corrupt_and_stale_frame_then_good_reply(self):
        corrupt = bytearray(response(10)); corrupt[-1] ^= 1
        wire, _ = self.wire([bytes(corrupt)+response(11)+response(10)])
        self.assertEqual(wire.exchange(uart.request(10, 0)), response(10))

    def test_lost_start_reply_retries_identical_request(self):
        wire, port = self.wire([b'', response(0, value=0)])
        client = uart.Client(wire)
        client.write(uart.CONTROL, 1)
        self.assertEqual(port.writes, [uart.request(0, 1, uart.CONTROL, 1, 15)]*2)
        self.assertEqual((client.transactions, client.retries_used, client.sequence), (1, 1, 1))
        self.assertEqual(port.resets, 1)

    def test_corrupt_pop_reply_retries_identical_request(self):
        corrupt = bytearray(response(0, value=0)); corrupt[-1] ^= 1
        wire, port = self.wire([bytes(corrupt), response(0, value=0)])
        client = uart.Client(wire)
        client.write(uart.POP, 1)
        self.assertEqual(port.writes[0], port.writes[1])
        self.assertEqual(client.retries_used, 1)

    def test_exhausted_timeout_keeps_sequence_and_payload(self):
        wire, port = self.wire([b'']*3)
        client = uart.Client(wire)
        with self.assertRaises(TimeoutError):
            client.write(uart.CONTROL, 1)
        self.assertEqual(len(port.writes), 3)
        self.assertEqual(len(set(port.writes)), 1)
        self.assertEqual((client.sequence, client.transactions), (0, 0))

    def test_mmio_error_is_not_reexecuted(self):
        wire, port = self.wire([response(0, value=0, status=1)])
        client = uart.Client(wire)
        with self.assertRaisesRegex(RuntimeError, 'UART status 1'):
            client.write(uart.CONTROL, 1)
        self.assertEqual((len(port.writes), client.sequence, client.retries_used), (1, 1, 0))

    def test_bulk_lost_reply_retries_complete_payload(self):
        wire, port = self.wire([b'', response(0, value=34)])
        client = uart.Client(wire, bulk_weights=True)
        payload = bytes(range(128))
        client.weight(863, payload)
        expected = uart.weight_request(0, 863, payload)
        self.assertEqual(port.writes, [expected, expected])
        self.assertEqual(len(expected), 142)
        self.assertEqual((client.transactions, client.retries_used), (1, 1))

    def test_bulk_rejects_bad_shape_before_io(self):
        wire, port = self.wire([])
        client = uart.Client(wire, bulk_weights=True)
        for index, payload in [(864, bytes(128)), (0, bytes(127))]:
            with self.assertRaises(ValueError):
                client.weight(index, payload)
        self.assertEqual(port.writes, [])

    def test_short_write_and_close(self):
        wire, port = self.wire([b''], short_write=True)
        with self.assertRaises(OSError):
            wire.exchange(uart.request(0, 0))
        wire.close()
        self.assertTrue(port.closed)


if __name__ == '__main__':
    unittest.main()
