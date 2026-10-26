// Functional mapped replay: bypass ONLY UART bit serialization/deserialization.
// GPLL is a test-only interface stub; force its clock/LOCK boundary explicitly.
// Never force MMIO, memory, NPU state or output data.
module tb_uart_byte_replay;
    timeunit 1ns;
    timeprecision 1ps;
    logic clk_27m = 0, board_reset_n = 0;
    logic injected_valid = 0;
    logic replay_clock = 0, replay_locked = 0;
    logic [7:0] injected_data = 0;
    wire uart_txd;
    logic [1135:0] request_word;
    logic [95:0] expected, observed;
    int fd, rc, frames = 0, clocks = 0, captured = 0, waited, request_bytes;
    bit variable_requests;
    bit capture_enable = 0;
    string trace_path;
    npu_uart_board100 dut (.clk_27m, .board_reset_n, .uart_rxd(1'b1), .uart_txd);
    // ADS exports the SEND one-hot FF and PHY busy FF across the byte boundary.
    // Keeping PHY busy low replaces its serializer-ready signal, without
    // changing the bridge, bus response, NPU or any SRAM/APM state.
    wire send_byte = dut.u_system.\u_bridge/state_q_reg [5];
    task tick;
        replay_clock = 0; #5;
        if (capture_enable && send_byte === 1'b1) begin
            if (captured >= 12) $fatal(1, "extra encoded reply byte");
            observed[captured*8 +: 8] = dut.u_system.tx_data;
            captured++;
        end
        replay_clock = 1; #5; clocks++;
    endtask
    initial begin
        $display("Mapped replay initialized at %0t", $time); $fflush();
        force dut.clk_100m = replay_clock;
        force dut.pll_locked = replay_locked;
        force dut.u_system.rx_valid = injected_valid;
        force dut.u_system.rx_error = 1'b0;
        force dut.u_system.rx_data = injected_data;
        force dut.u_system.\u_uart/tx_active_q = 1'b0;
        variable_requests = $test$plusargs("VARIABLE");
        if (!$value$plusargs("TRACE=%s", trace_path)) $fatal(1, "missing TRACE");
        fd = $fopen(trace_path, "r");
        if (!fd) $fatal(1, "cannot open trace");
        repeat (20) tick(); board_reset_n = 1; repeat (8) tick();
        if (dut.u_system.reset_release_q !== 3'b111) $fatal(1, "reset escaped PLL lock gate");
        replay_locked = 1; repeat (10) tick();
        $display("Mapped replay reset released, starting frames"); $fflush();
        while (!$feof(fd)) begin
            request_bytes = 14;
            if (variable_requests) rc = $fscanf(fd, "%d %h %h\n", request_bytes, request_word, expected);
            else rc = $fscanf(fd, "%h %h\n", request_word, expected);
            if (rc == (variable_requests ? 3 : 2)) begin
                if (request_bytes != 14 && request_bytes != 142) $fatal(1, "bad request byte count");
                observed = 0; captured = 0; capture_enable = 1;
                for (int i = 0; i < request_bytes; i++) begin
                    injected_valid = 1; injected_data = request_word[i*8 +: 8]; tick();
                end
                injected_valid = 0; waited = 0;
                while (captured < 12 && waited < 2048) begin tick(); waited++; end
                if (captured != 12 || observed !== expected)
                    $fatal(1, "byte replay frame %0d mismatch: got %h (%0d bytes) expected %h", frames, observed, captured, expected);
                capture_enable = 0; frames++;
                if (frames <= 3 || frames % 16 == 0) begin $display("Progress: %0d UART byte frames, %0d clocks", frames, clocks); $fflush(); end
                tick();
            end else if (!$feof(fd)) $fatal(1, "bad trace line");
        end
        $fclose(fd);
        if (frames == 0) $fatal(1, "empty trace");
        replay_locked = 0; #1;
        if (dut.u_system.reset_release_q !== 3'b111) $fatal(1, "PLL loss did not assert reset");
        repeat (4) tick(); replay_locked = 1; repeat (2) tick();
        if (dut.u_system.reset_release_q[2] !== 1'b1) $fatal(1, "PLL reset released early");
        tick();
        if (dut.u_system.reset_release_q[2] !== 1'b0) $fatal(1, "PLL reset release missing");
        $display("PASS mapped UART byte replay: %0d request/reply frames, %0d clocks; PHY bit timing omitted", frames, clocks);
        $finish;
    end
endmodule
