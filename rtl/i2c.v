// Byte-oriented I2C master with open-drain SCL/SDA.
//
// Register map:
//   0x00 CTRL   RW  [0] enable, [1] done irq enable
//   0x04 STATUS RW  [0] busy (RO), [1] done (W1C), [2] NACK (W1C),
//                   [3] bus active (RO), [4] SCL (RO), [5] SDA (RO)
//   0x08 CLKDIV RW  SCL half period in clocks - 1 (100 kHz: 499 at 100 MHz)
//   0x0C TXDATA RW  byte used by the next WRITE command
//   0x10 RXDATA RO  last received byte
//   0x14 CMD    WO  [0] START, [1] STOP, [2] READ, [3] WRITE, [4] NACK
//
// Software supplies the 7-bit address and R/W bit as the first TX byte. READ
// and WRITE are mutually exclusive. START-only and STOP-only commands are
// supported; a read acknowledges by default and NACK=1 is used for the last
// byte. DONE and NACK are also cleared by the next accepted command.
// SCL clock stretching is honoured. Multi-master arbitration and 10-bit
// addressing are not implemented.

module i2c (
    input  wire        clk,
    input  wire        rstn,

    input  wire        sel,
    input  wire        we,
    input  wire        rd,
    input  wire [11:0] addr,
    input  wire [31:0] wdata,
    input  wire [3:0]  wstrb,
    output wire [31:0] rdata,

    input  wire        scl_i,
    input  wire        sda_i,
    output reg         scl_oe,
    output reg         sda_oe,
    output wire        irq
);

    localparam [7:0] REG_CTRL   = 8'h00;
    localparam [7:0] REG_STATUS = 8'h04;
    localparam [7:0] REG_CLKDIV = 8'h08;
    localparam [7:0] REG_TXDATA = 8'h0c;
    localparam [7:0] REG_RXDATA = 8'h10;
    localparam [7:0] REG_CMD    = 8'h14;

    localparam [3:0] ST_IDLE   = 4'd0;
    localparam [3:0] ST_START0 = 4'd1;
    localparam [3:0] ST_START1 = 4'd2;
    localparam [3:0] ST_START2 = 4'd3;
    localparam [3:0] ST_BITLOW = 4'd4;
    localparam [3:0] ST_BITHIGH = 4'd5;
    localparam [3:0] ST_STOP0  = 4'd6;
    localparam [3:0] ST_STOP1  = 4'd7;
    localparam [3:0] ST_STOP2  = 4'd8;

    reg [31:0] ctrl;
    reg [31:0] clk_div;
    reg [7:0]  tx_data;
    reg [7:0]  rx_data;

    reg        busy;
    reg        done;
    reg        nack;
    reg        bus_active;
    reg [3:0]  state;
    reg [31:0] div_cnt;
    reg [3:0]  bit_cnt;
    reg [7:0]  tx_shift;
    reg [7:0]  rx_shift;
    reg        cmd_read;
    reg        cmd_stop;
    reg        cmd_nack;
    reg        cmd_data;

    reg        scl_s0, scl_s1;
    reg        sda_s0, sda_s1;

    wire [31:0] wmask = {{8{wstrb[3]}}, {8{wstrb[2]}},
                         {8{wstrb[1]}}, {8{wstrb[0]}}};
    wire cmd_data_valid = (wdata[3:2] == 2'b01) ||
                          (wdata[3:2] == 2'b10);
    wire cmd_control_valid = (wdata[3:2] == 2'b00) &&
                             ((wdata[1:0] == 2'b01) ||
                              (wdata[1:0] == 2'b10));
    wire cmd_start = we && (addr[7:0] == REG_CMD) && wstrb[0] &&
                     ctrl[0] && !busy &&
                     (cmd_data_valid || cmd_control_valid);
    wire tick = (div_cnt >= clk_div);

    assign irq = done && ctrl[1];

    always @(posedge clk or negedge rstn) begin
        if (!rstn) begin
            ctrl       <= 32'b0;
            clk_div    <= 32'd499;
            tx_data    <= 8'b0;
            rx_data    <= 8'b0;
            busy       <= 1'b0;
            done       <= 1'b0;
            nack       <= 1'b0;
            bus_active <= 1'b0;
            state      <= ST_IDLE;
            div_cnt    <= 32'b0;
            bit_cnt    <= 4'b0;
            tx_shift   <= 8'b0;
            rx_shift   <= 8'b0;
            cmd_read   <= 1'b0;
            cmd_stop   <= 1'b0;
            cmd_nack   <= 1'b0;
            cmd_data   <= 1'b0;
            scl_oe     <= 1'b0;
            sda_oe     <= 1'b0;
            scl_s0     <= 1'b1;
            scl_s1     <= 1'b1;
            sda_s0     <= 1'b1;
            sda_s1     <= 1'b1;
        end else begin
            scl_s0 <= scl_i;
            scl_s1 <= scl_s0;
            sda_s0 <= sda_i;
            sda_s1 <= sda_s0;

            if (we && (addr[7:0] == REG_CTRL) && wstrb[0]) begin
                ctrl[0] <= wdata[0];
                ctrl[1] <= wdata[1];
            end
            if (we && (addr[7:0] == REG_CLKDIV))
                clk_div <= (clk_div & ~wmask) | (wdata & wmask);
            if (we && (addr[7:0] == REG_TXDATA) && wstrb[0])
                tx_data <= wdata[7:0];
            if (we && (addr[7:0] == REG_STATUS) && wstrb[0]) begin
                if (wdata[1])
                    done <= 1'b0;
                if (wdata[2])
                    nack <= 1'b0;
            end

            if (!ctrl[0]) begin
                busy       <= 1'b0;
                done       <= 1'b0;
                nack       <= 1'b0;
                bus_active <= 1'b0;
                state      <= ST_IDLE;
                div_cnt    <= 32'b0;
                bit_cnt    <= 4'b0;
                scl_oe     <= 1'b0;
                sda_oe     <= 1'b0;
            end else begin
                case (state)
                    ST_IDLE: begin
                        // Between byte commands keep SCL low until STOP. This
                        // prevents a false bus-free interval during a message.
                        scl_oe <= bus_active;
                        sda_oe <= 1'b0;
                        div_cnt <= 32'b0;
                        if (cmd_start) begin
                            done      <= 1'b0;
                            nack      <= 1'b0;
                            cmd_read  <= wdata[2];
                            cmd_stop  <= wdata[1];
                            cmd_nack  <= wdata[4];
                            cmd_data  <= cmd_data_valid;
                            tx_shift  <= tx_data;
                            rx_shift  <= 8'b0;
                            bit_cnt   <= 4'b0;
                            div_cnt   <= 32'b0;
                            if (wdata[0]) begin
                                busy       <= 1'b1;
                                bus_active <= 1'b1;
                                // A free bus already has SCL high; only a
                                // repeated START needs the low-to-high setup.
                                state      <= bus_active ? ST_START0 : ST_START1;
                                scl_oe     <= bus_active;
                                sda_oe     <= 1'b0;
                            end else if (cmd_data_valid) begin
                                busy       <= 1'b1;
                                bus_active <= 1'b1;
                                state      <= ST_BITLOW;
                                scl_oe     <= 1'b1;
                                sda_oe     <= 1'b0;
                            end else if (wdata[1] && bus_active) begin
                                busy   <= 1'b1;
                                state  <= ST_STOP0;
                                scl_oe <= 1'b1;
                                sda_oe <= 1'b1;
                            end else begin
                                // STOP on an already free bus is a no-op.
                                done <= 1'b1;
                            end
                        end
                    end

                    ST_START0: begin
                        // Repeated START first releases SDA while SCL is low.
                        scl_oe <= 1'b1;
                        sda_oe <= 1'b0;
                        if (tick) begin
                            div_cnt <= 32'b0;
                            scl_oe  <= 1'b0;
                            state   <= ST_START1;
                        end else begin
                            div_cnt <= div_cnt + 32'd1;
                        end
                    end

                    ST_START1: begin
                        sda_oe <= 1'b0;
                        if (!scl_s1) begin
                            div_cnt <= 32'b0;
                        end else if (tick) begin
                            div_cnt <= 32'b0;
                            sda_oe  <= 1'b1;
                            state   <= ST_START2;
                        end else begin
                            div_cnt <= div_cnt + 32'd1;
                        end
                    end

                    ST_START2: begin
                        scl_oe <= 1'b0;
                        sda_oe <= 1'b1;
                        if (tick) begin
                            div_cnt <= 32'b0;
                            scl_oe  <= 1'b1;
                            if (cmd_data) begin
                                state <= ST_BITLOW;
                            end else begin
                                busy  <= 1'b0;
                                done  <= 1'b1;
                                state <= ST_IDLE;
                            end
                        end else begin
                            div_cnt <= div_cnt + 32'd1;
                        end
                    end

                    ST_BITLOW: begin
                        scl_oe <= 1'b1;
                        if (bit_cnt == 4'd8) begin
                            // Eighth falling edge: write receives ACK, read
                            // transmits ACK (low) or NACK (released).
                            sda_oe <= cmd_read ? !cmd_nack : 1'b0;
                        end else if (cmd_read) begin
                            sda_oe <= 1'b0;
                        end else begin
                            sda_oe <= !tx_shift[4'd7 - bit_cnt];
                        end
                        if (tick) begin
                            div_cnt <= 32'b0;
                            scl_oe  <= 1'b0;
                            state   <= ST_BITHIGH;
                        end else begin
                            div_cnt <= div_cnt + 32'd1;
                        end
                    end

                    ST_BITHIGH: begin
                        if (!scl_s1) begin
                            // A slave may hold SCL low. Restart the high-time
                            // count only after the synchronized pad rises.
                            div_cnt <= 32'b0;
                        end else if (tick) begin
                            div_cnt <= 32'b0;
                            scl_oe  <= 1'b1;
                            sda_oe  <= 1'b0;
                            if (cmd_read && (bit_cnt < 4'd8)) begin
                                rx_shift[4'd7 - bit_cnt] <= sda_s1;
                                if (bit_cnt == 4'd7)
                                    rx_data <= {rx_shift[7:1], sda_s1};
                            end
                            if (bit_cnt == 4'd8) begin
                                if (!cmd_read)
                                    nack <= sda_s1;
                                if (cmd_stop) begin
                                    state <= ST_STOP0;
                                end else begin
                                    busy  <= 1'b0;
                                    done  <= 1'b1;
                                    state <= ST_IDLE;
                                end
                            end else begin
                                bit_cnt <= bit_cnt + 4'd1;
                                state   <= ST_BITLOW;
                            end
                        end else begin
                            div_cnt <= div_cnt + 32'd1;
                        end
                    end

                    ST_STOP0: begin
                        // STOP requires SDA low before SCL rises.
                        scl_oe <= 1'b1;
                        sda_oe <= 1'b1;
                        if (tick) begin
                            div_cnt <= 32'b0;
                            scl_oe  <= 1'b0;
                            state   <= ST_STOP1;
                        end else begin
                            div_cnt <= div_cnt + 32'd1;
                        end
                    end

                    ST_STOP1: begin
                        sda_oe <= 1'b1;
                        if (!scl_s1) begin
                            div_cnt <= 32'b0;
                        end else if (tick) begin
                            div_cnt <= 32'b0;
                            sda_oe  <= 1'b0;
                            state   <= ST_STOP2;
                        end else begin
                            div_cnt <= div_cnt + 32'd1;
                        end
                    end

                    ST_STOP2: begin
                        scl_oe <= 1'b0;
                        sda_oe <= 1'b0;
                        if (tick) begin
                            div_cnt    <= 32'b0;
                            busy       <= 1'b0;
                            done       <= 1'b1;
                            bus_active <= 1'b0;
                            state      <= ST_IDLE;
                        end else begin
                            div_cnt <= div_cnt + 32'd1;
                        end
                    end

                    default: begin
                        busy       <= 1'b0;
                        bus_active <= 1'b0;
                        state      <= ST_IDLE;
                        scl_oe     <= 1'b0;
                        sda_oe     <= 1'b0;
                    end
                endcase
            end
        end
    end

    assign rdata = (addr[7:0] == REG_CTRL)   ? ctrl :
                   (addr[7:0] == REG_STATUS) ? {26'b0, sda_s1, scl_s1,
                                                bus_active, nack, done, busy} :
                   (addr[7:0] == REG_CLKDIV) ? clk_div :
                   (addr[7:0] == REG_TXDATA) ? {24'b0, tx_data} :
                   (addr[7:0] == REG_RXDATA) ? {24'b0, rx_data} : 32'b0;

endmodule
