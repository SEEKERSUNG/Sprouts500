// 8N1 UART with configurable baud divider.
//
// Register map:
//   0x00 CTRL   RW  [0] tx enable, [1] rx enable, [2] tx irq, [3] rx irq
//   0x04 STATUS RO  [0] tx busy, [1] rx valid, [2] tx done, [3] rx overrun
//   0x08 BAUD   RW  clocks per bit - 1
//   0x0C TXDATA WO  write byte to transmit
//   0x10 RXDATA RO  read received byte (clears RX_VALID)
//
// TX must be enabled, and software should wait for TX_BUSY=0 before writing
// a new byte. RX is sampled at mid-bit with a two-flop synchronizer.

module uart (
    input  wire        clk,
    input  wire        rstn,

    input  wire        sel,
    input  wire        we,
    input  wire        rd,
    input  wire [11:0] addr,
    input  wire [31:0] wdata,
    input  wire [3:0]  wstrb,
    output wire [31:0] rdata,

    output reg         txd,
    input  wire        rxd,
    output wire        irq
);

    localparam [7:0] REG_CTRL    = 8'h0;
    localparam [7:0] REG_STATUS  = 8'h4;
    localparam [7:0] REG_BAUD    = 8'h8;
    localparam [7:0] REG_TXDATA  = 8'hc;
    localparam [7:0] REG_RXDATA  = 8'h10;

    localparam [1:0] ST_IDLE  = 2'd0;
    localparam [1:0] ST_START = 2'd1;
    localparam [1:0] ST_DATA  = 2'd2;
    localparam [1:0] ST_STOP  = 2'd3;

    reg [31:0] ctrl;
    reg [31:0] baud_div;

    // TX
    reg [1:0]  tx_state;
    reg [31:0] tx_baud_cnt;
    reg [2:0]  tx_bit;
    reg [7:0]  tx_data;
    reg        tx_busy;
    reg        tx_done;

    // RX
    reg [1:0]  rx_state;
    reg [31:0] rx_cnt;
    reg [2:0]  rx_bit;
    reg [7:0]  rx_shift;
    reg [7:0]  rx_data;
    reg        rx_busy;
    reg        rx_valid;
    reg        rx_overrun;
    reg        rx_s0;
    reg        rx_s1;

    wire [31:0] wmask = {{8{wstrb[3]}}, {8{wstrb[2]}}, {8{wstrb[1]}}, {8{wstrb[0]}}};
    wire        tx_start = we && (addr[7:0] == REG_TXDATA) && wstrb[0] &&
                           ctrl[0] && !tx_busy;
    wire        rx_read  = rd && (addr[7:0] == REG_RXDATA);
    wire        rx_fall  = rx_s1 && !rx_s0;

    always @(posedge clk or negedge rstn) begin
        if (!rstn) begin
            ctrl        <= 32'b0;
            baud_div    <= 32'd433;   // 50 MHz / 115200 - 1
            tx_state    <= ST_IDLE;
            tx_baud_cnt <= 32'b0;
            tx_bit      <= 3'b0;
            tx_data     <= 8'b0;
            tx_busy     <= 1'b0;
            tx_done     <= 1'b0;
            txd         <= 1'b1;
            rx_state    <= ST_IDLE;
            rx_cnt      <= 32'b0;
            rx_bit      <= 3'b0;
            rx_shift    <= 8'b0;
            rx_data     <= 8'b0;
            rx_busy     <= 1'b0;
            rx_valid    <= 1'b0;
            rx_overrun  <= 1'b0;
            rx_s0       <= 1'b1;
            rx_s1       <= 1'b1;
        end else begin
            rx_s0 <= rxd;
            rx_s1 <= rx_s0;

            // register writes
            if (we && (addr[7:0] == REG_CTRL) && wstrb[0]) begin
                ctrl[0] <= wdata[0];
                ctrl[1] <= wdata[1];
                ctrl[2] <= wdata[2];
                ctrl[3] <= wdata[3];
            end
            if (we && (addr[7:0] == REG_BAUD))
                baud_div <= (baud_div & ~wmask) | (wdata & wmask);

            // ------------------------------------------------------------------
            // TX
            // ------------------------------------------------------------------
            if (!ctrl[0]) begin
                tx_state    <= ST_IDLE;
                tx_baud_cnt <= 32'b0;
                tx_bit      <= 3'b0;
                tx_busy     <= 1'b0;
                tx_done     <= 1'b0;
                txd         <= 1'b1;
            end else begin
                case (tx_state)
                    ST_IDLE: begin
                        txd <= 1'b1;
                        if (tx_start) begin
                            tx_data     <= wdata[7:0];
                            tx_busy     <= 1'b1;
                            tx_done     <= 1'b0;
                            tx_bit      <= 3'b0;
                            tx_baud_cnt <= 32'b0;
                            tx_state    <= ST_START;
                        end
                    end
                    ST_START: begin
                        txd <= 1'b0;
                        if (tx_baud_cnt >= baud_div) begin
                            tx_baud_cnt <= 32'b0;
                            tx_state    <= ST_DATA;
                            txd         <= tx_data[0];
                        end else begin
                            tx_baud_cnt <= tx_baud_cnt + 32'd1;
                        end
                    end
                    ST_DATA: begin
                        if (tx_baud_cnt >= baud_div) begin
                            tx_baud_cnt <= 32'b0;
                            if (tx_bit == 3'd7) begin
                                tx_state <= ST_STOP;
                                txd      <= 1'b1;
                            end else begin
                                tx_bit <= tx_bit + 3'd1;
                                txd    <= tx_data[tx_bit + 3'd1];
                            end
                        end else begin
                            tx_baud_cnt <= tx_baud_cnt + 32'd1;
                        end
                    end
                    ST_STOP: begin
                        txd <= 1'b1;
                        if (tx_baud_cnt >= baud_div) begin
                            tx_baud_cnt <= 32'b0;
                            tx_state    <= ST_IDLE;
                            tx_busy     <= 1'b0;
                            tx_done     <= 1'b1;
                        end else begin
                            tx_baud_cnt <= tx_baud_cnt + 32'd1;
                        end
                    end
                    default: tx_state <= ST_IDLE;
                endcase
            end

            // ------------------------------------------------------------------
            // RX
            // ------------------------------------------------------------------
            if (!ctrl[1]) begin
                rx_state <= ST_IDLE;
                rx_busy  <= 1'b0;
                rx_cnt   <= 32'b0;
                rx_bit   <= 3'b0;
            end else begin
                case (rx_state)
                    ST_IDLE: begin
                        rx_busy <= 1'b0;
                        if (rx_fall) begin
                            rx_busy  <= 1'b1;
                            rx_state <= ST_START;
                            rx_cnt   <= baud_div >> 1;
                            rx_bit   <= 3'b0;
                            rx_shift <= 8'b0;
                        end
                    end
                    ST_START: begin
                        if (rx_cnt == 32'b0) begin
                            rx_cnt <= baud_div;
                            if (!rx_s1)
                                rx_state <= ST_DATA;
                            else
                                rx_state <= ST_IDLE;   // false start
                        end else begin
                            rx_cnt <= rx_cnt - 32'd1;
                        end
                    end
                    ST_DATA: begin
                        if (rx_cnt == 32'b0) begin
                            rx_cnt <= baud_div;
                            rx_shift[rx_bit] <= rx_s1;
                            if (rx_bit == 3'd7)
                                rx_state <= ST_STOP;
                            else
                                rx_bit <= rx_bit + 3'd1;
                        end else begin
                            rx_cnt <= rx_cnt - 32'd1;
                        end
                    end
                    ST_STOP: begin
                        if (rx_cnt == 32'b0) begin
                            rx_state <= ST_IDLE;
                            rx_busy  <= 1'b0;
                            if (rx_s1) begin
                                if (rx_valid)
                                    rx_overrun <= 1'b1;
                                rx_data  <= rx_shift;
                                rx_valid <= 1'b1;
                            end else begin
                                rx_overrun <= 1'b1;   // framing error
                            end
                        end else begin
                            rx_cnt <= rx_cnt - 32'd1;
                        end
                    end
                    default: rx_state <= ST_IDLE;
                endcase
            end

            // reading RXDATA clears the status flags
            if (rx_read) begin
                rx_valid   <= 1'b0;
                rx_overrun <= 1'b0;
            end
        end
    end

    assign rdata = (addr[7:0] == REG_CTRL)   ? ctrl :
                   (addr[7:0] == REG_STATUS) ? {28'b0, rx_overrun, tx_done, rx_valid, tx_busy} :
                   (addr[7:0] == REG_BAUD)   ? baud_div :
                   (addr[7:0] == REG_RXDATA) ? {24'b0, rx_data} : 32'b0;

    assign irq = (tx_done && ctrl[2]) || (rx_valid && ctrl[3]);

endmodule
