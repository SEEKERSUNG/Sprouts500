`timescale 1ns/1ps

// SoC testbench for the benchmarks in bench/ (CoreMark, Dhrystone).
//
// Contract with the program (bench/runtime/bench_io.c):
//   ITCM 0x7FFC : 0x5A5A5A5A = halt
//   ITCM 0x7FF8 : result code (0 = valid run)
//   ITCM 0x7FF4 : measured cycles (informational)
//   UART        : the benchmark report, printed as it arrives
//
// plusargs:
//   +ITCM_HEX=<file>     flat word image loaded at ITCM address 0
//   +MAX_CYCLES=<dec>    timeout (default 100000000)
//   +UART_FILE=<file>    also write the decoded UART bytes to this file
//   +VCD                 dump build/tb_bench.vcd

module tb_bench;
    localparam ITCM_WORDS = 8192;                 // 32 KiB
    localparam HALT_IDX   = 32'h0000_7FFC >> 2;
    localparam RESULT_IDX = 32'h0000_7FF8 >> 2;
    localparam CYCLES_IDX = 32'h0000_7FF4 >> 2;
    localparam CLKS_PER_BIT = 868;                // 100 MHz / 115200

    reg clk = 1'b0;
    reg rstn = 1'b0;
    always #5 clk = ~clk;                         // 100 MHz

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
        .spi_miso(1'b0),
        .spi_cs_n(spi_cs_n)
    );

    // ------------------------------------------------------------------
    // UART receiver (same decoder as tb_soc.v)
    // ------------------------------------------------------------------
    localparam [1:0] U_IDLE = 2'd0, U_START = 2'd1, U_DATA = 2'd2, U_STOP = 2'd3;
    reg [1:0]  u_state;
    reg [15:0] u_cnt;
    reg [2:0]  u_bit;
    reg [7:0]  u_data;

    integer rx_count = 0;
    reg [7:0] rx_buf [0:16383];

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
                            if (rx_count < 16384)
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
    integer cycles = 0;
    integer halt_cycles = 0;
    integer i;

    reg        halt_seen = 1'b0;
    reg        timed_out = 1'b0;
    reg [31:0] app_result = 32'hxxxx_xxxx;
    reg [31:0] app_cycles = 32'hxxxx_xxxx;
    reg        uart_log_en = 1'b0;
    integer    uart_fd = 0;
    integer    uart_dump_n;
    integer    errors = 0;

    always @(posedge clk) begin
        if (rstn) begin
            cycles <= cycles + 1;
            if (!halt_seen && dut.u_core.ITCM[HALT_IDX] === 32'h5A5A5A5A) begin
                halt_seen  <= 1'b1;
                halt_cycles <= cycles;
                app_result <= dut.u_core.ITCM[RESULT_IDX];
                app_cycles <= dut.u_core.ITCM[CYCLES_IDX];
            end
        end
    end

    initial begin
        if (!$value$plusargs("ITCM_HEX=%s", itcm_file)) begin
            $display("ERROR: missing +ITCM_HEX");
            $finish;
        end
        if (!$value$plusargs("MAX_CYCLES=%d", max_cycles))
            max_cycles = 100000000;
        if (!$value$plusargs("DRAIN_NS=%d", drain_ns))
            drain_ns = 2000000;

        if ($value$plusargs("UART_FILE=%s", uart_file)) begin
            uart_log_en = 1'b1;
            uart_fd = $fopen(uart_file, "w");
        end
        if ($test$plusargs("VCD")) begin
            $dumpfile("build/tb_bench.vcd");
            $dumpvars(0, tb_bench);
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
                $display("\nBENCH_TIMEOUT cycles=%0d pc=%h", cycles, dut.u_core.IF_PC);
                timed_out = 1'b1;
                if (!halt_seen)
                    drain_ns = 0;
            end
        join_any
        disable fork;

        #(drain_ns);

        uart_dump_n = (rx_count < 16384) ? rx_count : 16384;
        if (uart_log_en && (uart_fd != 0))
            for (i = 0; i < uart_dump_n; i = i + 1)
                $fwrite(uart_fd, "%c", rx_buf[i]);

        $display("\nBENCH_RESULT=%0d (0x%08x)", app_result, app_result);
        $display("BENCH_CYCLES=%0d (%0d sim cycles) UART_RX_BYTES=%0d",
                 app_cycles, halt_cycles, rx_count);

        if (!halt_seen || timed_out)
            errors = errors + 1;
        if (app_result !== 32'd0)
            errors = errors + 1;

        if (errors == 0)
            $display("BENCH_PASS");
        else
            $display("BENCH_FAIL errors=%0d", errors);
        $finish;
    end

endmodule
