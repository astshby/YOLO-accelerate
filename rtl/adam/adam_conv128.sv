// SPDX-License-Identifier: Apache-2.0
// Module: adam_conv128
// Description: Tile sequencer around the ported adamgallas paired-MAC backend.
// New control/layout: 1x1/3x3, stride 1/2, zero padding, channel tails and INT32 bias.
// One command computes all pixels of an input tile for up to 16 output channels.
module adam_conv128 #(
    parameter int MAX_SIDE = 16,
    parameter int MAX_CIN = 768,
    parameter int GROUPS = (MAX_CIN + 7) / 8,
    parameter int ACT_DEPTH = MAX_SIDE * MAX_SIDE * GROUPS,
    parameter int WEIGHT_DEPTH = 9 * GROUPS,
    parameter int ACT_AW = $clog2(ACT_DEPTH),
    parameter int WEIGHT_AW = $clog2(WEIGHT_DEPTH)
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
    input  logic cmd_valid,
    input  logic cmd_act_bank,
    output logic cmd_ready,
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
    output logic [511:0] out_data,
    output logic [15:0] out_mask,
    output logic [7:0] out_x,
    output logic [7:0] out_y,
    output logic out_last,
    output logic [31:0] perf_mac_steps,
    output logic [31:0] perf_active_cycles
);
    timeunit 1ns;
    timeprecision 1ps;
    typedef enum logic {IDLE, ACTIVE} state_t;
    state_t state_q;
    logic cmd_fire, out_fire, descriptor_ok;
    logic active_bank_q;
    // Bound each physical array to 8K words. A 24K array is otherwise mapped
    // by ADS to 64 separate 32Kx1 DRM blocks, wasting the unused depth.
    localparam int ACT_SEG_AW = ACT_AW < 13 ? ACT_AW : 13;
    localparam int ACT_SEG_WORDS = 1 << ACT_SEG_AW;
    localparam int ACT_SEGMENTS = (ACT_DEPTH + ACT_SEG_WORDS - 1) / ACT_SEG_WORDS;
    logic [63:0] act_segment_data [2*ACT_SEGMENTS];
    logic [2*ACT_SEGMENTS-1:0] act_segment_read, act_segment_select_q;
    logic act_read_enable;
    logic [7:0] width_q, height_q, out_width_q, out_height_q;
    localparam int GROUP_W = (GROUPS < 2) ? 1 : $clog2(GROUPS + 1);
    localparam int PIXEL_W = (MAX_SIDE < 2) ? 1 : $clog2(MAX_SIDE * MAX_SIDE);
    localparam int ADDR_STAGES = 5;
    logic [15:0] cin_q;
    logic [GROUP_W-1:0] groups_q, group_q;
    logic [4:0] cout_q;
    logic kernel3_q, stride2_q, pad_q;
    logic [1:0] tap_x_q, tap_y_q;
    logic [511:0] bias_q;
    logic [31:0] accumulator_q [16];
    logic [511:0] accumulation_sum, result0_q, result1_q;
    logic read_ptr_q, write_ptr_q;
    logic [1:0] result_count_q, reserved_q;
    logic [7:0] issue_x_q, issue_y_q;
    logic issued_all_q, issue_fire, issue_first, result_push;
    logic [ACT_AW-1:0] act_rd_addr;
    logic [WEIGHT_AW-1:0] weight_rd_addr;
    logic [63:0] act_rd_data, mac_activation;
    logic [1023:0] weight_rd_data, mac_weight;
    logic read_valid_q, read_first_q, read_last_q, read_padding_q;
    logic [7:0] read_mask_q;
    logic issue_last;
    logic signed [10:0] coord_x_q, coord_y_q;
    logic [PIXEL_W-1:0] row_q, x_q1, pixel_q;
    logic [ACT_AW-1:0] base_q;
    logic [GROUP_W-1:0] group_q0, group_q1, group_q2, group_q3;
    logic padding_q1, padding_q2, padding_q3, padding_q4;
    logic [ADDR_STAGES-1:0] addr_valid_q, addr_first_q, addr_last_q;
    logic [6:0] mac_first_q;
    logic [7:0] addr_mask_q [ADDR_STAGES];
    logic [WEIGHT_AW-1:0] weight_issue_q, weight_addr_q [ADDR_STAGES];
    logic mac_valid, mac_last;
    logic [511:0] mac_dot;
    int proposed_width, proposed_height;

    assign busy = state_q != IDLE;
    // 装载优先于命令。计算期间只允许写非活动 bank，复位禁止任何握手。
    // 窄无符号量先补零再转 int；ADS 2025.2 对直接 int'(x) 的扩展与 Verilator 不同。
    assign act_wr_ready = !rst && (!busy || act_wr_bank != active_bank_q) &&
                          int'({1'b0, act_wr_addr}) < ACT_DEPTH;
    assign weight_wr_ready = !rst && !busy && int'({1'b0, weight_wr_addr}) < WEIGHT_DEPTH;
    assign cmd_ready = !rst && !busy && !act_wr_valid && !weight_wr_valid;
    assign cmd_fire = cmd_valid && cmd_ready;
    assign out_valid = !rst && state_q == ACTIVE && result_count_q != 0;
    assign out_fire = out_valid && out_ready;
    assign out_data = read_ptr_q ? result1_q : result0_q;
    assign out_last = out_x == out_width_q - 1'b1 && out_y == out_height_q - 1'b1;
    // A credit is reserved before the first beat and released only on delivery.
    // It accounts for both in-flight pixels and completed FIFO entries, so no
    // downstream stall can overflow the two-entry result buffer.
    assign issue_first = group_q == 0 && tap_x_q == 0 && tap_y_q == 0;
    assign issue_fire = !rst && state_q == ACTIVE && !issued_all_q &&
                        (!issue_first || reserved_q < 2 || out_fire);
    assign result_push = !rst && mac_valid && mac_last;

    always_comb begin
        proposed_width = int'({1'b0, cmd_width}) + 2*int'({1'b0, cmd_pad}) - (cmd_kernel3 ? 3 : 1);
        proposed_height = int'({1'b0, cmd_height}) + 2*int'({1'b0, cmd_pad}) - (cmd_kernel3 ? 3 : 1);
        descriptor_ok = cmd_width > 0 && int'({1'b0, cmd_width}) <= MAX_SIDE &&
                        cmd_height > 0 && int'({1'b0, cmd_height}) <= MAX_SIDE &&
                        cmd_cin > 0 && int'({1'b0, cmd_cin}) <= MAX_CIN &&
                        cmd_cout > 0 && cmd_cout <= 16 &&
                        (cmd_kernel3 || !cmd_pad) && proposed_width >= 0 && proposed_height >= 0;
    end

    assign issue_last = group_q == groups_q - 1'b1 &&
                        tap_x_q == (kernel3_q ? 2'd2 : 2'd0) &&
                        tap_y_q == (kernel3_q ? 2'd2 : 2'd0);
    assign weight_rd_addr = weight_addr_q[ADDR_STAGES-1];

    // One address per cycle, with no cascaded coordinate/address multipliers:
    // coordinate -> row multiply -> pixel add -> group multiply -> group add.
    // Tags and the sequential weight address travel through the same five edges.
    // Padding may wrap intermediate arithmetic, but never enables an activation read.
    always_ff @(posedge clk) begin
        coord_x_q <= (11'({1'b0, issue_x_q}) << stride2_q) + 11'({1'b0, tap_x_q}) - 11'({1'b0, pad_q});
        coord_y_q <= (11'({1'b0, issue_y_q}) << stride2_q) + 11'({1'b0, tap_y_q}) - 11'({1'b0, pad_q});
        row_q <= PIXEL_W'(coord_y_q) * PIXEL_W'({1'b0, width_q});
        x_q1 <= PIXEL_W'(coord_x_q);
        padding_q1 <= coord_x_q < 0 || coord_x_q >= $signed({3'b0, width_q}) ||
                      coord_y_q < 0 || coord_y_q >= $signed({3'b0, height_q});
        pixel_q <= row_q + x_q1;
        padding_q2 <= padding_q1;
        base_q <= ACT_AW'(pixel_q) * ACT_AW'({1'b0, groups_q});
        padding_q3 <= padding_q2;
        act_rd_addr <= base_q + ACT_AW'({1'b0, group_q3});
        padding_q4 <= padding_q3;
        group_q0 <= group_q;
        group_q1 <= group_q0;
        group_q2 <= group_q1;
        group_q3 <= group_q2;
        weight_addr_q[0] <= weight_issue_q;
        for (int stage = 1; stage < ADDR_STAGES; stage++)
            weight_addr_q[stage] <= weight_addr_q[stage-1];
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            addr_valid_q <= '0;
            addr_first_q <= '0;
            addr_last_q <= '0;
            for (int stage = 0; stage < ADDR_STAGES; stage++) addr_mask_q[stage] <= '0;
            weight_issue_q <= '0;
        end else begin
            addr_valid_q <= {addr_valid_q[ADDR_STAGES-2:0], issue_fire};
            addr_first_q <= {addr_first_q[ADDR_STAGES-2:0], issue_fire && issue_first};
            addr_last_q <= {addr_last_q[ADDR_STAGES-2:0], issue_fire && issue_last};
            for (int lane = 0; lane < 8; lane++)
                addr_mask_q[0][lane] <= int'({1'b0, group_q})*8 + lane < int'({1'b0, cin_q});
            for (int stage = 1; stage < ADDR_STAGES; stage++) addr_mask_q[stage] <= addr_mask_q[stage-1];
            if (state_q == IDLE || (issue_fire && issue_last)) weight_issue_q <= '0;
            else if (issue_fire) weight_issue_q <= weight_issue_q + 1'b1;
        end
    end

    assign act_read_enable = !rst && addr_valid_q[ADDR_STAGES-1] && !padding_q4;
    for (genvar bank = 0; bank < 2; bank++) begin : g_activation_bank
        for (genvar segment = 0; segment < ACT_SEGMENTS; segment++) begin : g_segment
            localparam int SLOT = bank*ACT_SEGMENTS + segment;
            localparam int REMAINING = ACT_DEPTH - segment*ACT_SEG_WORDS;
            localparam int DEPTH = REMAINING < ACT_SEG_WORDS ? REMAINING : ACT_SEG_WORDS;
            assign act_segment_read[SLOT] = act_read_enable && active_bank_q == 1'(bank) &&
                (int'({1'b0, act_rd_addr}) >> ACT_SEG_AW) == segment;
            adam_sdp_ram #(.WIDTH(64), .DEPTH(DEPTH), .ADDR_W(ACT_SEG_AW)) u_ram (
                .clk, .wr_en(act_wr_valid && act_wr_ready && act_wr_bank == 1'(bank) &&
                            (int'({1'b0, act_wr_addr}) >> ACT_SEG_AW) == segment),
                .wr_addr(act_wr_addr[ACT_SEG_AW-1:0]), .wr_data(act_wr_data),
                .rd_en(act_segment_read[SLOT]), .rd_addr(act_rd_addr[ACT_SEG_AW-1:0]),
                .rd_data(act_segment_data[SLOT])
            );
        end
    end
    // RAM data and the one-hot selector register on the same edge. Holding the
    // selector through padding/bubbles matches the held synchronous RAM output.
    always_ff @(posedge clk) begin
        if (rst) act_segment_select_q <= '0;
        else if (act_read_enable) act_segment_select_q <= act_segment_read;
    end
    always_comb begin
        act_rd_data = '0;
        for (int slot = 0; slot < 2*ACT_SEGMENTS; slot++)
            if (act_segment_select_q[slot]) act_rd_data |= act_segment_data[slot];
    end
    adam_sdp_ram #(.WIDTH(1024), .DEPTH(WEIGHT_DEPTH), .ADDR_W(WEIGHT_AW)) u_weight_ram (
        .clk, .wr_en(weight_wr_valid && weight_wr_ready), .wr_addr(weight_wr_addr), .wr_data(weight_wr_data),
        .rd_en(!rst && addr_valid_q[ADDR_STAGES-1]), .rd_addr(weight_rd_addr), .rd_data(weight_rd_data)
    );

    // SRAM 一拍读延迟的标签；padding 不读输入 RAM，直接向 MAC 送真实的零。
    always_ff @(posedge clk) begin
        if (rst) begin
            read_valid_q <= 1'b0;
            read_first_q <= 1'b0;
            read_last_q <= 1'b0;
            read_padding_q <= 1'b0;
            read_mask_q <= '0;
        end else begin
            read_valid_q <= addr_valid_q[ADDR_STAGES-1];
            read_first_q <= addr_first_q[ADDR_STAGES-1];
            read_last_q <= addr_last_q[ADDR_STAGES-1];
            read_padding_q <= padding_q4;
            read_mask_q <= addr_mask_q[ADDR_STAGES-1];
        end
    end

    always_comb begin
        mac_activation = '0;
        mac_weight = '0;
        for (int ic = 0; ic < 8; ic++) begin
            if (read_mask_q[ic] && !read_padding_q)
                mac_activation[ic*8 +: 8] = act_rd_data[ic*8 +: 8];
            for (int oc = 0; oc < 16; oc++) begin
                if (oc < int'({1'b0, cout_q}) && read_mask_q[ic])
                    mac_weight[(oc*8+ic)*8 +: 8] = weight_rd_data[(oc*8+ic)*8 +: 8];
            end
        end
    end
    adam_mac128 u_mac (
        .clk, .rst, .in_valid(read_valid_q), .in_last(read_last_q),
        .in_activation(mac_activation), .in_weight(mac_weight),
        .out_valid(mac_valid), .out_last(mac_last), .out_dot(mac_dot)
    );

    // Pixels remain ordered in the MAC pipeline. First/last tags delimit each
    // accumulator lifetime; initializing from bias on the first result needs
    // only one INT32 adder, including the single-beat first+last case.
    always_comb begin
        for (int oc = 0; oc < 16; oc++) begin
            if (mac_first_q[6])
                accumulation_sum[oc*32 +: 32] =
                    (oc < int'({1'b0, cout_q}) ? bias_q[oc*32 +: 32] : 32'b0) + mac_dot[oc*32 +: 32];
            else accumulation_sum[oc*32 +: 32] = accumulator_q[oc] + mac_dot[oc*32 +: 32];
        end
    end
    always_ff @(posedge clk) begin
        if (rst) mac_first_q <= '0;
        else mac_first_q <= {mac_first_q[5:0], read_valid_q && read_first_q};
        if (!rst && mac_valid) begin
            for (int oc = 0; oc < 16; oc++) accumulator_q[oc] <= accumulation_sum[oc*32 +: 32];
            if (mac_last) begin
                if (write_ptr_q) result1_q <= accumulation_sum;
                else result0_q <= accumulation_sum;
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            read_ptr_q <= 1'b0;
            write_ptr_q <= 1'b0;
            result_count_q <= '0;
            reserved_q <= '0;
        end else begin
            if (result_push) write_ptr_q <= ~write_ptr_q;
            if (out_fire) read_ptr_q <= ~read_ptr_q;
            case ({result_push, out_fire})
                2'b10: result_count_q <= result_count_q + 1'b1;
                2'b01: result_count_q <= result_count_q - 1'b1;
                default: begin end
            endcase
            case ({issue_fire && issue_first, out_fire})
                2'b10: reserved_q <= reserved_q + 1'b1;
                2'b01: reserved_q <= reserved_q - 1'b1;
                default: begin end
            endcase
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            state_q <= IDLE;
            active_bank_q <= 1'b0;
            cmd_error <= 1'b0;
            done <= 1'b0;
            width_q <= '0;
            height_q <= '0;
            cin_q <= '0;
            cout_q <= '0;
            groups_q <= '0;
            group_q <= '0;
            kernel3_q <= 1'b0;
            stride2_q <= 1'b0;
            pad_q <= 1'b0;
            tap_x_q <= '0;
            tap_y_q <= '0;
            bias_q <= '0;
            out_width_q <= '0;
            out_height_q <= '0;
            out_x <= '0;
            out_y <= '0;
            issue_x_q <= '0;
            issue_y_q <= '0;
            issued_all_q <= 1'b0;
            out_mask <= '0;
            perf_mac_steps <= '0;
            perf_active_cycles <= '0;
        end else begin
            cmd_error <= 1'b0;
            done <= 1'b0;
            if (busy) perf_active_cycles <= perf_active_cycles + 1'b1;
            unique case (state_q)
                IDLE: begin
                    if (cmd_fire) begin
                        if (!descriptor_ok) begin
                            cmd_error <= 1'b1;
                        end else begin
                            width_q <= cmd_width;
                            height_q <= cmd_height;
                            cin_q <= cmd_cin;
                            cout_q <= cmd_cout;
                            groups_q <= GROUP_W'((cmd_cin + 16'd7) >> 3);
                            kernel3_q <= cmd_kernel3;
                            stride2_q <= cmd_stride2;
                            pad_q <= cmd_pad;
                            bias_q <= cmd_bias;
                            out_width_q <= 8'((proposed_width >> int'({1'b0, cmd_stride2})) + 1);
                            out_height_q <= 8'((proposed_height >> int'({1'b0, cmd_stride2})) + 1);
                            out_x <= '0;
                            out_y <= '0;
                            issue_x_q <= '0;
                            issue_y_q <= '0;
                            active_bank_q <= cmd_act_bank;
                            issued_all_q <= 1'b0;
                            group_q <= '0;
                            tap_x_q <= '0;
                            tap_y_q <= '0;
                            for (int oc = 0; oc < 16; oc++) out_mask[oc] <= oc < int'({1'b0, cmd_cout});
                            perf_mac_steps <= '0;
                            perf_active_cycles <= '0;
                            state_q <= ACTIVE;
                        end
                    end
                end
                ACTIVE: begin
                    if (issue_fire) begin
                        perf_mac_steps <= perf_mac_steps + 1'b1;
                        if (issue_last) begin
                            group_q <= '0;
                            tap_x_q <= '0;
                            tap_y_q <= '0;
                            if (issue_x_q == out_width_q - 1'b1 && issue_y_q == out_height_q - 1'b1)
                                issued_all_q <= 1'b1;
                            else if (issue_x_q == out_width_q - 1'b1) begin
                                issue_x_q <= '0;
                                issue_y_q <= issue_y_q + 1'b1;
                            end else issue_x_q <= issue_x_q + 1'b1;
                        end else if (group_q == groups_q - 1'b1) begin
                            group_q <= '0;
                            if (tap_x_q == (kernel3_q ? 2'd2 : 2'd0)) begin
                                tap_x_q <= '0;
                                tap_y_q <= tap_y_q + 1'b1;
                            end else tap_x_q <= tap_x_q + 1'b1;
                        end else begin
                            group_q <= group_q + 1'b1;
                        end
                    end
                    if (out_fire) begin
                        if (out_last) begin
                            done <= 1'b1;
                            state_q <= IDLE;
                        end else begin
                            if (out_x == out_width_q - 1'b1) begin
                                out_x <= '0;
                                out_y <= out_y + 1'b1;
                            end else begin
                                out_x <= out_x + 1'b1;
                            end
                        end
                    end
                end
                default: state_q <= IDLE;
            endcase
        end
    end
endmodule
