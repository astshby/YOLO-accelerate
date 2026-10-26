#!/usr/bin/env python3
"""Collect real JTAG evidence without claiming an unavailable UART test."""
import argparse
import json
from pathlib import Path
import re
from collect_report import ROOT, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-directory', type=Path, required=True)
    parser.add_argument('--bitstream-validation', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--user-confirmed-key0', action='store_true')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('preserve existing report; use a fresh output')
    p = args.run_directory.resolve()
    proof = json.loads(args.bitstream_validation.read_text())
    assert proof['schema'] == 'npu128-uart-bitstream-assessment-v1' and proof['status'] == 'PASS'
    clock_hz = proof['clock_hz']
    assert clock_hz in (27000000, 100000000)
    expected_code = '12810001' if clock_hz == 100000000 else '12802701'
    if proof.get('bulk_weights'):
        assert clock_hz == 100000000
        expected_code = '12810002'
    receipt_path = p/'program-a/program-receipt.json'
    receipt = json.loads(receipt_path.read_text())
    assert receipt['schema'] == 'npu128-uart-jtag-v1' and receipt['status'] == 'PASS'
    assert receipt['bitstream_sha256'] == proof['bitstream_sha256']
    assert receipt['validation_sha256'] == digest(args.bitstream_validation) == digest(p/'program-a/bitstream-validation.json')
    assert digest(p/'program_uart_board.ps1') == digest(ROOT/'tools/pango/program_uart_board.ps1')
    assert digest(p/'GuardedJob.cs') == digest(ROOT/'tools/pango/GuardedJob.cs')
    log = (p/'program-a/program.log').read_text(errors='replace')
    assert re.search(r'(?m)^PG2L200H\s*$', log)
    assert re.search(r'The ID value is: 0x00603899', log)
    for name in ('PROGRAM', 'VERIFY'):
        assert re.search(rf'(?m)^UART_DIAG_{name}_RETURN=1\s*$', log)
    code = re.search(r'(?m)^UART_DIAG_USERCODE=([0-9a-fA-F]+)\s*$', log)[1]
    status = int(re.search(r'(?m)^UART_DIAG_CONFIG_STATUS=([0-9a-fA-F]+)\s*$', log)[1], 16)
    assert code == expected_code == proof['bitstream_configuration']['usercode']
    assert status & 0x1000 and not status & 0x1800001f
    assert 'UART_DIAG_JTAG_CHECKS_PASS' in log and not re.search(r'(?m)^E:', log)
    script = (p/'program-a/program.tcl').read_text()
    assert 'cfg_set_device_property -device_index 0 -program 1' in script
    assert not re.search(r'\bcfg_(?:flash|efuse|program_key|program_user_efuse)', script)
    ports = json.loads((p/'serial-inventory.json').read_text())
    assert ports['serial_port_opened'] is False
    attempts = []
    root = p.parent
    for suffix, disposition in [('a', 'REJECTED_ARGUMENTS'), ('b', 'ABORTED_UNEXPECTED_RERUN'),
                                 ('c', 'REJECTED_UNSET_SCBV'), ('d', 'ACCEPTED')]:
        folder = root/f'npu128-uart-bitstream-20261024{suffix}'
        evidence = [folder/name for name in ('bitstream.log', 'bitstream-guard.json', 'bitstream-aborted.json')
                    if (folder/name).is_file()]
        if clock_hz == 27000000:
            attempts.append({'project': folder.name, 'disposition': disposition,
                             'evidence_sha256': {f.name: digest(f) for f in evidence}})
    raw = [p/name for name in ['scan.tcl', 'scan.log', 'scan-return.tcl', 'scan-return.log',
        'program_uart_board.ps1', 'GuardedJob.cs', 'program-a/program.tcl', 'program-a/program.log',
        'program-a/program-receipt.json', 'program-a/bitstream-validation.json', 'serial-inventory.json',
        'jtag-server.stdout.log', 'jtag-server.stderr.log']]
    raw += sorted(p.glob('jtag-server*.log')) + sorted(p.glob('jtag-server*process.json'))
    report = {'schema': 'npu128-uart-board-assessment-v1', 'status': 'JTAG_PASS_UART_PENDING',
        'scope': 'actual board volatile JTAG programming/readback only; UART convolution not run',
        'device': 'PG2L200H', 'idcode': '0x00603899', 'clock_hz': clock_hz,
        'bulk_weights': proof.get('bulk_weights', False),
        'bitstream_validation_sha256': digest(args.bitstream_validation),
        'bitstream_sha256': proof['bitstream_sha256'], 'usercode': '0x'+code,
        'config_status': f'0x{status:08x}', 'done': True, 'configuration_error_mask': '0x1800001f',
        'configuration_error_bits': status & 0x1800001f, 'jtag_readback_verified': True,
        'jtag_clock_hz': 5000000, 'jtag_receipt': receipt,
        'user_reported_key0_pressed': args.user_confirmed_key0,
        'uart': {'run': False, 'reason': 'UART functional testing is collected separately; this report proves only JTAG configuration.',
                 'preflight': ports},
        'flash_written': False, 'efuse_or_keys_written': False, 'bitstream_attempts': attempts,
        'source_sha256': {name: digest(ROOT/name) for name in ['tools/pango/program_uart_board.ps1',
            'tools/pango/scan_uart_board.tcl', 'tools/pango/collect_uart_board.py', 'tools/npu_uart.py']},
        'raw_evidence_sha256': {str(f.relative_to(p)): digest(f) for f in raw if f.is_file()},
        'limitations': ['DONE, usercode and readback validate FPGA configuration, not convolution behavior.',
            'No physical UART ID/ABI or numerical test. Existing pin-level RTL and byte-level mapped tests remain separate.',
            'Configuration is volatile; power cycling requires programming again.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n')
    print(f'{report["status"]}: ID=0x00603899, usercode=0x{code}, status=0x{status:08x}')


if __name__ == '__main__':
    main()
