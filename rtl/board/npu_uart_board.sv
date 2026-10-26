// SPDX-License-Identifier: Apache-2.0
// MES2L676-200HP diagnostic: native 27 MHz, no PLL, CPU, DDR or DMA.
module npu_uart_board #(
    parameter int CLOCK_HZ = 27000000,
    parameter int BAUD = 115200,
    parameter int INTERBYTE_TIMEOUT = CLOCK_HZ/10
) (
    input logic clk_27m, board_reset_n, uart_rxd,
    output logic uart_txd
);
    timeunit 1ns;
    timeprecision 1ps;
    localparam int DIVISOR = (CLOCK_HZ+BAUD/2)/BAUD;
    // Async assertion and three rising edges for release. Press KEY0 after
    // configuration for an explicit reset, regardless of power-on state.
    logic [2:0] reset_release_q;
    logic rst;
    logic rx_valid, rx_error, tx_valid, tx_ready;
    logic [7:0] rx_data, tx_data;
    logic req_valid, req_ready, req_write, rsp_valid, rsp_ready, rsp_error;
    logic [15:0] req_addr;
    logic [31:0] req_wdata, rsp_rdata;
    logic [3:0] req_wstrb;
    always_ff @(posedge clk_27m or negedge board_reset_n) begin
        if (!board_reset_n) reset_release_q <= 3'b111;
        else reset_release_q <= {reset_release_q[1:0], 1'b0};
    end
    assign rst = reset_release_q[2];
    uart_8n1 #(.CLOCKS_PER_BIT(DIVISOR)) u_uart (
        .clk(clk_27m), .rst, .uart_rx(uart_rxd), .uart_tx(uart_txd),
        .rx_valid, .rx_error, .rx_data, .tx_valid, .tx_ready, .tx_data
    );
    uart_mmio_bridge #(.INTERBYTE_TIMEOUT(INTERBYTE_TIMEOUT)) u_bridge (
        .clk(clk_27m), .rst, .rx_valid, .rx_error, .rx_data, .tx_valid, .tx_ready, .tx_data,
        .req_valid, .req_ready, .req_write, .req_addr, .req_wdata, .req_wstrb,
        .rsp_valid, .rsp_ready, .rsp_rdata, .rsp_error
    );
    npu_mmio128 u_device (
        .clk(clk_27m), .rst, .req_valid, .req_ready, .req_write, .req_addr, .req_wdata, .req_wstrb,
        .rsp_valid, .rsp_ready, .rsp_rdata, .rsp_error
    );
endmodule
