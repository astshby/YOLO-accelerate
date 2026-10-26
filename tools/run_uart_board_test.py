#!/usr/bin/env python3
"""Run scalar-checked convolutions on a specifically identified CP2102 UART."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import time
from npu_uart import Client, SerialWire, self_test, ID, ABI, STATUS

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__+' Resets the diagnostic NPU and replaces its test data.')
    parser.add_argument('--port', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=1)
    parser.add_argument('--rtl-validation', type=Path, default=ROOT/'reports/npu128-uart-validation.json')
    parser.add_argument('--jtag-validation', type=Path, default=ROOT/'reports/npu128-uart-board.json')
    parser.add_argument('--bitstream-validation', type=Path, default=ROOT/'reports/npu128-uart-bitstream.json')
    parser.add_argument('--bulk-weights', action='store_true')
    parser.add_argument('--host-validation', type=Path, help='current host-only verification when the client changed')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('preserve previous evidence; choose a new output file')
    import serial
    from serial.tools.list_ports import comports
    matches = [p for p in comports() if p.device.casefold() == args.port.casefold()]
    if len(matches) != 1 or (matches[0].vid, matches[0].pid) != (0x10c4, 0xea60):
        parser.error('port must be the present CP2102 VID:PID=10c4:ea60, not the FTDI JTAG or Bluetooth port')
    functional_path, jtag_path, bit_path = args.rtl_validation, args.jtag_validation, args.bitstream_validation
    functional, jtag, bit = [json.loads(p.read_text(encoding='utf-8')) for p in (functional_path, jtag_path, bit_path)]
    assert functional['status'] == bit['status'] == 'PASS'
    assert jtag['jtag_readback_verified'] and jtag['done'] and jtag['configuration_error_bits'] == 0
    assert jtag['bitstream_sha256'] == bit['bitstream_sha256']
    assert jtag['bitstream_validation_sha256'] == digest(bit_path)
    assert functional['default_parameters']['CLOCK_HZ'] == bit['clock_hz'] == jtag['clock_hz']
    assert bit['immutable_sha256']['input/rtl-validation.json'] == digest(functional_path)
    if args.bulk_weights:
        assert functional.get('bulk_weights') is True
        assert bit['bitstream_configuration']['usercode'] == '12810002'
    host = None
    if args.host_validation:
        host = json.loads(args.host_validation.read_text(encoding='utf-8'))
        assert host['schema'] == 'npu128-uart-host-functional-v1' and host['status'] == 'PASS'
        assert host['baseline_client_sha256'] == functional['source_sha256']['tools/npu_uart.py']
        for name, expected in host['source_sha256'].items():
            assert digest(ROOT/name) == expected, name
    for name, expected in functional['source_sha256'].items():
        if digest(ROOT/name) != expected:
            assert name == 'tools/npu_uart.py' and host is not None, name
            assert digest(ROOT/name) == host['source_sha256'][name], name
    source_hashes = {'tools/npu_uart.py': digest(ROOT/'tools/npu_uart.py'),
                     'tools/run_uart_board_test.py': digest(Path(__file__))}
    report = {'schema': 'npu128-uart-hardware-functional-v1', 'status': 'RUNNING',
        'scope': 'real CP2102 115200 8N1 UART, three synthetic scalar-checked convolution tasks',
        'timestamp_utc': datetime.now(timezone.utc).isoformat(), 'port': matches[0].device,
        'usb_vid': '10c4', 'usb_pid': 'ea60', 'pyserial': serial.__version__,
        'python': platform.python_version(), 'host': platform.platform(),
        'bulk_weights': args.bulk_weights, 'baud': 115200, 'clock_hz': bit['clock_hz'], 'timeout_seconds': args.timeout, 'source_sha256': source_hashes,
        'rtl_validation_sha256': digest(functional_path), 'jtag_reference_sha256': digest(jtag_path),
        'host_validation_sha256': digest(args.host_validation) if host else None,
        'bitstream_validation_sha256': digest(bit_path), 'bitstream_sha256': bit['bitstream_sha256'],
        'board_run': True, 'simulator_used': False,
        'limitations': ['Three synthetic tiles, not full YOLOX inference or accuracy validation.',
            'Elapsed time includes serial/USB/host overhead and is not NPU compute latency.',
            'JTAG reference is from the earlier verified configuration; this UART run reads NPU ID/ABI without a fresh JTAG scan.',
            'No injected lost replies on hardware; protocol fault/retry coverage is in the separate RTL report.']}
    wire, client = None, None
    start = time.perf_counter()
    try:
        wire = SerialWire(args.port, 115200, args.timeout)
        client = Client(wire, bulk_weights=args.bulk_weights)
        client.sync()
        identity = {'id': client.read(ID), 'abi': client.read(ABI), 'status_before': client.read(STATUS)}
        assert identity['id'] == 0x4e505531 and identity['abi'] == 0x10000
        report['identity'] = identity
        print(f'Identity PASS on {args.port}; starting RAW/quantized/LUT convolutions.', flush=True)
        result = self_test(client)
        assert result['status'] == 'PASS' and len(result['cases']) == 3
        vectors = sum(len(c['outputs']) for c in result['cases'])
        assert vectors == 12
        report.update(status='PASS', result=result, tasks=3, output_vectors=vectors,
            raw_values_checked=vectors*16, quantized_values_checked=vectors*16)
        print(f'PASS actual board: 3 convolution tasks, {vectors} vectors, {vectors*32} RAW/INT8 values.', flush=True)
    except BaseException as exc:
        report.update(status='FAIL', reason=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        if wire is not None:
            wire.close()
        report['elapsed_seconds'] = time.perf_counter()-start
        if client is not None:
            report.update(transactions=client.transactions, retransmissions=client.retries_used)
        for name, expected in source_hashes.items():
            if digest(ROOT/name) != expected:
                report.update(status='FAIL', reason='client/test source changed during execution')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    if report['status'] != 'PASS':
        raise RuntimeError(report.get('reason', 'hardware verification failed'))


if __name__ == '__main__':
    main()
