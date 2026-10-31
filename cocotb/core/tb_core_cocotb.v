`timescale 1ns/1ps

// Core-only cocotb testbench.
//
// All stimulus (clock, reset, bus slave response, interrupt levels) comes from
// cocotb.  ITCM/DTCM are accessed hierarchically from Python via the VPI
// memory array handles (`dut.ITCM[i]`, `dut.DTCM[i]`), so no program-loading
// port is needed.  Parameters are deliberately small (16 KiB ITCM / 4 KiB
// DTCM) so that a full memory scoreboard stays cheap; the halt mailbox is the
// last ITCM word (byte 0x0000_3FFC).
module tb_core_cocotb (
    input  wire        clk,
    input  wire        rstn,

    input  wire        bus_ready,
    input  wire [31:0] bus_rdata,

    input  wire        timer_irq,
    input  wire        ext_irq,
    input  wire        soft_irq,

    output wire        bus_req,
    output wire        bus_we,
    output wire [31:0] bus_addr,
    output wire [31:0] bus_wdata,
    output wire [3:0]  bus_wstrb
);

    core #(
        .RESET_PC  (32'h0000_0000),
        .ITCM_WORDS(4096),
        .DTCM_WORDS(1024)
    ) dut (
        .clk        (clk),
        .rstn       (rstn),
        .bus_req_o  (bus_req),
        .bus_we_o   (bus_we),
        .bus_addr_o (bus_addr),
        .bus_wdata_o(bus_wdata),
        .bus_wstrb_o(bus_wstrb),
        .bus_rdata_i(bus_rdata),
        .bus_ready_i(bus_ready),
        .timer_irq_i(timer_irq),
        .ext_irq_i  (ext_irq),
        .soft_irq_i (soft_irq)
    );

endmodule
