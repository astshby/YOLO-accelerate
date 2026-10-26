// SPDX-License-Identifier: Apache-2.0
// Same board pins; the physical 27 MHz oscillator feeds a real 100 MHz GPLL.
module npu_uart_board100 (
    input wire clk_27m, board_reset_n, uart_rxd,
    output wire uart_txd
);
    timeunit 1ns;
    timeprecision 1ps;
    wire clk_100m, pll_locked;
    npu_clock100 u_clock (.clk_27m, .reset_n(board_reset_n), .clk_100m, .locked(pll_locked));
    // PLL loss asynchronously asserts the existing release synchronizer.
    // All UART/MMIO/NPU state releases on the generated clock after lock.
    npu_uart_board #(.CLOCK_HZ(100000000), .BAUD(115200), .INTERBYTE_TIMEOUT(10000000)) u_system (
        .clk_27m(clk_100m), .board_reset_n(board_reset_n && pll_locked), .uart_rxd, .uart_txd
    );
endmodule
