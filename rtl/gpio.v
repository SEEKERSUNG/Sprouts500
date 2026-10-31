// 32-bit GPIO with direction control and edge interrupt.
//
// Register map:
//   0x00 DIR      RW  1 = output
//   0x04 OUT      RW  output value
//   0x08 IN       RO  synchronized pin value
//   0x0C IRQ_EN   RW  per-pin interrupt enable
//   0x10 IRQ_PEND RW1C per-pin pending (write 1 to clear)

module gpio (
    input  wire        clk,
    input  wire        rstn,

    input  wire        sel,
    input  wire        we,
    input  wire        rd,
    input  wire [11:0] addr,
    input  wire [31:0] wdata,
    input  wire [3:0]  wstrb,
    output wire [31:0] rdata,

    input  wire [31:0] gpio_i,
    output wire [31:0] gpio_o,
    output wire [31:0] gpio_oe,
    output wire        irq
);

    localparam [7:0] REG_DIR    = 8'h0;
    localparam [7:0] REG_OUT    = 8'h4;
    localparam [7:0] REG_IN     = 8'h8;
    localparam [7:0] REG_IE     = 8'hc;
    localparam [7:0] REG_PEND   = 8'h10;

    reg [31:0] dir;
    reg [31:0] out;
    reg [31:0] ie;
    reg [31:0] pend;
    reg [31:0] in_s0;
    reg [31:0] in_s1;

    wire [31:0] wmask = {{8{wstrb[3]}}, {8{wstrb[2]}}, {8{wstrb[1]}}, {8{wstrb[0]}}};
    wire [31:0] in_edge = in_s0 ^ in_s1;

    always @(posedge clk or negedge rstn) begin
        if (!rstn) begin
            dir   <= 32'b0;
            out   <= 32'b0;
            ie    <= 32'b0;
            pend  <= 32'b0;
            in_s0 <= 32'b0;
            in_s1 <= 32'b0;
        end else begin
            in_s0 <= gpio_i;
            in_s1 <= in_s0;

            if (we && (addr[7:0] == REG_DIR))
                dir <= (dir & ~wmask) | (wdata & wmask);
            if (we && (addr[7:0] == REG_OUT))
                out <= (out & ~wmask) | (wdata & wmask);
            if (we && (addr[7:0] == REG_IE))
                ie <= (ie & ~wmask) | (wdata & wmask);

            // edge detect on synchronized inputs + W1C on the pending register
            pend <= (pend | (in_edge & ie)) &
                    ~((we && (addr[7:0] == REG_PEND)) ? (wdata & wmask) : 32'b0);
        end
    end

    assign rdata = (addr[7:0] == REG_DIR)  ? dir :
                   (addr[7:0] == REG_OUT)  ? out :
                   (addr[7:0] == REG_IN)   ? in_s1 :
                   (addr[7:0] == REG_IE)   ? ie :
                   (addr[7:0] == REG_PEND) ? pend : 32'b0;

    assign gpio_o  = out;
    assign gpio_oe = dir;
    assign irq     = |pend;

endmodule
