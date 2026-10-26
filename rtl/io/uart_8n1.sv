// SPDX-License-Identifier: Apache-2.0
// Fixed-rate 8N1 UART. Asynchronous RX enters ONLY the two-flop synchronizer.
module uart_8n1 #(
    parameter int CLOCKS_PER_BIT = 234,
    parameter int CW = $clog2(CLOCKS_PER_BIT) < 1 ? 1 : $clog2(CLOCKS_PER_BIT)
) (
    input logic clk, rst, uart_rx,
    output logic uart_tx,
    output logic rx_valid, rx_error,
    output logic [7:0] rx_data,
    input logic tx_valid,
    output logic tx_ready,
    input logic [7:0] tx_data
);
    timeunit 1ns;
    timeprecision 1ps;
    logic rx_meta_q, rx_sync_q;
    typedef enum logic [2:0] {RX_IDLE, RX_START, RX_DATA, RX_STOP, RX_HIGH} rx_state_t;
    rx_state_t rx_state_q;
    logic [CW-1:0] rx_timer_q, tx_timer_q;
    logic [2:0] rx_bit_q;
    logic [7:0] rx_shift_q;
    logic tx_active_q;
    logic [3:0] tx_bit_q;
    logic [9:0] tx_shift_q;
    assign uart_tx = !tx_active_q || tx_shift_q[0];
    assign tx_ready = !rst && !tx_active_q;
    always_ff @(posedge clk) begin
        if (rst) begin rx_meta_q <= 1'b1; rx_sync_q <= 1'b1; end
        else begin rx_meta_q <= uart_rx; rx_sync_q <= rx_meta_q; end
    end
    always_ff @(posedge clk) begin
        if (rst) begin
            rx_state_q <= RX_IDLE;
            rx_timer_q <= '0;
            rx_bit_q <= '0;
            rx_shift_q <= '0;
            rx_data <= '0;
            rx_valid <= 1'b0;
            rx_error <= 1'b0;
        end else begin
            rx_valid <= 1'b0;
            rx_error <= 1'b0;
            unique case (rx_state_q)
                RX_IDLE: if (!rx_sync_q) begin
                    rx_timer_q <= CW'(CLOCKS_PER_BIT/2-1);
                    rx_state_q <= RX_START;
                end
                RX_START: if (rx_timer_q != 0) rx_timer_q <= rx_timer_q - 1'b1;
                    else if (rx_sync_q) rx_state_q <= RX_IDLE;
                    else begin
                        rx_state_q <= RX_DATA;
                        rx_timer_q <= CW'(CLOCKS_PER_BIT-1);
                        rx_bit_q <= '0;
                    end
                RX_DATA: if (rx_timer_q != 0) rx_timer_q <= rx_timer_q - 1'b1;
                    else begin
                        rx_shift_q[rx_bit_q] <= rx_sync_q;
                        rx_timer_q <= CW'(CLOCKS_PER_BIT-1);
                        if (rx_bit_q == 7) rx_state_q <= RX_STOP;
                        else rx_bit_q <= rx_bit_q + 1'b1;
                    end
                RX_STOP: if (rx_timer_q != 0) rx_timer_q <= rx_timer_q - 1'b1;
                    else if (rx_sync_q) begin
                        rx_data <= rx_shift_q;
                        rx_valid <= 1'b1;
                        rx_state_q <= RX_IDLE;
                    end else begin
                        rx_error <= 1'b1;
                        rx_state_q <= RX_HIGH;
                    end
                RX_HIGH: if (rx_sync_q) rx_state_q <= RX_IDLE;
                default: rx_state_q <= RX_IDLE;
            endcase
        end
    end
    always_ff @(posedge clk) begin
        if (rst) begin
            tx_active_q <= 1'b0;
            tx_shift_q <= '1;
            tx_timer_q <= '0;
            tx_bit_q <= '0;
        end else if (!tx_active_q) begin
            if (tx_valid) begin
                tx_active_q <= 1'b1;
                tx_shift_q <= {1'b1, tx_data, 1'b0};
                tx_timer_q <= CW'(CLOCKS_PER_BIT-1);
                tx_bit_q <= '0;
            end
        end else if (tx_timer_q != 0) tx_timer_q <= tx_timer_q - 1'b1;
        else if (tx_bit_q == 9) tx_active_q <= 1'b0;
        else begin
            tx_shift_q <= {1'b1, tx_shift_q[9:1]};
            tx_timer_q <= CW'(CLOCKS_PER_BIT-1);
            tx_bit_q <= tx_bit_q + 1'b1;
        end
    end
endmodule
