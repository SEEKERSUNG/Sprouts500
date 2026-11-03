`timescale 1ns/1ps

// Check the FPGA ITCM_INIT_FILE path: soc_fpga_top's default parameter must
// preload the 2 KiB Boot image and zero the rest of the inferred ITCM, so an
// invalid/absent application header never starts as X in simulation.
module tb_boot_init;
    reg clk = 1'b0;
    reg rstn = 1'b1;
    reg uart_rx = 1'b1;
    wire uart_tx;
    wire [31:0] gpio_pin;
    wire spi_sclk, spi_mosi;
    wire [3:0] spi_cs_n;

    soc_fpga_top dut (
        .clk(clk), .rstn(rstn),
        .uart_rx(uart_rx), .uart_tx(uart_tx),
        .gpio_pin(gpio_pin),
        .spi_sclk(spi_sclk), .spi_mosi(spi_mosi), .spi_miso(1'b0),
        .spi_cs_n(spi_cs_n)
    );

    reg [1023:0] boot_file;
    reg [31:0] ref_words [0:511];
    integer i;
    integer errors = 0;

    initial begin
        if (!$value$plusargs("BOOT_HEX=%s", boot_file)) begin
            $display("BOOT_INIT_FAIL missing +BOOT_HEX");
            $finish;
        end
        $readmemh(boot_file, ref_words, 0, 511);
        #1;
        for (i = 0; i < 512; i = i + 1)
            if (dut.u_soc.u_core.ITCM[i] !== ref_words[i]) begin
                if (errors < 8)
                    $display("BOOT_INIT_ERROR word %0d got=%08x expected=%08x",
                             i, dut.u_soc.u_core.ITCM[i], ref_words[i]);
                errors = errors + 1;
            end
        for (i = 512; i < 8192; i = i + 1)
            if (dut.u_soc.u_core.ITCM[i] !== 32'b0) begin
                if (errors < 8)
                    $display("BOOT_INIT_ERROR uninitialized word %0d got=%08x",
                             i, dut.u_soc.u_core.ITCM[i]);
                errors = errors + 1;
            end
        if (errors == 0)
            $display("BOOT_INIT_PASS");
        else
            $display("BOOT_INIT_FAIL errors=%0d", errors);
        $finish;
    end
endmodule
