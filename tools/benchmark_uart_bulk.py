#!/usr/bin/env python3
"""Same bulk-capable bitstream: legacy versus bulk full convolutions and weight transfers."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
from npu_uart import Client, SerialWire, ID, ABI, STATUS, WEIGHT_INDEX, WEIGHT_DATA
ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class MeterWire(SerialWire):
    def __init__(self, port):
        super().__init__(port, 115200, 1)
        self.tx_bytes = self.rx_bytes = 0

    def exchange(self, frame):
        self.tx_bytes += len(frame)
        answer = super().exchange(frame)
        self.rx_bytes += len(answer)
        return answer


def main():
    p = argparse.ArgumentParser(description=__doc__+' Replaces NPU test data.')
    p.add_argument('--port', required=True)
    p.add_argument('--rtl-validation', type=Path, required=True)
    p.add_argument('--bitstream-validation', type=Path, required=True)
    p.add_argument('--jtag-validation', type=Path, required=True)
    p.add_argument('--run-directory', type=Path, required=True)
    p.add_argument('--key0-confirmed', action='store_true')
    a = p.parse_args()
    if not a.run_directory.is_dir() or not a.key0_confirmed:
        p.error('existing run directory and confirmation of this configuration KEY0 required')
    out = a.run_directory/'transfer-benchmark.json'
    if out.exists() or any((a.run_directory/(m+'-hardware.json')).exists() for m in ('legacy','bulk')):
        p.error('preserve old measurements; use fresh run paths')
    fw, bit, jtag = [json.loads(f.read_text(encoding='utf-8')) for f in
                    (a.rtl_validation,a.bitstream_validation,a.jtag_validation)]
    assert fw['status'] == bit['status'] == 'PASS' and fw['bulk_weights'] and bit['bulk_weights']
    assert bit['bitstream_configuration']['usercode'] == '12810002'
    assert jtag['bitstream_sha256'] == bit['bitstream_sha256'] and jtag['done'] and jtag['jtag_readback_verified']
    assert jtag['bitstream_validation_sha256'] == digest(a.bitstream_validation)
    assert bit['immutable_sha256']['input/rtl-validation.json'] == digest(a.rtl_validation)
    for name, expected in fw['source_sha256'].items(): assert digest(ROOT/name) == expected, name
    sources = {name:digest(ROOT/name) for name in ('tools/benchmark_uart_bulk.py','tools/npu_uart.py','tools/run_uart_board_test.py')}
    report = dict(schema='npu128-uart-bulk-throughput-v1',status='RUNNING',
        timestamp_utc=datetime.now(timezone.utc).isoformat(), port=a.port, clock_hz=100000000, baud=115200,
        source_sha256=sources, bitstream_sha256=bit['bitstream_sha256'],
        rtl_validation_sha256=digest(a.rtl_validation), bitstream_validation_sha256=digest(a.bitstream_validation),
        jtag_validation_sha256=digest(a.jtag_validation), user_confirmed_key0=True,
        full_tests={}, weight_samples=[], board_run=True, simulator_used=False,
        limitations=['Three synthetic tiles per mode, not YOLOX inference or NPU-only latency.',
            'One full run per mode, legacy first; three weight-transfer rounds alternate order.',
            'No injected faults on hardware; fault/cache coverage remains in RTL/mapped reports.'])
    try:
        for mode in ('legacy','bulk'):
            path = a.run_directory/(mode+'-hardware.json')
            log = a.run_directory/(mode+'-hardware.log')
            if log.exists(): raise RuntimeError('preserve existing log')
            command = [sys.executable,'-X','utf8',str(ROOT/'tools/run_uart_board_test.py'),
                '--port',a.port,'--rtl-validation',str(a.rtl_validation),
                '--bitstream-validation',str(a.bitstream_validation),'--jtag-validation',str(a.jtag_validation),
                '--output',str(path), *(['--bulk-weights'] if mode == 'bulk' else [])]
            print(f'Running actual {mode} scalar convolutions',flush=True)
            with log.open('w',encoding='utf-8') as sink:
                subprocess.run(command,stdout=sink,stderr=subprocess.STDOUT,text=True,timeout=180,check=True)
            r = json.loads(path.read_text(encoding='utf-8'))
            assert r['status'] == 'PASS' and r['retransmissions'] == 0 and r['bitstream_sha256'] == bit['bitstream_sha256']
            assert r['tasks'] == 3 and r['output_vectors'] == 12
            report['full_tests'][mode] = dict(report_sha256=digest(path), log_sha256=digest(log),
                elapsed_seconds=r['elapsed_seconds'],transactions=r['transactions'],retransmissions=r['retransmissions'],
                result=r['result'],raw_values_checked=r['raw_values_checked'],quantized_values_checked=r['quantized_values_checked'])
            print(f'{mode}: {r["transactions"]} transactions, {r["elapsed_seconds"]:.3f}s',flush=True)
        old, new = report['full_tests']['legacy'],report['full_tests']['bulk']
        assert old['result'] == new['result']
        assert old['transactions'] == 2759 and new['transactions'] == 1538
        for round_index in range(3):
            modes = ('legacy','bulk') if round_index % 2 == 0 else ('bulk','legacy')
            for mode in modes:
                wire = MeterWire(a.port)
                try:
                    client = Client(wire,bulk_weights=mode == 'bulk')
                    client.sync(); assert client.read(ID) == 0x4e505531 and client.read(ABI) == 0x10000
                    assert client.read(STATUS) & 3 == 0
                    count,tx,rx = client.transactions,wire.tx_bytes,wire.rx_bytes
                    started = time.perf_counter()
                    for word in range(8):
                        data = bytes((i*7+word+round_index) & 255 for i in range(128))
                        client.weight(round_index*8+word,data)
                    elapsed = time.perf_counter()-started
                    sample = dict(mode=mode,round=round_index,words=8,payload_bytes=1024,
                        elapsed_seconds=elapsed,transactions=client.transactions-count,
                        tx_bytes=wire.tx_bytes-tx,rx_bytes=wire.rx_bytes-rx,
                        payload_bytes_per_second=1024/elapsed,retransmissions=client.retries_used)
                    assert sample['transactions'] == (8 if mode == 'bulk' else 272) and client.retries_used == 0
                    assert client.read(WEIGHT_INDEX) == round_index*8+7
                    assert client.read(WEIGHT_DATA+124) == int.from_bytes(data[-4:],'little')
                    report['weight_samples'].append(sample)
                    print(f'{mode} weight round {round_index}: {sample["payload_bytes_per_second"]:.1f} payload B/s',flush=True)
                finally:
                    wire.close()
        old_rate,new_rate = [statistics.median(s['payload_bytes_per_second'] for s in report['weight_samples'] if s['mode'] == mode)
                            for mode in ('legacy','bulk')]
        report.update(status='PASS',weight_payload_speedup=new_rate/old_rate,
            legacy_weight_median_payload_bytes_per_second=old_rate,bulk_weight_median_payload_bytes_per_second=new_rate,
            full_test_speedup=old['elapsed_seconds']/new['elapsed_seconds'],
            full_test_time_reduction_percent=(1-new['elapsed_seconds']/old['elapsed_seconds'])*100,
            transaction_reduction_percent=(1-1538/2759)*100,
            legacy_full_protocol_bytes=2759*26,bulk_full_protocol_bytes=1538*26+37*128)
        report['performance_status'] = 'IMPROVED' if new_rate > old_rate and new['elapsed_seconds'] < old['elapsed_seconds'] else 'NO_IMPROVEMENT'
        print(f'PASS identical scalar outputs; full {report["full_test_speedup"]:.2f}x, weights {report["weight_payload_speedup"]:.2f}x',flush=True)
    except BaseException as exc:
        report.update(status='FAIL',reason=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        for name,expected in sources.items():
            if digest(ROOT/name) != expected: report.update(status='FAIL',reason='source changed during measurement')
        out.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')


if __name__ == '__main__':
    main()
