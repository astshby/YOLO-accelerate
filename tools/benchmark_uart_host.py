#!/usr/bin/env python3
"""Compare original and optimized serial receivers on the same verified FPGA bitstream."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import statistics
import time
import npu_uart as current
from run_uart_host_tests import ROOT, digest, unchanged_logic


class MeasuredPort:
    def __init__(self, port):
        self.port = port
        self.tx_bytes = self.rx_bytes = 0
        self.read_sizes = Counter()
        self.operations = Counter()
        self.addresses = Counter()

    def __getattr__(self, name):
        return getattr(self.port, name)

    def write(self, frame):
        count = self.port.write(frame)
        self.tx_bytes += count
        if len(frame) == 14:
            self.operations[str(frame[4])] += 1
            self.addresses[f'{frame[4]}:0x{int.from_bytes(frame[6:8], "little"):04x}'] += 1
        return count

    def read(self, count):
        self.read_sizes[str(count)] += 1
        data = self.port.read(count)
        self.rx_bytes += len(data)
        return data

    def snapshot(self):
        return dict(tx_bytes=self.tx_bytes, rx_bytes=self.rx_bytes,
            read_sizes=dict(self.read_sizes), operations=dict(self.operations), addresses=dict(self.addresses))


def main():
    parser = argparse.ArgumentParser(description=__doc__+' The baseline convolution resets the NPU and replaces test data.')
    parser.add_argument('--port', required=True)
    parser.add_argument('--baseline-client', type=Path, required=True)
    parser.add_argument('--host-validation', type=Path, required=True)
    parser.add_argument('--candidate-validation', type=Path, required=True)
    parser.add_argument('--program-receipt', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=64)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--key0-confirmed', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or not 16 <= args.samples <= 512 or not 2 <= args.rounds <= 8:
        parser.error('use a fresh output; samples 16..512, rounds 2..8')
    if not args.key0_confirmed:
        parser.error('KEY0 must be confirmed after this configuration')
    from serial.tools.list_ports import comports
    ports = [p for p in comports() if p.device.casefold() == args.port.casefold()]
    assert len(ports) == 1 and (ports[0].vid, ports[0].pid) == (0x10c4, 0xea60)
    firmware_path = ROOT/'reports/npu128-uart100-functional.json'
    bit_path = ROOT/'reports/npu128-uart100-bitstream.json'
    firmware, bit, host, candidate, receipt = [json.loads(p.read_text(encoding='utf-8')) for p in
        (firmware_path, bit_path, args.host_validation, args.candidate_validation, args.program_receipt)]
    assert all(r['status'] == 'PASS' for r in (firmware, bit, host, candidate, receipt))
    assert host['schema'] == 'npu128-uart-host-functional-v1'
    assert digest(args.baseline_client) == host['baseline_client_sha256'] == firmware['source_sha256']['tools/npu_uart.py']
    assert digest(firmware_path) == host['firmware_validation_sha256'] == candidate['rtl_validation_sha256']
    assert digest(bit_path) == candidate['bitstream_validation_sha256'] == receipt['validation_sha256']
    assert digest(args.host_validation) == candidate['host_validation_sha256']
    assert candidate['clock_hz'] == bit['clock_hz'] == 100000000
    assert candidate['bitstream_sha256'] == receipt['bitstream_sha256'] == bit['bitstream_sha256']
    assert candidate['board_run'] and not candidate['simulator_used']
    assert candidate['retransmissions'] == 0 and candidate['tasks'] == 3 and candidate['output_vectors'] == 12
    for name, expected in host['source_sha256'].items(): assert digest(ROOT/name) == expected, name
    for name, expected in candidate['source_sha256'].items(): assert digest(ROOT/name) == expected, name
    for name, expected in firmware['source_sha256'].items():
        if name != 'tools/npu_uart.py': assert digest(ROOT/name) == expected, name
    # ast.dump gains fields across Python versions (WSL 3.10, native 3.12).
    # Compare baseline/candidate within this interpreter; bind both raw sources
    # above rather than incorrectly requiring cross-version AST dump equality.
    runtime_logic_hash = unchanged_logic(args.baseline_client.read_text(), (ROOT/'tools/npu_uart.py').read_text())
    log_path = args.program_receipt.parent/'program.log'
    log = log_path.read_text(errors='replace')
    assert 'UART_DIAG_USERCODE=12810001' in log and 'UART_DIAG_JTAG_CHECKS_PASS' in log
    assert re.search(r'UART_DIAG_VERIFY_RETURN=1\s', log)
    spec = importlib.util.spec_from_file_location('uart_host_baseline', args.baseline_client)
    baseline = importlib.util.module_from_spec(spec); spec.loader.exec_module(baseline)
    sources = {name:digest(ROOT/name) for name in ('tools/npu_uart.py', 'tools/benchmark_uart_host.py',
        'tools/run_uart_host_tests.py', 'tools/run_uart_board_test.py')}
    report = dict(schema='npu128-uart-host-throughput-v1', status='RUNNING',
        timestamp_utc=datetime.now(timezone.utc).isoformat(), port=ports[0].device, baud=115200,
        clock_hz=100000000, source_sha256=sources, baseline_client_sha256=digest(args.baseline_client),
        benchmark_clock='perf_counter', benchmark_clock_resolution_seconds=time.get_clock_info('perf_counter').resolution,
        host_validation_sha256=digest(args.host_validation), firmware_validation_sha256=digest(firmware_path),
        runtime_unchanged_logic_ast_sha256=runtime_logic_hash,
        bitstream_validation_sha256=digest(bit_path), bitstream_sha256=bit['bitstream_sha256'],
        candidate_validation_sha256=digest(args.candidate_validation),
        fresh_jtag_receipt_sha256=digest(args.program_receipt), fresh_jtag_log_sha256=digest(log_path),
        user_confirmed_key0=True, microbenchmarks=[], board_run=True, firmware_changed=False,
        limitations=['Synthetic tiles and ID reads, not YOLOX inference or NPU-only compute throughput.',
            'Elapsed time includes USB/serial/host overhead; no new baud or bulk protocol.',
            'Repeated microbenchmarks alternate order; one full self-test per client, candidate before baseline.',
            'Baseline and candidate share codec, scalar reference and Client by AST comparison; only receive method differs.'])

    def open_client(module):
        wire = module.SerialWire(args.port, 115200, 1)
        wire.port = MeasuredPort(wire.port)
        return wire, module.Client(wire)

    try:
        for round_index in range(args.rounds):
            order = [('baseline',baseline),('optimized',current)]
            if round_index % 2: order.reverse()
            for label, module in order:
                wire, client = open_client(module)
                try:
                    client.sync(0x484f5354)
                    assert client.read(module.ID) == 0x4e505531 and client.read(module.ABI) == 0x10000
                    before = wire.port.snapshot(); retries = client.retries_used
                    latencies = []
                    started = time.perf_counter()
                    for _ in range(args.samples):
                        tick = time.perf_counter()
                        assert client.read(module.ID) == 0x4e505531
                        latencies.append(time.perf_counter()-tick)
                    elapsed = time.perf_counter()-started
                    after = wire.port.snapshot()
                    sample = dict(client=label, round=round_index, samples=args.samples,
                        elapsed_seconds=elapsed, transactions_per_second=args.samples/elapsed,
                        median_transaction_ms=statistics.median(latencies)*1000,
                        p95_transaction_ms=sorted(latencies)[int((len(latencies)-1)*0.95)]*1000,
                        retransmissions=client.retries_used-retries,
                        tx_bytes=after['tx_bytes']-before['tx_bytes'], rx_bytes=after['rx_bytes']-before['rx_bytes'],
                        read_sizes=dict(Counter(after['read_sizes'])-Counter(before['read_sizes'])))
                    assert sample['retransmissions'] == 0
                    report['microbenchmarks'].append(sample)
                    print(f'{label} round {round_index}: {sample["transactions_per_second"]:.2f} txn/s, '
                          f'{sample["median_transaction_ms"]:.3f} ms median', flush=True)
                finally:
                    wire.close()
        started = time.perf_counter()
        wire, client = open_client(baseline)
        try:
            client.sync()
            assert client.read(baseline.ID) == 0x4e505531 and client.read(baseline.ABI) == 0x10000
            client.read(baseline.STATUS)
            result = baseline.self_test(client)
            elapsed = time.perf_counter()-started
            assert result == candidate['result'], 'full scalar outputs differ between clients'
            assert client.transactions == candidate['transactions'] and client.retries_used == 0
            report['baseline_full_self_test'] = dict(status='PASS', result=result, elapsed_seconds=elapsed,
                transactions=client.transactions, retransmissions=client.retries_used, io=wire.port.snapshot())
        finally:
            wire.close()
        old_rate, new_rate = [statistics.median(r['transactions_per_second'] for r in report['microbenchmarks']
            if r['client'] == label) for label in ('baseline','optimized')]
        report.update(status='PASS', baseline_median_transactions_per_second=old_rate,
            optimized_median_transactions_per_second=new_rate, microbenchmark_speedup=new_rate/old_rate,
            candidate_full_self_test_seconds=candidate['elapsed_seconds'],
            full_self_test_speedup=elapsed/candidate['elapsed_seconds'],
            full_self_test_time_reduction_percent=(1-candidate['elapsed_seconds']/elapsed)*100,
            full_self_test_transactions=client.transactions, full_self_test_wire_bytes=client.transactions*26,
            wire_time_lower_bound_seconds=client.transactions*26*10/115200)
        report['performance_status'] = 'IMPROVED' if new_rate > old_rate and elapsed > candidate['elapsed_seconds'] else 'NO_IMPROVEMENT'
        print(f'PASS same-bitstream scalar outputs; full self-test {elapsed:.3f} -> '
              f'{candidate["elapsed_seconds"]:.3f}s ({report["full_self_test_speedup"]:.2f}x)', flush=True)
    except BaseException as exc:
        report.update(status='FAIL', reason=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        for name, expected in sources.items():
            if digest(ROOT/name) != expected: report.update(status='FAIL', reason='source changed during benchmark')
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')


if __name__ == '__main__':
    main()
