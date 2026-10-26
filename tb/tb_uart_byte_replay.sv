// Functional mapped replay: bypass ONLY UART bit serialization/deserialization.
// Never force MMIO, memory, NPU state or output data.
module tb_uart_byte_replay;
    timeunit 1ns;
    timeprecision 1ps;
    logic clk_27m = 0, board_reset_n = 0;
    logic injected_valid = 0;
    logic [7:0] injected_data = 0;
    wire uart_txd;
    logic [111:0] request_word;
    logic [95:0] expected, observed;
    int fd, rc, frames = 0, clocks = 0, captured = 0, waited;
    bit capture_enable = 0;
    string trace_path;
    npu_uart_board dut (.clk_27m, .board_reset_n, .uart_rxd(1'b1), .uart_txd);
    // ADS exports the SEND one-hot FF and PHY busy FF across the byte boundary.
    // Keeping PHY busy low replaces its serializer-ready signal, without
    // changing the bridge, bus response, NPU or any SRAM/APM state.
    wire send_byte = dut.\u_bridge/state_q_reg [4];
    task tick;
        clk_27m = 0; #5;
        if (capture_enable && send_byte === 1'b1) begin
            if (captured >= 12) $fatal(1, "extra encoded reply byte");
            observed[captured*8 +: 8] = dut.tx_data;
            captured++;
        end
        clk_27m = 1; #5; clocks++;
    endtask
    initial begin
        force dut.rx_valid = injected_valid;
        force dut.rx_error = 1'b0;
        force dut.rx_data = injected_data;
        force dut.\u_uart/tx_active_q = 1'b0;
        if (!$value$plusargs("TRACE=%s", trace_path)) $fatal(1, "missing TRACE");
        fd = $fopen(trace_path, "r");
        if (!fd) $fatal(1, "cannot open trace");
        repeat (20) tick(); board_reset_n = 1; repeat (10) tick();
        while (!$feof(fd)) begin
            rc = $fscanf(fd, "%h %h\n", request_word, expected);
            if (rc == 2) begin
                observed = 0; captured = 0; capture_enable = 1;
                for (int i = 0; i < 14; i++) begin
                    injected_valid = 1; injected_data = request_word[i*8 +: 8]; tick();
                end
                injected_valid = 0; waited = 0;
                while (captured < 12 && waited < 2048) begin tick(); waited++; end
                if (captured != 12 || observed !== expected)
                    $fatal(1, "byte replay frame %0d mismatch: got %h (%0d bytes) expected %h", frames, observed, captured, expected);
                capture_enable = 0; frames++;
                if (frames % 16 == 0) begin $display("Progress: %0d UART byte frames, %0d clocks", frames, clocks); $fflush(); end
                tick();
            end else if (!$feof(fd)) $fatal(1, "bad trace line");
        end
        $fclose(fd);
        if (frames == 0) $fatal(1, "empty trace");
        $display("PASS mapped UART byte replay: %0d request/reply frames, %0d clocks; PHY bit timing omitted", frames, clocks);
        $finish;
    end
endmodule
