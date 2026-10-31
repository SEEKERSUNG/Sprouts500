// Deterministic fake bus slave used by the SoC cocotb tests.
//
// The real SoC has no external bus ports, so tb_soc_bus exposes the core/bus
// interconnect and muxes an external master onto it.  Addresses with
// addr[31:28] >= 3 (i.e. 0x3000_0000 and above) are handled by this slave:
//
//   - 64 words at 0x3000_0000..0x3000_00FF, aliasing every 256 bytes through
//     addr[7:2]; reset value of word i is 0xA5A5_0000 + i;
//   - byte-lane masked writes (wstrb), so sb/sh/sw can be checked;
//   - reads are combinational, writes commit on the one-cycle `first` strobe;
//   - `ready` is low while `hold` is high, which lets tests stall the bus.
//
// The slave is intentionally simple and independent from the RTL peripherals.
`timescale 1ns/1ps

module fake_slave #(
    parameter WORDS = 64
) (
    input  wire        clk,
    input  wire        rstn,

    input  wire        first,   // req && !req_q, one-cycle strobe
    input  wire        we,
    input  wire        hit,     // address belongs to this slave
    input  wire        hold,    // level: delay ready

    input  wire [31:0] addr,
    input  wire [31:0] wdata,
    input  wire [3:0]  wstrb,

    output wire [31:0] rdata,
    output wire        ready,

    output reg         wr,      // registered write pulse (observability)
    output reg  [31:0] wr_addr,
    output reg  [31:0] wr_wdata,
    output reg  [3:0]  wr_wstrb
);

    reg [31:0] mem [0:WORDS-1];
    reg [31:0] merged;
    wire [5:0] idx = addr[7:2];

    integer i;
    // Time-0 initialization so reads on the very first cycle still work.
    initial begin
        for (i = 0; i < WORDS; i = i + 1)
            mem[i] = 32'hA5A5_0000 + i;
    end
    always @(posedge clk or negedge rstn) begin
        if (!rstn) begin
            for (i = 0; i < WORDS; i = i + 1)
                mem[i] <= 32'hA5A5_0000 + i;
            wr       <= 1'b0;
            wr_addr  <= 32'b0;
            wr_wdata <= 32'b0;
            wr_wstrb <= 4'b0;
        end else begin
            wr <= first && we && hit;
            if (first && we && hit) begin
                wr_addr  <= addr;
                wr_wdata <= wdata;
                wr_wstrb <= wstrb;
                // Icarus mis-handles nonblocking byte-slice writes into a
                // memory word, so merge into a temporary word and write the
                // whole word once.
                merged = mem[idx];
                if (wstrb[0]) merged[7:0]   = wdata[7:0];
                if (wstrb[1]) merged[15:8]  = wdata[15:8];
                if (wstrb[2]) merged[23:16] = wdata[23:16];
                if (wstrb[3]) merged[31:24] = wdata[31:24];
                mem[idx] <= merged;
            end
        end
    end

    assign rdata = mem[idx];
    assign ready = !hold;

endmodule
