// SPDX-License-Identifier: Apache-2.0
// Module: npu_conv128
// Description: CPU-issued tile convolution with optional fixed-point output processing.
// Mode 0: INT32 passthrough; 1: requantized INT8; 2: requantized INT8 + activation LUT.
module npu_conv128 #(
    parameter int MAX_SIDE = 16,
    parameter int MAX_CIN = 768,
    parameter int GROUPS = (MAX_CIN + 7) / 8,
    parameter int ACT_AW = $clog2(MAX_SIDE * MAX_SIDE * GROUPS),
    parameter int WEIGHT_AW = $clog2(9 * GROUPS)
) (
    input  logic clk,
    input  logic rst,
    input  logic act_wr_bank,
    input  logic act_wr_valid,
    output logic act_wr_ready,
    input  logic [ACT_AW-1:0] act_wr_addr,
    input  logic [63:0] act_wr_data,
    input  logic weight_wr_valid,
    output logic weight_wr_ready,
    input  logic [WEIGHT_AW-1:0] weight_wr_addr,
    input  logic [1023:0] weight_wr_data,
    input  logic cfg_valid,
    output logic cfg_ready,
    input  logic [1:0] cfg_kind,
    input  logic [7:0] cfg_addr,
    input  logic [31:0] cfg_data,
    output logic cfg_error,
    output logic lut_ready,
    input  logic cmd_valid,
    input  logic cmd_act_bank,
    output logic cmd_ready,
    input  logic [1:0] cmd_mode,
    input  logic [7:0] cmd_width,
    input  logic [7:0] cmd_height,
    input  logic [15:0] cmd_cin,
    input  logic [4:0] cmd_cout,
    input  logic cmd_kernel3,
    input  logic cmd_stride2,
    input  logic cmd_pad,
    input  logic [511:0] cmd_bias,
    output logic cmd_error,
    output logic busy,
    output logic done,
    output logic out_valid,
    input  logic out_ready,
    output logic [1:0] out_mode,
    output logic [511:0] out_raw,
    output logic [127:0] out_int8,
    output logic [15:0] out_mask,
    output logic [7:0] out_x,
    output logic [7:0] out_y,
    output logic out_last,
    output logic [31:0] perf_mac_steps,
    output logic [31:0] perf_conv_cycles,
    output logic [31:0] perf_total_cycles,
    output logic [31:0] perf_post_stalls
);
    timeunit 1ns;
    timeprecision 1ps;
    logic active_q, local_error_q, mode_ok, cmd_fire, out_fire;
    logic active_bank_q, act_bank_writable;
    logic [1:0] mode_q;
    logic conv_cmd_ready, conv_error, conv_valid, conv_ready;
    logic conv_busy, post_busy, unused_conv_done;
    logic conv_act_ready, conv_weight_ready, post_cfg_ready;
    logic [511:0] conv_data;
    logic [15:0] conv_mask;
    logic [7:0] conv_x, conv_y;
    logic conv_last;

    assign busy = active_q || conv_busy || post_busy;
    assign mode_ok = cmd_mode < 3 && (cmd_mode != 2 || lut_ready);
    assign cfg_ready = !rst && !busy && post_cfg_ready;
    assign act_bank_writable = !busy || act_wr_bank != active_bank_q;
    assign act_wr_ready = !rst && act_bank_writable && conv_act_ready;
    assign weight_wr_ready = !rst && !busy && conv_weight_ready;
    assign cmd_ready = !rst && !busy && !cfg_valid && conv_cmd_ready;
    assign cmd_fire = cmd_valid && cmd_ready;
    assign out_fire = out_valid && out_ready;
    assign cmd_error = local_error_q || conv_error;

    adam_conv128 #(.MAX_SIDE(MAX_SIDE), .MAX_CIN(MAX_CIN)) u_conv (
        .clk, .rst,
        .act_wr_valid(act_wr_valid && act_bank_writable), .act_wr_ready(conv_act_ready), .act_wr_bank,
        .act_wr_addr, .act_wr_data,
        .weight_wr_valid(weight_wr_valid && !busy), .weight_wr_ready(conv_weight_ready),
        .weight_wr_addr, .weight_wr_data,
        .cmd_valid(cmd_fire && mode_ok), .cmd_ready(conv_cmd_ready), .cmd_act_bank,
        .cmd_width, .cmd_height, .cmd_cin, .cmd_cout,
        .cmd_kernel3, .cmd_stride2, .cmd_pad, .cmd_bias,
        .cmd_error(conv_error), .busy(conv_busy), .done(unused_conv_done),
        .out_valid(conv_valid), .out_ready(conv_ready), .out_data(conv_data),
        .out_mask(conv_mask), .out_x(conv_x), .out_y(conv_y), .out_last(conv_last),
        .perf_mac_steps, .perf_active_cycles(perf_conv_cycles)
    );

    npu_postprocess u_post (
        .clk, .rst,
        .cfg_valid(cfg_valid && !busy), .cfg_ready(post_cfg_ready),
        .cfg_kind, .cfg_addr, .cfg_data, .cfg_error, .lut_ready,
        .in_valid(conv_valid), .in_ready(conv_ready), .in_mode(mode_q),
        .in_data(conv_data), .in_mask(conv_mask), .in_x(conv_x), .in_y(conv_y), .in_last(conv_last),
        .out_valid, .out_ready, .out_mode, .out_raw, .out_int8,
        .out_mask, .out_x, .out_y, .out_last, .busy(post_busy)
    );

    always_ff @(posedge clk) begin
        if (rst) begin
            active_q <= 1'b0;
            active_bank_q <= 1'b0;
            local_error_q <= 1'b0;
            mode_q <= '0;
            done <= 1'b0;
            perf_total_cycles <= '0;
            perf_post_stalls <= '0;
        end else begin
            local_error_q <= 1'b0;
            done <= 1'b0;
            if (active_q) perf_total_cycles <= perf_total_cycles + 32'd1;
            if (conv_valid && !conv_ready) perf_post_stalls <= perf_post_stalls + 32'd1;
            // 对外完成必须等待后处理的最后一个输出真正交付，不能沿用卷积核 done。
            if (conv_error) active_q <= 1'b0;
            else if (out_fire && out_last) begin
                active_q <= 1'b0;
                done <= 1'b1;
            end else if (cmd_fire) begin
                if (!mode_ok) local_error_q <= 1'b1;
                else begin
                    active_q <= 1'b1;
                    active_bank_q <= cmd_act_bank;
                    mode_q <= cmd_mode;
                    perf_total_cycles <= '0;
                    perf_post_stalls <= '0;
                end
            end
        end
    end
endmodule
