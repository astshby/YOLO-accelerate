module tb_npu_clock100;
    timeunit 1ns;
    timeprecision 1fs;
    logic clk_27m = 0, reset_n = 0;
    wire clk_100m, locked, uart_txd;
    realtime rise_time, previous_rise, high_time;
    int checked = 0;
    npu_uart_board100 dut (.clk_27m, .board_reset_n(reset_n), .uart_rxd(1'b1), .uart_txd);
    assign clk_100m = dut.clk_100m;
    assign locked = dut.pll_locked;
    initial begin
        #10; $display("PLL model initialized at %0t", $time);
        #200; $display("PLL reference running at %0t; lock=%b", $time, locked);
        #2000; $display("PLL acquisition at %0t; lock=%b", $time, locked);
    end
    always #18.5185185 clk_27m = !clk_27m;
    task check_locked_clock;
        // With KEY0 released, an unlocked PLL must still hold the system reset.
        if (dut.u_system.rst !== 1'b1) $fatal(1, "system escaped reset before PLL lock");
        wait (locked === 1'b1);
        if (dut.u_system.rst !== 1'b1) $fatal(1, "system reset released at lock edge");
        repeat (2) begin
            @(posedge clk_100m); #0.001;
            if (dut.u_system.rst !== 1'b1) $fatal(1, "reset release too early");
        end
        @(posedge clk_100m); #0.001;
        if (dut.u_system.rst !== 1'b0) $fatal(1, "reset did not release on third system clock");
        if (uart_txd !== 1'b1) $fatal(1, "UART TX not idle after reset release");
        repeat (10) @(posedge clk_100m);
        previous_rise = $realtime;
        repeat (100) begin
            @(negedge clk_100m); high_time = $realtime-previous_rise;
            if (high_time < 4.99 || high_time > 5.01) $fatal(1, "PLL high width %f", high_time);
            @(posedge clk_100m); rise_time = $realtime;
            if (rise_time-previous_rise < 9.99 || rise_time-previous_rise > 10.01)
                $fatal(1, "PLL period %f", rise_time-previous_rise);
            previous_rise = rise_time; checked++;
        end
    endtask
    initial begin
        #100; reset_n = 1;
        check_locked_clock();
        reset_n = 0; #0.001;
        if (dut.u_system.rst !== 1'b1) $fatal(1, "KEY0 did not asynchronously assert system reset");
        #100;
        if (locked !== 1'b0) $fatal(1, "PLL did not lose lock under reset");
        reset_n = 1;
        check_locked_clock();
        $display("PASS real GTP_GPLL board model: %0d 100MHz periods/high widths; lock gating, three-clock reset release, KEY0 reset and relock", checked);
        $finish;
    end
    initial begin #1000000; $fatal(1, "PLL lock timeout"); end
endmodule
