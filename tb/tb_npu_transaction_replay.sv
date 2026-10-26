// Module: tb_npu_transaction_replay
// Description: Check full mapped NPU logic while bypassing only serial measurement shifts.
module tb_npu_transaction_replay;
    timeunit 1ns;
    timeprecision 1ps;
    logic clk = 0, rst = 1, scan_update = 0, scan_capture = 0;
    wire scan_dout;
    logic [1715:0] drive = '0, word_value;
    logic [812:0] expected;
    wire [684:0] observed = {scan_dout, dut.snapshot_q[811:128]};
    string trace_path;
    reg [7:0] operation;
    int fd, rc, tries, requests = 0, captures = 0, cycles = 0;
    npu_measure_top dut (.clk, .rst, .scan_shift(1'b0), .scan_update,
                         .scan_capture, .scan_din(1'b0), .scan_dout);
    task tick;
        clk = 0; #5; clk = 1; #5; cycles++;
    endtask
    initial begin
        // 只替代测量壳的串行移位，不强制任何 NPU 数据、RAM、APM 或控制状态。
        force dut.input_shift_q = drive;
        if (!$value$plusargs("TRACE=%s", trace_path)) $fatal(1, "missing TRACE");
        fd = $fopen(trace_path, "r");
        if (!fd) $fatal(1, "cannot open TRACE");
        tick(); tick(); rst = 0;
        while (!$feof(fd)) begin
            rc = $fscanf(fd, "%c %h\n", operation, word_value);
            if (rc == 2) begin
                if (operation == "D") begin
                    drive = word_value;
                    scan_update = 1; tick(); scan_update = 0; tick();
                    requests++;
                end else if (operation == "C") begin
                    expected = word_value[812:0];
                    tries = 0;
                    scan_capture = 1;
                    tick();
                    while (expected[803] && dut.snapshot_q[803] !== 1'b1 && tries < 4096) begin
                        tick(); tries++;
                    end
                    scan_capture = 0;
                    // 删除扫描空拍会改变性能计数器；数据、状态、坐标和握手仍逐位核对。
                    // PDS 将最高位的 Q 直接接到输出缓冲，snapshot_q[812] 别名已无驱动。
                    if (observed !== expected[812:128])
                        $fatal(1, "mapped transaction mismatch after request %0d capture %0d: got %h expected %h",
                               requests, captures, observed, expected[812:128]);
                    captures++;
                    if (captures % 16 == 0) begin
                        $display("Progress: %0d requests, %0d snapshots, %0d clocks", requests, captures, cycles);
                        $fflush();
                    end
                end else $fatal(1, "unknown operation");
            end else if (!$feof(fd)) $fatal(1, "malformed TRACE");
        end
        $fclose(fd);
        if (!requests || !captures) $fatal(1, "empty TRACE");
        $display("PASS mapped NPU transaction replay: %0d requests, %0d snapshots, %0d clocks; only scan shifts and performance-counter comparison omitted",
                 requests, captures, cycles);
        $finish;
    end
endmodule
