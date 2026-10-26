#!/usr/bin/env python3
"""Snapshot a UART physical PASS and bind bit generation to its routed database."""
import argparse
import json
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
from collect_report import ROOT, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--implementation', type=Path, required=True)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=ROOT/'config/uart-bitstream.json')
    args = parser.parse_args()
    source, target = args.project.resolve(), args.target.resolve()
    report = json.loads(args.implementation.read_text())
    config = json.loads(args.config.read_text())
    profiles = {27000000: ('npu_uart_board', 'npu128_uart', 37.037037,
                          'npu128-uart-routed-assessment-v1', '12802701'),
                100000000: ('npu_uart_board100', 'npu128_uart100', 10.0,
                           'npu128-uart100-routed-assessment-v1', '12810001')}
    top, project_name, period, schema, usercode = profiles[config['clock_hz']]
    bulk_weights = report['functional_validation'].get('bulk_weights', False)
    if bulk_weights:
        assert config['clock_hz'] == 100000000
        usercode = '12810002'
    assert config['scbv'] == 'High Voltage' and config['configuration_bank_voltage'] == 3.3
    assert config['device'] == report['device'] and config['usercode'] == usercode
    assert report['schema'] == schema and report['status'] == 'PASS'
    assert report['device'] == 'PG2L200H-6IFBB676' and report['clock_period_ns'] == period
    assert report['mapped_functional_run'] and report['unrouted_nets'] == 0
    assert not report['board_run']
    for name, expected in report['raw_evidence_sha256'].items():
        path = (source/name).resolve()
        assert path.is_relative_to(source) and digest(path) == expected, name
    for name, expected in report['functional_validation']['source_sha256'].items():
        assert digest(ROOT/name) == expected, name
    db = f'prj_tasks/pnr_1/place_route/{top}_pnr.adf'
    assert db in report['raw_evidence_sha256']
    native = subprocess.check_output(['wslpath', '-w', str(target)], text=True).strip()
    if (target.exists() or target.is_relative_to(ROOT) or ROOT.is_relative_to(target)
            or target.is_relative_to(source) or source.is_relative_to(target)
            or not PureWindowsPath(native).drive or native.startswith('\\\\')
            or not native.isascii() or any(c in native for c in '{}"\r\n')):
        parser.error('use a fresh, safe native Windows directory outside the source project and repository')
    # Preserve the accepted project in place; all PDS mutations occur in this copy.
    shutil.copytree(source, target)
    inputs = target/'bitstream-input'
    inputs.mkdir()
    shutil.copyfile(args.implementation, inputs/'implementation.json')
    shutil.copyfile(target/f'{project_name}.pds', inputs/'implemented.pds')
    shutil.copyfile(args.config, inputs/'uart-bitstream.json')
    for name in ['stage_bitstream.py', 'run_bitstream.ps1', 'GuardedJob.cs']:
        shutil.copyfile(ROOT/'tools/pango'/name, inputs/name)
    immutable = {name: digest(target/name) for name in report['raw_evidence_sha256']
        if name.endswith(('.adf', '.db', '.vm'))}
    original_manifest = json.loads((source/'manifest.json').read_text())
    script = target/'generate-bitstream.tcl'
    script.write_text('\n'.join([
        '# Generate only the accepted routed DB, without dependency reruns.',
        'set_arch -family Logos2 -device PG2L200H -package FBB676 -speedgrade -6',
        'set_option max_threads 1',
        *(f'add_design {{{name}}}' for name in original_manifest['rtl']),
        'add_constraint {board.fdc}',
        f'set_option -options {{top_module {{{top}}} verilog_standard {{SystemVerilog}} loop_limit {{65536}}}} [get_filesets design_1]',
        f'gen_bit_stream -force_to_run -input_db_file {{{db}}} -scbv {{{config["scbv"]}}} -usercode {config["usercode"]}'])+'\n')
    immutable.update({name: digest(target/name) for name in original_manifest['files']})
    immutable.update({str(p.relative_to(target)): digest(p) for p in inputs.iterdir()})
    immutable[script.name] = digest(script)
    manifest = {'schema': 'npu128-pango-bitstream-v1', 'device': report['device'],
        'top': top, 'clock_hz': config['clock_hz'], 'project_name': project_name, 'action': 'gen_bit_stream',
        'bulk_weights': bulk_weights,
        'repository_config': str(args.config.resolve().relative_to(ROOT)),
        'repository_implementation': str(args.implementation.resolve().relative_to(ROOT)),
        'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'source_project': str(source), 'project': native, 'routed_database': db,
        'routed_database_sha256': digest(target/db), 'immutable_sha256': immutable,
        'implementation_sha256': digest(args.implementation),
        'bitstream_configuration': config,
        'scope': f'{config["clock_hz"]//1000000} MHz UART diagnostic bitstream only; no hardware, flash, eFuse or key operations'}
    (target/'bitstream-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(f'PASS staged exact routed database for bitstream: {native}')


if __name__ == '__main__':
    main()
