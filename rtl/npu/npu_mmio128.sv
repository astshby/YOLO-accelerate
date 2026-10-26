// SPDX-License-Identifier: Apache-2.0
// Single-clock, one-response MMIO endpoint around the unchanged 128-MAC NPU.
// Byte addresses; 32-bit little-endian data. This is a local bus, not AXI.
module npu_mmio128 (
    input  logic clk,
    input  logic rst,
    input  logic req_valid,
    output logic req_ready,
    input  logic req_write,
    input  logic [15:0] req_addr,
    input  logic [31:0] req_wdata,
    input  logic [3:0] req_wstrb,
    output logic rsp_valid,
    input  logic rsp_ready,
    output logic [31:0] rsp_rdata,
    output logic rsp_error
);
    timeunit 1ns;
    timeprecision 1ps;
    localparam logic [31:0] ERR_ADDRESS = 32'h01, ERR_STATE = 32'h02,
        ERR_RANGE = 32'h04, ERR_COMMAND = 32'h08, ERR_CONFIG = 32'h10;
    localparam int ACT_WORDS = 24576, WEIGHT_WORDS = 864;
    typedef enum logic [3:0] {OP_NONE, OP_START, OP_RESET, OP_ACT, OP_WEIGHT,
        OP_CONFIG, OP_POP, OP_DONE_CLEAR, OP_ERROR_CLEAR, OP_REGISTER} operation_t;
    operation_t operation;
    typedef enum logic [1:0] {BUS_IDLE, BUS_EXECUTE, BUS_ISSUE} bus_state_t;
    bus_state_t bus_state_q;
    logic fire, execute, deferred, core_rst, device_busy, running_q, active_bank_q, done_q;
    logic request_write_q;
    logic [15:0] request_addr_q;
    logic [31:0] request_data_q;
    logic [3:0] request_strb_q;
    logic act_commit_q, weight_commit_q, config_commit_q, start_commit_q, reset_commit_q;
    logic [31:0] shape_q, options_q, act_index_q, weight_index_q, cfg_index_q, cfg_data_q;
    logic [63:0] act_data_q;
    logic [1023:0] weight_data_q;
    logic [511:0] bias_q;
    logic [31:0] errors_q, error_view, access_error, read_data, write_mask, effective_write;
    logic [31:0] output_count_q;
    logic output_valid_q;
    logic [511:0] output_raw_q;
    logic [127:0] output_int8_q;
    logic [31:0] output_meta_q;
    logic [2:0] output_flags_q;
    logic core_busy, core_done, core_cmd_error, core_cfg_error, core_lut_ready;
    logic core_out_valid, core_out_ready, output_push, output_pop;
    logic [511:0] core_raw;
    logic [127:0] core_int8;
    logic [15:0] core_mask;
    logic [7:0] core_x, core_y;
    logic [1:0] core_mode;
    logic core_last;
    logic [31:0] perf_mac, perf_conv, perf_total, perf_post;
    logic unused_act_ready, unused_weight_ready, unused_cfg_ready, unused_cmd_ready;

    function automatic logic [31:0] merge_bytes(
        input logic [31:0] old_value, input logic [31:0] new_value, input logic [3:0] strobes
    );
        for (int byte_index = 0; byte_index < 4; byte_index++)
            merge_bytes[byte_index*8 +: 8] = strobes[byte_index] ?
                new_value[byte_index*8 +: 8] : old_value[byte_index*8 +: 8];
    endfunction

    assign req_ready = !rst && bus_state_q == BUS_IDLE && (!rsp_valid || rsp_ready);
    assign fire = req_valid && req_ready;
    assign execute = bus_state_q == BUS_EXECUTE;
    assign deferred = access_error == 0 && (operation == OP_ACT || operation == OP_WEIGHT ||
        operation == OP_CONFIG || operation == OP_START || operation == OP_RESET);
    assign device_busy = running_q || core_busy;
    assign write_mask = {{8{request_strb_q[3]}}, {8{request_strb_q[2]}},
                         {8{request_strb_q[1]}}, {8{request_strb_q[0]}}};
    assign effective_write = request_data_q & write_mask;
    // RESET is always legal, including while active or with an unread result.
    // Its response survives; only external reset cancels the bus response.
    assign core_rst = rst || reset_commit_q;
    assign error_view = errors_q | (core_cmd_error ? ERR_COMMAND : 0) |
                                  (core_cfg_error ? ERR_CONFIG : 0);

    always_comb begin
        operation = OP_NONE;
        access_error = '0;
        read_data = '0;
        if (request_addr_q[1:0] != 0) access_error = ERR_ADDRESS;
        else if (request_write_q) begin
            unique case (request_addr_q)
                16'h0008: begin
                    if (effective_write == 1) begin
                        operation = OP_START;
                        if (device_busy || output_valid_q) access_error = ERR_STATE;
                    end else if (effective_write == 2) operation = OP_RESET;
                    else if (effective_write != 0) access_error = ERR_RANGE;
                end
                16'h000c: begin
                    operation = OP_DONE_CLEAR;
                    if ((effective_write & ~32'h04) != 0) access_error = ERR_RANGE;
                end
                16'h0010: begin
                    operation = OP_ERROR_CLEAR;
                    if ((effective_write & ~32'h1f) != 0) access_error = ERR_RANGE;
                end
                16'h0020, 16'h0024, 16'h0028, 16'h002c, 16'h0030, 16'h0034,
                16'h0060, 16'h0064: begin
                    operation = OP_REGISTER;
                    if (request_addr_q == 16'h0024 &&
                        (merge_bytes(options_q, request_data_q, request_strb_q) & ~32'h7ff) != 0)
                        access_error = ERR_RANGE;
                end
                16'h0038, 16'h003c, 16'h0040, 16'h0044: begin
                    if (effective_write == 1) begin
                        unique case (request_addr_q)
                            16'h0038: begin
                                operation = OP_ACT;
                                if (act_index_q[30:0] >= 31'(ACT_WORDS)) access_error = ERR_RANGE;
                                else if (device_busy && act_index_q[31] == active_bank_q)
                                    access_error = ERR_STATE;
                            end
                            16'h003c: begin
                                operation = OP_WEIGHT;
                                if (weight_index_q >= WEIGHT_WORDS) access_error = ERR_RANGE;
                                else if (device_busy) access_error = ERR_STATE;
                            end
                            16'h0040: begin
                                operation = OP_CONFIG;
                                if (cfg_index_q[31:10] != 0) access_error = ERR_RANGE;
                                else if (device_busy) access_error = ERR_STATE;
                            end
                            default: begin
                                operation = OP_POP;
                                if (!output_valid_q) access_error = ERR_STATE;
                            end
                        endcase
                    end else if (effective_write != 0) access_error = ERR_RANGE;
                end
                default: begin
                    if ((request_addr_q >= 16'h0080 && request_addr_q < 16'h00c0) ||
                        (request_addr_q >= 16'h0100 && request_addr_q < 16'h0180)) operation = OP_REGISTER;
                    else access_error = ERR_ADDRESS;
                end
            endcase
        end else begin
            unique case (request_addr_q)
                16'h0000: read_data = 32'h4e505531; // NPU1
                16'h0004: read_data = 32'h00010000;
                16'h0008, 16'h0038, 16'h003c, 16'h0040, 16'h0044: read_data = '0;
                16'h000c: read_data = {27'd0, core_lut_ready, error_view != 0,
                                       done_q || core_done, output_valid_q, device_busy};
                16'h0010: read_data = error_view;
                16'h0014: read_data = ACT_WORDS;
                16'h0018: read_data = WEIGHT_WORDS;
                16'h001c: read_data = output_count_q;
                16'h0020: read_data = shape_q;
                16'h0024: read_data = options_q;
                16'h0028: read_data = act_index_q;
                16'h002c: read_data = weight_index_q;
                16'h0030: read_data = cfg_index_q;
                16'h0034: read_data = cfg_data_q;
                16'h0048: read_data = perf_mac;
                16'h004c: read_data = perf_conv;
                16'h0050: read_data = perf_total;
                16'h0054: read_data = perf_post;
                16'h0058: begin
                    read_data = output_meta_q;
                    if (!output_valid_q) access_error = ERR_STATE;
                end
                16'h005c: read_data = output_valid_q ? {28'd0, 1'b1, output_flags_q} : 0;
                16'h0060: read_data = act_data_q[31:0];
                16'h0064: read_data = act_data_q[63:32];
                default: begin
                    if (request_addr_q >= 16'h0080 && request_addr_q < 16'h00c0)
                        read_data = bias_q[request_addr_q[5:2]*32 +: 32];
                    else if (request_addr_q >= 16'h0100 && request_addr_q < 16'h0180)
                        read_data = weight_data_q[request_addr_q[6:2]*32 +: 32];
                    else if (request_addr_q >= 16'h0200 && request_addr_q < 16'h0240) begin
                        read_data = output_raw_q[request_addr_q[5:2]*32 +: 32];
                        if (!output_valid_q) access_error = ERR_STATE;
                    end else if (request_addr_q >= 16'h0240 && request_addr_q < 16'h0250) begin
                        read_data = output_int8_q[request_addr_q[3:2]*32 +: 32];
                        if (!output_valid_q) access_error = ERR_STATE;
                    end else access_error = ERR_ADDRESS;
                end
            endcase
        end
    end

    assign output_pop = execute && access_error == 0 && operation == OP_POP;
    assign core_out_ready = !core_rst && (!output_valid_q || output_pop);
    assign output_push = core_out_valid && core_out_ready;

    npu_conv128 u_npu (
        .clk, .rst(core_rst),
        .act_wr_bank(act_index_q[31]),
        .act_wr_valid(act_commit_q),
        .act_wr_ready(unused_act_ready), .act_wr_addr(act_index_q[14:0]), .act_wr_data(act_data_q),
        .weight_wr_valid(weight_commit_q),
        .weight_wr_ready(unused_weight_ready), .weight_wr_addr(weight_index_q[9:0]), .weight_wr_data(weight_data_q),
        .cfg_valid(config_commit_q), .cfg_ready(unused_cfg_ready),
        .cfg_kind(cfg_index_q[9:8]), .cfg_addr(cfg_index_q[7:0]), .cfg_data(cfg_data_q),
        .cfg_error(core_cfg_error), .lut_ready(core_lut_ready),
        .cmd_valid(start_commit_q), .cmd_ready(unused_cmd_ready),
        .cmd_act_bank(options_q[10]), .cmd_mode(options_q[9:8]), .cmd_width(shape_q[7:0]),
        .cmd_height(shape_q[15:8]), .cmd_cin(shape_q[31:16]), .cmd_cout(options_q[4:0]),
        .cmd_kernel3(options_q[5]), .cmd_stride2(options_q[6]), .cmd_pad(options_q[7]), .cmd_bias(bias_q),
        .cmd_error(core_cmd_error), .busy(core_busy), .done(core_done), .out_valid(core_out_valid),
        .out_ready(core_out_ready), .out_mode(core_mode), .out_raw(core_raw), .out_int8(core_int8),
        .out_mask(core_mask), .out_x(core_x), .out_y(core_y), .out_last(core_last),
        .perf_mac_steps(perf_mac), .perf_conv_cycles(perf_conv),
        .perf_total_cycles(perf_total), .perf_post_stalls(perf_post)
    );

    always_ff @(posedge clk) begin
        if (rst) begin
            bus_state_q <= BUS_IDLE;
            request_write_q <= 1'b0;
            request_addr_q <= '0;
            request_data_q <= '0;
            request_strb_q <= '0;
            act_commit_q <= 1'b0;
            weight_commit_q <= 1'b0;
            config_commit_q <= 1'b0;
            start_commit_q <= 1'b0;
            reset_commit_q <= 1'b0;
            rsp_valid <= 1'b0;
            rsp_rdata <= '0;
            rsp_error <= 1'b0;
        end else begin
            act_commit_q <= 1'b0;
            weight_commit_q <= 1'b0;
            config_commit_q <= 1'b0;
            start_commit_q <= 1'b0;
            reset_commit_q <= 1'b0;
            if (rsp_ready) rsp_valid <= 1'b0;
            unique case (bus_state_q)
                BUS_IDLE: if (fire) begin
                    request_write_q <= req_write;
                    request_addr_q <= req_addr;
                    request_data_q <= req_wdata;
                    request_strb_q <= req_wstrb;
                    bus_state_q <= BUS_EXECUTE;
                end
                BUS_EXECUTE: begin
                    rsp_rdata <= request_write_q || access_error != 0 ? 0 : read_data;
                    rsp_error <= access_error != 0;
                    if (deferred) begin
                        act_commit_q <= operation == OP_ACT;
                        weight_commit_q <= operation == OP_WEIGHT;
                        config_commit_q <= operation == OP_CONFIG;
                        start_commit_q <= operation == OP_START;
                        reset_commit_q <= operation == OP_RESET;
                        bus_state_q <= BUS_ISSUE;
                    end else begin
                        rsp_valid <= 1'b1;
                        bus_state_q <= BUS_IDLE;
                    end
                end
                BUS_ISSUE: begin
                    // Core side effects commit on this edge. Staging is held
                    // throughout EXECUTE/ISSUE; no later request can overwrite
                    // the payload before it is consumed. Acknowledge only now.
                    rsp_valid <= 1'b1;
                    bus_state_q <= BUS_IDLE;
                end
                default: bus_state_q <= BUS_IDLE;
            endcase
        end
    end

    always_ff @(posedge clk) begin
        if (core_rst) begin
            shape_q <= 32'h00080101; // 1x1, Ci8
            options_q <= 32'h00000010; // Co16, raw, bank0
            act_index_q <= '0;
            weight_index_q <= '0;
            cfg_index_q <= '0;
            cfg_data_q <= '0;
            act_data_q <= '0;
            weight_data_q <= '0;
            bias_q <= '0;
            errors_q <= '0;
            done_q <= 1'b0;
            running_q <= 1'b0;
            active_bank_q <= 1'b0;
            output_valid_q <= 1'b0;
            output_raw_q <= '0;
            output_int8_q <= '0;
            output_meta_q <= '0;
            output_flags_q <= '0;
            output_count_q <= '0;
        end else begin
            // New errors win over W1C, including a core error arriving that cycle.
            errors_q <= (errors_q & ~(execute && access_error == 0 && operation == OP_ERROR_CLEAR ?
                                      effective_write : 0)) |
                        (execute ? access_error : 0) |
                        (core_cmd_error ? ERR_COMMAND : 0) | (core_cfg_error ? ERR_CONFIG : 0);
            if (execute && access_error == 0 && operation == OP_DONE_CLEAR && effective_write[2]) done_q <= 1'b0;
            if (core_done) begin done_q <= 1'b1; running_q <= 1'b0; end
            if (core_cmd_error) running_q <= 1'b0;
            if (execute && access_error == 0 && operation == OP_START) begin
                running_q <= 1'b1;
                active_bank_q <= options_q[10];
                done_q <= 1'b0;
                output_count_q <= '0;
            end
            if (output_pop) output_valid_q <= 1'b0;
            if (output_push) begin
                output_valid_q <= 1'b1;
                output_raw_q <= core_raw;
                output_int8_q <= core_int8;
                output_meta_q <= {core_y, core_x, core_mask};
                output_flags_q <= {core_last, core_mode};
                output_count_q <= output_count_q + 32'd1;
            end
            if (execute && access_error == 0 && operation == OP_REGISTER) begin
                unique case (request_addr_q)
                    16'h0020: shape_q <= merge_bytes(shape_q, request_data_q, request_strb_q);
                    16'h0024: options_q <= merge_bytes(options_q, request_data_q, request_strb_q);
                    16'h0028: act_index_q <= merge_bytes(act_index_q, request_data_q, request_strb_q);
                    16'h002c: weight_index_q <= merge_bytes(weight_index_q, request_data_q, request_strb_q);
                    16'h0030: cfg_index_q <= merge_bytes(cfg_index_q, request_data_q, request_strb_q);
                    16'h0034: cfg_data_q <= merge_bytes(cfg_data_q, request_data_q, request_strb_q);
                    16'h0060: act_data_q[31:0] <= merge_bytes(act_data_q[31:0], request_data_q, request_strb_q);
                    16'h0064: act_data_q[63:32] <= merge_bytes(act_data_q[63:32], request_data_q, request_strb_q);
                    default: begin
                        if (request_addr_q >= 16'h0080 && request_addr_q < 16'h00c0)
                            bias_q[request_addr_q[5:2]*32 +: 32] <=
                                merge_bytes(bias_q[request_addr_q[5:2]*32 +: 32], request_data_q, request_strb_q);
                        else weight_data_q[request_addr_q[6:2]*32 +: 32] <=
                                merge_bytes(weight_data_q[request_addr_q[6:2]*32 +: 32], request_data_q, request_strb_q);
                    end
                endcase
            end
        end
    end
endmodule
