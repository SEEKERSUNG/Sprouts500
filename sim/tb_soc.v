`timescale 1ns/1ps

// SoC demo testbench: boots a bare-metal program from ITCM, decodes the UART
// output and checks the expected hello string.
//
// plusargs:
//   +ITCM_HEX=<file>
//   +MAX_CYCLES=<dec>   default 2000000

module tb_soc;
    localparam ITCM_WORDS = 8192;
    localparam HALT_IDX   = 32'h7FFC >> 2;
    localparam CLKS_PER_BIT = 434;      // 50 MHz / 115200

    reg clk = 1'b0;
    reg rstn = 1'b0;
    always #10 clk = ~clk;              // 50 MHz

    wire [31:0] gpio_pin;
    wire uart_tx;
    wire spi_sclk, spi_mosi;
    wire [3:0] spi_cs_n;

    soc_top #(
        .RESET_PC(32'h0000_0000),
        .ITCM_WORDS(ITCM_WORDS),
        .DTCM_WORDS(4096)
    ) dut (
        .clk(clk),
        .rstn(rstn),
        .uart_rx(1'b1),
        .uart_tx(uart_tx),
        .gpio_pin(gpio_pin),
        .spi_sclk(spi_sclk),
        .spi_mosi(spi_mosi),
        .spi_miso(1'b0),
        .spi_cs_n(spi_cs_n)
    );

    localparam EXP_LEN = 40;
    reg [1023:0] itcm_file;
    reg [8*EXP_LEN-1:0] expected = "Hello Sprouts500!\nperipherals ok\nirq ok\n";

    integer cycles = 0;
    integer max_cycles = 2000000;
    integer rx_count = 0;
    integer i;
    integer errors = 0;
    reg [7:0] rx_buf [0:127];
    reg halt_seen = 1'b0;

    // UART receiver
    localparam [1:0] U_IDLE = 2'd0, U_START = 2'd1, U_DATA = 2'd2, U_STOP = 2'd3;
    reg [1:0]  u_state;
    reg [15:0] u_cnt;
    reg [2:0]  u_bit;
    reg [7:0]  u_data;

    always @(posedge clk or negedge rstn) begin
        if (!rstn) begin
            u_state <= U_IDLE;
            u_cnt   <= 16'b0;
            u_bit   <= 3'b0;
            u_data  <= 8'b0;
        end else begin
            case (u_state)
                U_IDLE: begin
                    u_cnt <= 16'b0;
                    if (!uart_tx) begin
                        u_state <= U_START;
                        u_cnt   <= CLKS_PER_BIT / 2;
                    end
                end
                U_START: begin
                    if (u_cnt == 0) begin
                        if (!uart_tx) begin
                            u_state <= U_DATA;
                            u_cnt   <= CLKS_PER_BIT - 1;
                            u_bit   <= 3'b0;
                        end else begin
                            u_state <= U_IDLE;
                        end
                    end else begin
                        u_cnt <= u_cnt - 1;
                    end
                end
                U_DATA: begin
                    if (u_cnt == 0) begin
                        u_data[u_bit] <= uart_tx;
                        u_cnt <= CLKS_PER_BIT - 1;
                        if (u_bit == 3'd7)
                            u_state <= U_STOP;
                        else
                            u_bit <= u_bit + 3'd1;
                    end else begin
                        u_cnt <= u_cnt - 1;
                    end
                end
                U_STOP: begin
                    if (u_cnt == 0) begin
                        if (uart_tx) begin
                            if (rx_count < 128)
                                rx_buf[rx_count] <= u_data;
                            rx_count <= rx_count + 1;
                            $write("%c", u_data);
                        end
                        u_state <= U_IDLE;
                    end else begin
                        u_cnt <= u_cnt - 1;
                    end
                end
                default: u_state <= U_IDLE;
            endcase
        end
    end

    always @(posedge clk) begin
        if (rstn) begin
            cycles <= cycles + 1;
            if (dut.u_core.ITCM[HALT_IDX] === 32'h5A5A5A5A)
                halt_seen <= 1'b1;
        end
    end

    initial begin
        if (!$value$plusargs("ITCM_HEX=%s", itcm_file)) begin
            $display("ERROR: missing +ITCM_HEX");
            $finish;
        end
        if (!$value$plusargs("MAX_CYCLES=%d", max_cycles))
            max_cycles = 2000000;

        if ($test$plusargs("VCD")) begin
            $dumpfile("build/tb_soc.vcd");
            $dumpvars(0, tb_soc);
        end

        $readmemh(itcm_file, dut.u_core.ITCM);

        rstn = 1'b0;
        repeat (16) @(posedge clk);
        rstn = 1'b1;

        fork
            begin
                wait (halt_seen === 1'b1);
            end
            begin
                wait (cycles > max_cycles);
                $display("\nTEST_TIMEOUT cycles=%0d pc=%h", cycles, dut.u_core.IF_PC);
            end
        join_any
        disable fork;

        #100000;
        $display("\nUART_RX_BYTES=%0d", rx_count);

        for (i = 0; i < EXP_LEN; i = i + 1) begin
            if (rx_buf[i] !== expected[8*(EXP_LEN-1-i) +: 8]) begin
                $display("UART_MISMATCH idx=%0d got=%02x exp=%02x",
                         i, rx_buf[i], expected[8*(EXP_LEN-1-i) +: 8]);
                errors = errors + 1;
            end
        end
        if ((rx_count != EXP_LEN) || (cycles > max_cycles))
            errors = errors + 1;

        if (errors == 0)
            $display("TEST_PASS cycles=%0d", cycles);
        else
            $display("TEST_FAIL errors=%0d", errors);
        $finish;
    end

endmodule
