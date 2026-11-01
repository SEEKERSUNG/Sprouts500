// 8-bit SPI master, modes 0..3, software chip-select.
//
// Register map:
//   0x00 CTRL   RW  [0] enable, [1] cpol, [2] cpha, [3] irq enable, [4] lsb first
//   0x04 STATUS RO  [0] busy, [1] done
//   0x08 CLKDIV RW  half SCLK period in clocks - 1
//   0x0C TXDATA WO  write byte to start a transfer
//   0x10 RXDATA RO  received byte (clears DONE)
//   0x14 CS     RW  bit n = 1 asserts cs_n[n] (drives low)
//
// Software must assert CS before writing TXDATA and deassert it afterwards.

module spi (
    input  wire        clk,
    input  wire        rstn,

    input  wire        sel,
    input  wire        we,
    input  wire        rd,
    input  wire [11:0] addr,
    input  wire [31:0] wdata,
    input  wire [3:0]  wstrb,
    output wire [31:0] rdata,

    output reg         sclk,
    output reg         mosi,
    input  wire        miso,
    output wire [3:0]  cs_n,
    output wire        irq
);

    localparam [7:0] REG_CTRL    = 8'h0;
    localparam [7:0] REG_STATUS  = 8'h4;
    localparam [7:0] REG_CLKDIV  = 8'h8;
    localparam [7:0] REG_TXDATA  = 8'hc;
    localparam [7:0] REG_RXDATA  = 8'h10;
    localparam [7:0] REG_CS      = 8'h14;

    reg [31:0] ctrl;
    reg [31:0] clk_div;
    reg [3:0]  cs_reg;

    reg        busy;
    reg        done;
    reg [4:0]  step;        // 0..16 half-periods
    reg [31:0] half_cnt;
    reg [7:0]  tx_data;
    reg [7:0]  rx_shift;
    reg [7:0]  rx_data;
    reg        miso_s0;
    reg        miso_s1;

    wire [31:0] wmask = {{8{wstrb[3]}}, {8{wstrb[2]}}, {8{wstrb[1]}}, {8{wstrb[0]}}};
    wire        cpol  = ctrl[1];
    wire        cpha  = ctrl[2];
    wire        lsb   = ctrl[4];

    wire        start = we && (addr[7:0] == REG_TXDATA) && wstrb[0] &&
                        ctrl[0] && !busy;
    wire        rx_read = rd && (addr[7:0] == REG_RXDATA);

    wire [2:0] raw_bit = step[4:1];          // 0..7 for the current half-period pair
    wire [2:0] bit_idx = lsb ? raw_bit : (3'd7 - raw_bit);
    wire [2:0] trail_bit = (step >> 1) - 5'd1;   // 0..7 at even steps 2..16
    wire [2:0] trail_idx = lsb ? trail_bit : (3'd7 - trail_bit);

    assign cs_n = ~cs_reg;
    assign irq  = done && ctrl[3];

    always @(posedge clk or negedge rstn) begin
        if (!rstn) begin
            ctrl     <= 32'b0;
            clk_div  <= 32'd49;       // ~1 MHz SCLK at 100 MHz
            cs_reg   <= 4'b0;
            busy     <= 1'b0;
            done     <= 1'b0;
            step     <= 5'b0;
            half_cnt <= 32'b0;
            tx_data  <= 8'b0;
            rx_shift <= 8'b0;
            rx_data  <= 8'b0;
            sclk     <= 1'b0;
            mosi     <= 1'b0;
            miso_s0  <= 1'b0;
            miso_s1  <= 1'b0;
        end else begin
            miso_s0 <= miso;
            miso_s1 <= miso_s0;

            if (we && (addr[7:0] == REG_CTRL) && wstrb[0]) begin
                ctrl[0] <= wdata[0];
                ctrl[1] <= wdata[1];
                ctrl[2] <= wdata[2];
                ctrl[3] <= wdata[3];
                ctrl[4] <= wdata[4];
            end
            if (we && (addr[7:0] == REG_CLKDIV))
                clk_div <= (clk_div & ~wmask) | (wdata & wmask);
            if (we && (addr[7:0] == REG_CS) && wstrb[0])
                cs_reg <= wdata[3:0];

            if (rx_read)
                done <= 1'b0;

            if (!ctrl[0]) begin
                busy     <= 1'b0;
                done     <= 1'b0;
                step     <= 5'b0;
                half_cnt <= 32'b0;
                sclk     <= cpol;
                mosi     <= 1'b0;
            end else if (!busy) begin
                sclk <= cpol;
                if (start) begin
                    tx_data  <= wdata[7:0];
                    rx_shift <= 8'b0;
                    busy     <= 1'b1;
                    done     <= 1'b0;
                    step     <= 5'd1;
                    half_cnt <= 32'b0;
                    sclk     <= cpol;
                    // CPHA=0 requires data valid before the first leading edge;
                    // CPHA=1 drives it again at the leading edge.
                    mosi <= wdata[lsb ? 3'd0 : 3'd7];
                end
            end else begin
                if (half_cnt >= clk_div) begin
                    half_cnt <= 32'b0;
                    sclk     <= cpol ^ step[0];

                    if (step == 5'd17) begin
                        step     <= 5'b0;
                        busy     <= 1'b0;
                        done     <= 1'b1;
                        sclk     <= cpol;
                        rx_data  <= rx_shift;
                    end else begin
                        if (!cpha) begin
                            if (step[0]) begin
                                // leading edge: sample MISO
                                rx_shift[bit_idx] <= miso_s1;
                            end else if ((step >= 5'd2) && (step < 5'd16)) begin
                                // trailing edge: present the next bit
                                mosi <= tx_data[bit_idx];
                            end
                        end else begin
                            if (step[0]) begin
                                // leading edge: present the next bit
                                mosi <= tx_data[bit_idx];
                            end else if (step >= 5'd2) begin
                                // trailing edge: sample MISO
                                rx_shift[trail_idx] <= miso_s1;
                            end
                        end
                        step <= step + 5'd1;
                    end
                end else begin
                    half_cnt <= half_cnt + 32'd1;
                end
            end
        end
    end

    assign rdata = (addr[7:0] == REG_CTRL)   ? ctrl :
                   (addr[7:0] == REG_STATUS) ? {30'b0, done, busy} :
                   (addr[7:0] == REG_CLKDIV) ? clk_div :
                   (addr[7:0] == REG_RXDATA) ? {24'b0, rx_data} :
                   (addr[7:0] == REG_CS)     ? {28'b0, cs_reg} : 32'b0;

endmodule
