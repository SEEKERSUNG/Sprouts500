`timescale 1ns/1ps

// Generic SoC testbench for user programs built with sw/lib.
//
// Contract with the program:
//   ITCM 0x7FF8 : result code   (0 = pass)
//   ITCM 0x7FFC : halt magic 0x5A5A5A5A
//   UART        : free-form; decoded and printed as it arrives
//
// plusargs:
//   +ITCM_HEX=<file>     flat word image loaded at ITCM address 0
//   +MAX_CYCLES=<dec>    timeout (default 2000000)
//   +EXPECT=<dec>        expected result code (default 0)
//   +DRAIN_NS=<dec>      wait after halt for UART to finish (default 1000000)
//   +UART_FILE=<file>    also write received UART bytes to this file
//   +GPIO_IN=<hex>       drive input GPIO pins (pins configured as inputs)
//   +SPI_LOOPBACK        connect spi_mosi back to spi_miso
//   +VCD                 dump build/tb_app.vcd

module tb_app;
    localparam ITCM_WORDS = 8192;                 // 32 KiB
    localparam HALT_IDX   = 32'h0000_7FFC >> 2;
    localparam RESULT_IDX = 32'h0000_7FF8 >> 2;
    localparam CLKS_PER_BIT = 434;                // 50 MHz / 115200

    reg clk = 1'b0;
    reg rstn = 1'b0;
    always #10 clk = ~clk;                        // 50 MHz

    reg  [31:0] gpio_ext = 32'b0;
    reg         gpio_ext_en = 1'b0;
    reg         spi_loopback = 1'b0;

    wire [31:0] gpio_pin;
    wire        uart_tx;
    wire        spi_sclk, spi_mosi;
    wire [3:0]  spi_cs_n;

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
        .spi_miso(spi_loopback ? spi_mosi : 1'b0),
        .spi_cs_n(spi_cs_n)
    );

    // Drive only pins the core has not configured as outputs, so the
    // testbench never fights the SoC pad driver.
    genvar gi;
    generate
        for (gi = 0; gi < 32; gi = gi + 1) begin : g_ext_gpio
            assign gpio_pin[gi] = (gpio_ext_en && !dut.u_gpio.gpio_oe[gi])
                                  ? gpio_ext[gi] : 1'bz;
        end
    endgenerate

    // ------------------------------------------------------------------
    // UART receiver
    // ------------------------------------------------------------------
    localparam [1:0] U_IDLE = 2'd0, U_START = 2'd1, U_DATA = 2'd2, U_STOP = 2'd3;
    reg [1:0]  u_state;
    reg [15:0] u_cnt;
    reg [2:0]  u_bit;
    reg [7:0]  u_data;

    integer rx_count = 0;
    reg [7:0] rx_buf [0:255];

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
                            if (rx_count < 256)
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

    // ------------------------------------------------------------------
    // run control
    // ------------------------------------------------------------------
    reg [1023:0] itcm_file;
    reg [1023:0] uart_file;
    integer max_cycles;
    integer drain_ns;
    integer expected_result;
    integer cycles = 0;
    integer halt_cycles = 0;
    integer i;

    reg        halt_seen = 1'b0;
    reg        timed_out = 1'b0;
    reg [31:0] app_result = 32'hxxxx_xxxx;
    reg        uart_log_en = 1'b0;
    integer    uart_fd;
    integer    uart_dump_n;
    integer    errors = 0;

    always @(posedge clk) begin
        if (rstn) begin
            cycles <= cycles + 1;
            if (!halt_seen && dut.u_core.ITCM[HALT_IDX] === 32'h5A5A5A5A) begin
                halt_seen  <= 1'b1;
                halt_cycles <= cycles;
                app_result <= dut.u_core.ITCM[RESULT_IDX];
            end
        end
    end

    initial begin
        if (!$value$plusargs("ITCM_HEX=%s", itcm_file)) begin
            $display("ERROR: missing +ITCM_HEX");
            $finish;
        end
        if (!$value$plusargs("MAX_CYCLES=%d", max_cycles))
            max_cycles = 2000000;
        if (!$value$plusargs("EXPECT=%d", expected_result))
            expected_result = 0;
        if (!$value$plusargs("DRAIN_NS=%d", drain_ns))
            drain_ns = 1000000;

        if ($value$plusargs("GPIO_IN=%h", gpio_ext))
            gpio_ext_en = 1'b1;
        spi_loopback = $test$plusargs("SPI_LOOPBACK");

        if ($value$plusargs("UART_FILE=%s", uart_file)) begin
            uart_log_en = 1'b1;
            uart_fd = $fopen(uart_file, "w");
        end

        if ($test$plusargs("VCD")) begin
            $dumpfile("build/tb_app.vcd");
            $dumpvars(0, tb_app);
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
                $display("\nAPP_TIMEOUT cycles=%0d pc=%h", cycles, dut.u_core.IF_PC);
                timed_out = 1'b1;
                // A timeout is usually a dead loop: finish the run right away
                // instead of draining UART for another DRAIN_NS.
                if (!halt_seen)
                    drain_ns = 0;
            end
        join_any
        disable fork;

        // Let the UART receiver finish any byte still on the wire.
        #(drain_ns);

        // The capture buffer holds the first 256 bytes; print them all but
        // only write what was actually stored.
        uart_dump_n = (rx_count < 256) ? rx_count : 256;
        if (uart_log_en && (uart_fd != 0))
            for (i = 0; i < uart_dump_n; i = i + 1)
                $fwrite(uart_fd, "%c", rx_buf[i]);

        $display("\nAPP_RESULT=%0d (0x%08x) expected=%0d",
                 app_result, app_result, expected_result);
        $display("APP_CYCLES=%0d UART_RX_BYTES=%0d", halt_cycles, rx_count);

        if (!halt_seen || timed_out)
            errors = errors + 1;
        if (app_result !== expected_result)
            errors = errors + 1;

        if (errors == 0)
            $display("TEST_PASS");
        else
            $display("TEST_FAIL errors=%0d", errors);
        $finish;
    end

endmodule
