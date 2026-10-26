#!/usr/bin/env python3
"""Accept a clean UART bitstream only when it uses the exact routed PASS DB."""
import argparse
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from collect_report import ROOT, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('preserve existing report; use a fresh output')
    p = args.project.resolve()
    m = json.loads((p/'bitstream-manifest.json').read_text())
    guard = json.loads((p/'bitstream-guard.json').read_text())
    assert m['schema'] == 'npu128-pango-bitstream-v1' and guard['status'] == 'PASS'
    assert guard['manifest_sha256'] == digest(p/'bitstream-manifest.json')
    for name, expected in m['immutable_sha256'].items():
        path = (p/name).resolve()
        assert path.is_relative_to(p) and digest(path) == expected, name
    physical_path = p/'bitstream-input/implementation.json'
    implementation = (ROOT/m.get('repository_implementation', 'reports/npu128-uart-implementation.json')).resolve()
    assert implementation.is_relative_to(ROOT)
    assert digest(physical_path) == m['implementation_sha256'] == digest(implementation)
    physical = json.loads(physical_path.read_text())
    assert physical['status'] == 'PASS' and physical['mapped_functional_run']
    assert m.get('bulk_weights', False) == physical['functional_validation'].get('bulk_weights', False)
    for name, expected in physical['functional_validation']['source_sha256'].items():
        assert digest(ROOT/name) == expected, name
    original = Path(m['source_project'])
    for name, expected in physical['raw_evidence_sha256'].items():
        assert digest(original/name) == expected, name
    assert digest(p/m['routed_database']) == m['routed_database_sha256'] == physical['raw_evidence_sha256'][m['routed_database']]
    directory = p/'prj_tasks/pnr_1/generate_bitstream'
    settings_path = directory/'report_db/generate_bit_stream_settings.db'
    settings = {row[0].text or '': row[1].text or '' for row in ET.parse(settings_path).getroot().iter('row') if len(row) == 2}
    assert settings['Set Configuration Bank Voltage(SCBV)'] == 'High Voltage'
    assert settings['Usercode'].lower().removeprefix('0x') == m['bitstream_configuration']['usercode']
    assert settings['Disable Cyclic Redundancy Checking(CRC)'] == 'false'
    assert settings['Unused IO Status'] == 'NONE'
    assert settings['Top Module'] == m['top'] and settings['Part'] == 'PG2L200H-6FBB676'
    assert settings['Encrypt Bitstream'] == 'false'
    config_path = (ROOT/m.get('repository_config', 'config/uart-bitstream.json')).resolve()
    assert config_path.is_relative_to(ROOT)
    assert m['bitstream_configuration'] == json.loads(config_path.read_text())
    log = (p/'bitstream.log').read_text()
    assert not re.search(r'(?m)^[EC]:', log)
    assert not re.search(r'(?m)^Process "(?:Compile|Synthesize|Device Map|Place|Route|Route Optimize)" started\.', log)
    bgr = (directory/f'{m["top"]}.bgr').read_text()
    assert str(p/m['routed_database']) in bgr.replace('F:/', '/mnt/f/'), 'unexpected input DB'
    bits = list(directory.glob('*.sbit'))
    assert len(bits) == 1 and bits[0].stat().st_size == guard['bitstream_bytes']
    assert digest(bits[0]) == guard['bitstream_sha256']
    raw = [p/'bitstream-manifest.json', p/'bitstream-guard.json', p/'bitstream-samples.jsonl',
           p/'bitstream.log', p/'uart_bitstream.pds', *directory.rglob('*')]
    report = {'schema': 'npu128-uart-bitstream-assessment-v1', 'status': 'PASS',
        'device': m['device'], 'top': m['top'], 'clock_hz': m['clock_hz'], 'project': m['project'],
        'bulk_weights': m.get('bulk_weights', False),
        'source_commit': m['source_commit'], 'implementation_sha256': m['implementation_sha256'],
        'routed_database_sha256': m['routed_database_sha256'], 'immutable_sha256': m['immutable_sha256'],
        'bitstream': str(bits[0].relative_to(p)), 'bitstream_sha256': digest(bits[0]),
        'bitstream_bytes': bits[0].stat().st_size, 'bitstream_configuration': m['bitstream_configuration'],
        'actual_settings': settings, 'memory_guard': guard, 'implementation_rerun': False,
        'original_implementation_unchanged': True, 'hardware_programmed_by_this_flow': False,
        'raw_evidence_sha256': {str(f.relative_to(p)): digest(f) for f in sorted(set(raw)) if f.is_file()},
        'collector_sha256': digest(Path(__file__)),
        'limitations': [f'{m["clock_hz"]//1000000} MHz diagnostic; timing proof is in its exact routed implementation reference.',
            'Generation proof only; see separate board report for JTAG/serial results.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(f'PASS accepted bitstream: {report["bitstream_bytes"]} bytes, SHA256={report["bitstream_sha256"]}')


if __name__ == '__main__':
    main()
