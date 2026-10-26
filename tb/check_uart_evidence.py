"""Asynchronous timing exceptions must terminate at the intended FF boundary."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools/pango'))
from collect_uart_implementation import async_boundary_audit

GOOD = '''
GTP_INBUF #(.IOSTANDARD("DEFAULT")) rxbuf (.I(uart_rxd), .O(rx));
GTP_DFF_R rx_meta_q (.D(rx), .Q(meta), .R(reset), .CLK(clk));
GTP_DFF_R rx_sync_q (.D(meta), .Q(sync), .R(reset), .CLK(clk));
GTP_INBUF rstbuf (.I(board_reset_n), .O(rn));
GTP_LUT1 inv (.I0(rn), .Z(ar));
GTP_DFF_P reset_release_q0 (.D(zero), .Q(q0), .P(ar), .CLK(clk));
GTP_DFF_P reset_release_q1 (.D(q0), .Q(q1), .P(ar), .CLK(clk));
GTP_DFF_P reset_release_q2 (.D(q1), .Q(reset), .P(ar), .CLK(clk));
'''


class BoundaryTests(unittest.TestCase):
    def test_expected_chain(self):
        self.assertEqual(async_boundary_audit(GOOD)['status'], 'PASS')

    def test_reject_rx_bypass(self):
        with self.assertRaises(RuntimeError):
            async_boundary_audit(GOOD+'GTP_DFF bad (.D(rx), .Q(other), .CLK(clk));')

    def test_reject_reset_bypass(self):
        with self.assertRaises(RuntimeError):
            async_boundary_audit(GOOD+'GTP_DFF_R bad (.D(data), .Q(other), .R(ar), .CLK(clk));')

    def test_reject_second_stage_fanout(self):
        with self.assertRaises(RuntimeError):
            async_boundary_audit(GOOD+'GTP_LUT1 escape (.I0(meta), .O(other));')


if __name__ == '__main__':
    unittest.main()
