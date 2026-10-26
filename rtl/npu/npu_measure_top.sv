// SPDX-License-Identifier: Apache-2.0
// Module: npu_measure_top
// Description: Synchronous scan boundary for independent NPU resource/timing measurement.
// Diagnostic only: no CPU, graph scheduler, AXI, DDR, PLL, board pins or bitstream.
module npu_measure_top (
    input  logic clk,
    input  logic rst,
    input  logic scan_shift,
    input  logic scan_update,
    input  logic scan_capture,
    input  logic scan_din,
    output logic scan_dout
);
    timeunit 1ns;
    timeprecision 1ps;
    typedef struct packed {
        logic act_wr_bank;
        logic cmd_act_bank;
        logic act_wr_valid;
        logic [14:0] act_wr_addr;
        logic [63:0] act_wr_data;
        logic weight_wr_valid;
        logic [9:0] weight_wr_addr;
        logic [1023:0] weight_wr_data;
        logic cfg_valid;
        logic [1:0] cfg_kind;
        logic [7:0] cfg_addr;
        logic [31:0] cfg_data;
        logic cmd_valid;
        logic [1:0] cmd_mode;
        logic [7:0] cmd_width;
        logic [7:0] cmd_height;
        logic [15:0] cmd_cin;
        logic [4:0] cmd_cout;
        logic cmd_kernel3;
        logic cmd_stride2;
        logic cmd_pad;
        logic [511:0] cmd_bias;
        logic out_ready;
    } drive_t;
    localparam int INPUT_BITS = $bits(drive_t);
    localparam int OUTPUT_BITS = 813;
    logic [INPUT_BITS-1:0] input_shift_q;
    drive_t drive_q;
    logic apply_q;
    logic [OUTPUT_BITS-1:0] observe, snapshot_q;
    logic act_wr_ready, weight_wr_ready, cfg_ready, cfg_error, lut_ready;
    logic cmd_ready, cmd_error, busy, done, out_valid, out_last;
    logic [1:0] out_mode;
    logic [511:0] out_raw;
    logic [127:0] out_int8;
    logic [15:0] out_mask;
    logic [7:0] out_x, out_y;
    logic [31:0] perf_mac_steps, perf_conv_cycles, perf_total_cycles, perf_post_stalls;

    assign observe = {act_wr_ready, weight_wr_ready, cfg_ready, cfg_error, lut_ready,
                      cmd_ready, cmd_error, busy, done, out_valid, out_mode, out_raw,
                      out_int8, out_mask, out_x, out_y, out_last,
                      perf_mac_steps, perf_conv_cycles, perf_total_cycles, perf_post_stalls};
    assign scan_dout = snapshot_q[OUTPUT_BITS-1];

    // MSB 先入/先出。update 原子提交完整输入，下一拍仅产生一次操作脉冲。
    // capture 优先于输出移位；移位过程不改变正在驱动 DUT 的数据。
    always_ff @(posedge clk) begin
        if (rst) begin
            input_shift_q <= '0;
            drive_q <= '0;
            apply_q <= 1'b0;
            snapshot_q <= '0;
        end else begin
            apply_q <= scan_update;
            if (scan_update) begin
                drive_q <= drive_t'(input_shift_q);
                input_shift_q <= '0;
            end else if (scan_shift) input_shift_q <= {input_shift_q[INPUT_BITS-2:0], scan_din};
            if (scan_capture) snapshot_q <= observe;
            else if (scan_shift) snapshot_q <= {snapshot_q[OUTPUT_BITS-2:0], 1'b0};
        end
    end

    npu_conv128 u_npu (
        .clk, .rst,
        .act_wr_bank(drive_q.act_wr_bank), .cmd_act_bank(drive_q.cmd_act_bank),
        .act_wr_valid(apply_q && drive_q.act_wr_valid), .act_wr_ready,
        .act_wr_addr(drive_q.act_wr_addr), .act_wr_data(drive_q.act_wr_data),
        .weight_wr_valid(apply_q && drive_q.weight_wr_valid), .weight_wr_ready,
        .weight_wr_addr(drive_q.weight_wr_addr), .weight_wr_data(drive_q.weight_wr_data),
        .cfg_valid(apply_q && drive_q.cfg_valid), .cfg_ready,
        .cfg_kind(drive_q.cfg_kind), .cfg_addr(drive_q.cfg_addr), .cfg_data(drive_q.cfg_data),
        .cfg_error, .lut_ready,
        .cmd_valid(apply_q && drive_q.cmd_valid), .cmd_ready,
        .cmd_mode(drive_q.cmd_mode), .cmd_width(drive_q.cmd_width), .cmd_height(drive_q.cmd_height),
        .cmd_cin(drive_q.cmd_cin), .cmd_cout(drive_q.cmd_cout),
        .cmd_kernel3(drive_q.cmd_kernel3), .cmd_stride2(drive_q.cmd_stride2),
        .cmd_pad(drive_q.cmd_pad), .cmd_bias(drive_q.cmd_bias),
        .cmd_error, .busy, .done, .out_valid,
        .out_ready(apply_q && drive_q.out_ready),
        .out_mode, .out_raw, .out_int8, .out_mask, .out_x, .out_y, .out_last,
        .perf_mac_steps, .perf_conv_cycles, .perf_total_cycles, .perf_post_stalls
    );
endmodule
