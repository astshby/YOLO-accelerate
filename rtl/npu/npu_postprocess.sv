// SPDX-License-Identifier: Apache-2.0
// Module: npu_postprocess
// Description: Four-lane fixed-point requantization and programmable activation LUT.
// A 16-channel vector is issued in four beats; graph/layer scheduling stays external.
module npu_postprocess (
    input  logic clk,
    input  logic rst,
    input  logic cfg_valid,
    output logic cfg_ready,
    input  logic [1:0] cfg_kind,
    input  logic [7:0] cfg_addr,
    input  logic [31:0] cfg_data,
    output logic cfg_error,
    output logic lut_ready,
    input  logic in_valid,
    output logic in_ready,
    input  logic [1:0] in_mode,
    input  logic [511:0] in_data,
    input  logic [15:0] in_mask,
    input  logic [7:0] in_x,
    input  logic [7:0] in_y,
    input  logic in_last,
    output logic out_valid,
    input  logic out_ready,
    output logic [1:0] out_mode,
    output logic [511:0] out_raw,
    output logic [127:0] out_int8,
    output logic [15:0] out_mask,
    output logic [7:0] out_x,
    output logic [7:0] out_y,
    output logic out_last,
    output logic busy
);
    timeunit 1ns;
    timeprecision 1ps;
    typedef enum logic [1:0] {IDLE, ISSUE, DRAIN, OUTPUT} state_t;
    state_t state_q;
    logic cfg_fire, in_fire, out_fire, cfg_ok, lut_write;
    logic [8:0] lut_next_q;
    logic [30:0] multiplier_q [16];
    logic [5:0] shift_q [16];
    logic [1:0] issue_group_q;
    localparam int POST_STAGES = 6;
    logic [POST_STAGES-1:0] valid_q;
    logic [1:0] group_q [POST_STAGES];
    logic [3:0] mask_q [POST_STAGES];
    logic signed [31:0] value_q0 [4];
    logic [30:0] multiplier_q0 [4];
    logic [5:0] shift_q0 [4], shift_q1 [4], shift_q2 [4];
    logic signed [62:0] product_q1 [4];
    logic [62:0] magnitude_q2 [4], shifted_q3 [4];
    logic negative_q2 [4], negative_q3 [4], round_q3 [4];
    logic [7:0] quantized_q4 [4], bypass_q5 [4], lut_value [4];

    assign busy = state_q != IDLE;
    assign cfg_ready = !rst && !busy;
    assign cfg_fire = cfg_valid && cfg_ready;
    // 配置写优先，禁止同拍接受使用旧配置的新向量。
    assign in_ready = !rst && !busy && !cfg_valid;
    assign in_fire = in_valid && in_ready;
    assign out_valid = !rst && state_q == OUTPUT;
    assign out_fire = out_valid && out_ready;
    assign lut_write = cfg_fire && cfg_ok && cfg_kind == 2'd2;

    always_comb begin
        cfg_ok = 1'b0;
        unique case (cfg_kind)
            2'd0: cfg_ok = cfg_addr < 16 && !cfg_data[31];
            2'd1: cfg_ok = cfg_addr < 16 && cfg_data[31:6] == 0;
            // 256 项顺序装载；地址 0 开始重载，未完成时禁止使用 LUT 模式。
            2'd2: cfg_ok = cfg_data[31:8] == 0 &&
                          (cfg_addr == 0 || (!lut_ready && {1'b0, cfg_addr} == lut_next_q));
            default: cfg_ok = 1'b0;
        endcase
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            cfg_error <= 1'b0;
            lut_ready <= 1'b0;
            lut_next_q <= '0;
            for (int lane = 0; lane < 16; lane++) begin
                multiplier_q[lane] <= 31'd1;
                shift_q[lane] <= '0;
            end
        end else begin
            cfg_error <= cfg_fire && !cfg_ok;
            if (cfg_fire && cfg_ok) begin
                unique case (cfg_kind)
                    2'd0: multiplier_q[cfg_addr[3:0]] <= cfg_data[30:0];
                    2'd1: shift_q[cfg_addr[3:0]] <= cfg_data[5:0];
                    2'd2: begin
                        lut_next_q <= {1'b0, cfg_addr} + 9'd1;
                        lut_ready <= cfg_addr == 8'hff;
                    end
                    default: begin end
                endcase
            end
        end
    end

    function automatic logic [7:0] saturate_rounded(
        input logic [62:0] shifted,
        input logic round_up,
        input logic negative
    );
        logic [7:0] rounded;
        begin
            // floor(m / 2^s) plus bit s-1 is exactly nearest, ties away from zero.
            // Clamp the wide quotient before an eight-bit increment/sign restore.
            rounded = shifted[7:0] + {7'b0, round_up};
            if (negative) begin
                if (shifted >= 63'd128) saturate_rounded = 8'h80;
                else saturate_rounded = 8'd0 - rounded;
            end else begin
                if (shifted >= 63'd127) saturate_rounded = 8'h7f;
                else saturate_rounded = rounded;
            end
        end
    endfunction

    // 四份小表提供独立读地址；共享配置写，不在复位中清除 RAM。
    for (genvar lane = 0; lane < 4; lane++) begin : g_post_lane
        adam_sdp_ram #(.WIDTH(8), .DEPTH(256), .ADDR_W(8)) u_lut (
            .clk,
            .wr_en(lut_write), .wr_addr(cfg_addr), .wr_data(cfg_data[7:0]),
            .rd_en(valid_q[4] && out_mode == 2'd2),
            .rd_addr(quantized_q4[lane]), .rd_data(lut_value[lane])
        );
        // Select -> multiply -> magnitude -> shift/guard -> clamp -> LUT.
        // group 先补零再转 int，避免 ADS 将 2/3 当成负索引。
        always_ff @(posedge clk) begin
            if (state_q == ISSUE) begin
                value_q0[lane] <= $signed(out_raw[(int'({1'b0, issue_group_q})*4+lane)*32 +: 32]);
                multiplier_q0[lane] <= multiplier_q[int'({1'b0, issue_group_q})*4+lane];
                shift_q0[lane] <= shift_q[int'({1'b0, issue_group_q})*4+lane];
            end
            product_q1[lane] <= value_q0[lane] * $signed({1'b0, multiplier_q0[lane]});
            shift_q1[lane] <= shift_q0[lane];
            magnitude_q2[lane] <= product_q1[lane][62] ? (~product_q1[lane] + 63'd1) : product_q1[lane];
            negative_q2[lane] <= product_q1[lane][62];
            shift_q2[lane] <= shift_q1[lane];
            shifted_q3[lane] <= magnitude_q2[lane] >> shift_q2[lane];
            round_q3[lane] <= shift_q2[lane] != 0 && magnitude_q2[lane][shift_q2[lane] - 6'd1];
            negative_q3[lane] <= negative_q2[lane];
            quantized_q4[lane] <= saturate_rounded(shifted_q3[lane], round_q3[lane], negative_q3[lane]);
            bypass_q5[lane] <= quantized_q4[lane];
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            state_q <= IDLE;
            valid_q <= '0;
            issue_group_q <= '0;
            for (int stage = 0; stage < POST_STAGES; stage++) begin
                group_q[stage] <= '0;
                mask_q[stage] <= '0;
            end
            out_mode <= '0;
            out_raw <= '0;
            out_int8 <= '0;
            out_mask <= '0;
            out_x <= '0;
            out_y <= '0;
            out_last <= 1'b0;
        end else begin
            valid_q <= {valid_q[POST_STAGES-2:0], state_q == ISSUE};
            group_q[0] <= issue_group_q;
            mask_q[0] <= out_mask[int'({1'b0, issue_group_q})*4 +: 4];
            for (int stage = 1; stage < POST_STAGES; stage++) begin
                group_q[stage] <= group_q[stage-1];
                mask_q[stage] <= mask_q[stage-1];
            end
            if (valid_q[POST_STAGES-1]) begin
                for (int lane = 0; lane < 4; lane++) begin
                    if (!mask_q[POST_STAGES-1][lane]) out_int8[(int'({1'b0, group_q[POST_STAGES-1]})*4+lane)*8 +: 8] <= '0;
                    else if (out_mode == 2'd2)
                        out_int8[(int'({1'b0, group_q[POST_STAGES-1]})*4+lane)*8 +: 8] <= lut_value[lane];
                    else out_int8[(int'({1'b0, group_q[POST_STAGES-1]})*4+lane)*8 +: 8] <= bypass_q5[lane];
                end
                if (group_q[POST_STAGES-1] == 2'd3) state_q <= OUTPUT;
            end
            unique case (state_q)
                IDLE: begin
                    if (in_fire) begin
                        out_mode <= in_mode;
                        out_raw <= in_data;
                        out_int8 <= '0;
                        out_mask <= in_mask;
                        out_x <= in_x;
                        out_y <= in_y;
                        out_last <= in_last;
                        issue_group_q <= '0;
                        if (in_mode == 0) state_q <= OUTPUT;
                        else state_q <= ISSUE;
                    end
                end
                ISSUE: begin
                    issue_group_q <= issue_group_q + 2'd1;
                    if (issue_group_q == 2'd3) state_q <= DRAIN;
                end
                DRAIN: begin end
                OUTPUT: if (out_fire) state_q <= IDLE;
                default: state_q <= IDLE;
            endcase
        end
    end
endmodule
