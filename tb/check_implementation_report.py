"""Protect evidence collection from stale zero counts or ignored logic changes."""
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools/pango'))
from collect_implementation import activation_memory_mapping, canonical_netlist, final_unrouted_count, functional_report


class EvidenceTests(unittest.TestCase):
    def test_memory_mapping_counts_physical_primitives_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'mapping.log'
            instance = 'u_npu/u_conv/g_activation_bank[0].g_segment[1].u_ram'
            path.write_text(
                f'| {instance}/mem | 8 K x 64 | | | | | |\n'
                f'| {instance}/mem_0 | 8 K x 4 | 8 K x 4 | NORMAL_WRITE | NORMAL_WRITE | SIMPLE_DUAL_PORT | GTP_DRM36K_E1 |\n'
                f'| {instance}/mem_1 | 8 K x 4 | 8 K x 4 | NORMAL_WRITE | NORMAL_WRITE | SIMPLE_DUAL_PORT | GTP_DRM36K_E1 |\n'
                '| u_npu/u_conv/u_weight_ram/mem_0 | 1 K x 36 | 1 K x 36 | NORMAL_WRITE | NORMAL_WRITE | SIMPLE_DUAL_PORT | GTP_DRM36K_E1 |\n')
            self.assertEqual(activation_memory_mapping(path), [{
                'instance': instance, 'write_configuration': '8 K x 4',
                'read_configuration': '8 K x 4', 'primitive': 'GTP_DRM36K_E1', 'count': 2}])

    def test_changed_netlist_requires_its_own_functional_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            netlist, receipt = [Path(directory)/name for name in ('netlist.vm', 'gate.json')]
            netlist.write_text('module x; assign q = 1; endmodule\n')
            report = {'status': 'PASS', 'netlist_sha256': hashlib.sha256(netlist.read_bytes()).hexdigest()}
            receipt.write_text(json.dumps(report))
            self.assertEqual(functional_report(receipt, netlist), report)
            netlist.write_text('module x; assign q = 0; endmodule\n')
            with self.assertRaises(RuntimeError):
                functional_report(receipt, netlist)
            report.update(status='FAIL', netlist_sha256=hashlib.sha256(netlist.read_bytes()).hexdigest())
            receipt.write_text(json.dumps(report))
            with self.assertRaises(RuntimeError):
                functional_report(receipt, netlist)

    def test_latest_count_wins_across_formats(self):
        text = 'Unrouted nets: 0\nUnrouted nets at iteration 38 (0.220 sec): 3\n'
        self.assertEqual(final_unrouted_count(text)[0], 3)
        self.assertEqual(final_unrouted_count(text+'Number of unrouted nets = 0\n')[0], 0)

    def test_no_completion_only_shortcut(self):
        with self.assertRaises(RuntimeError):
            final_unrouted_count("Design 'npu_measure_top' has been routed successfully.\n")
        self.assertEqual(final_unrouted_count('All nets are routed successfully\n')[0], 0)
        self.assertEqual(final_unrouted_count('Unrouted nets | 1,024\n')[0], 1024)

    def test_netlist_metadata_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'netlist.vm'
            path.write_text('// Generated at time A\n\t// SDC constraint : path A\nmodule x;\nassign q = 1;\nendmodule\n')
            reference = canonical_netlist(path)
            path.write_text('// Generated at time B\n\t// SDC constraint : path B\nmodule x;\nassign q = 1;\nendmodule\n')
            self.assertEqual(reference, canonical_netlist(path))
            path.write_text('module x;\nassign q = 0;\nendmodule\n')
            self.assertNotEqual(reference, canonical_netlist(path))


if __name__ == '__main__':
    unittest.main()
