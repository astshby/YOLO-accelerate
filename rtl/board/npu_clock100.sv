// SPDX-License-Identifier: Apache-2.0
// Logos2 GPLL: PFD=27 MHz, VCO=27*25=675 MHz, CLKOUT0=675/6.75=100 MHz.
// Fractional divider 6+6/8 has symmetric 3+3/8 VCO-cycle high/low pulses.
module npu_clock100 (
    input wire clk_27m, reset_n,
    output wire clk_100m, locked
);
    timeunit 1ns;
    timeprecision 1ps;
    GTP_GPLL #(
        .CLKIN_FREQ(27.0), .STATIC_RATIOI(1), .STATIC_RATIOM(1),
        .STATIC_RATIOF(25.0), .STATIC_DUTYF(25),
        .STATIC_RATIO0(6.75), .STATIC_DUTY0(6),
        .STATIC_RATIO1(10), .STATIC_DUTY1(10),
        .STATIC_RATIO2(10), .STATIC_DUTY2(10),
        .STATIC_RATIO3(10), .STATIC_DUTY3(10),
        .STATIC_RATIO4(10), .STATIC_DUTY4(10),
        .STATIC_RATIO5(10), .STATIC_DUTY5(10),
        .STATIC_RATIO6(10), .STATIC_DUTY6(10),
        .INTERNAL_FB("CLKOUTF"), .EXTERNAL_FB("DISABLE")
    ) u_gpll (
        .CLKIN1(clk_27m), .CLKIN2(1'b0), .CLKIN_SEL(1'b0), .CLKFB(1'b0),
        .RST(!reset_n), .PLL_PWD(1'b0), .CLKOUT0(clk_100m), .LOCK(locked),
        .CLKOUT0N(), .CLKOUT1(), .CLKOUT1N(), .CLKOUT2(), .CLKOUT2N(),
        .CLKOUT3(), .CLKOUT3N(), .CLKOUT4(), .CLKOUT5(), .CLKOUT6(),
        .CLKOUTF(), .CLKOUTFN(), .DPS_DONE(), .APB_RDATA(), .APB_READY(),
        .DPS_CLK(1'b0), .DPS_EN(1'b0), .DPS_DIR(1'b0),
        .CLKOUT0_SYN(1'b0), .CLKOUT1_SYN(1'b0), .CLKOUT2_SYN(1'b0),
        .CLKOUT3_SYN(1'b0), .CLKOUT4_SYN(1'b0), .CLKOUT5_SYN(1'b0),
        .CLKOUT6_SYN(1'b0), .CLKOUTF_SYN(1'b0),
        .APB_CLK(1'b0), .APB_RST_N(1'b0), .APB_ADDR(5'd0),
        .APB_SEL(1'b0), .APB_EN(1'b0), .APB_WRITE(1'b0), .APB_WDATA(16'd0)
    );
endmodule
