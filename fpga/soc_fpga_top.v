// FPGA-oriented SoC configuration with a resident UART bootloader.
//
// Connect an active-low BOOT0 button to gpio_pin[0], with an external pull-up:
//   BOOT0=0 at reset: stay in the UART downloader even if an app is valid.
//   BOOT0=1 at reset: run a valid app, otherwise enter the downloader.
// ITCM_INIT_FILE must contain the word-hex image produced by build_boot.sh.

module soc_fpga_top #(
    parameter        ITCM_INIT_FILE = "build/fpga/boot/boot.hex",
    parameter [31:0] ITCM_BOOT_BYTES = 32'd2048
)(
    input  wire        clk,
    input  wire        rstn,
    input  wire        uart_rx,
    output wire        uart_tx,
    inout  wire [31:0] gpio_pin,
    output wire        spi_sclk,
    output wire        spi_mosi,
    input  wire        spi_miso,
    output wire [3:0]  spi_cs_n,
    inout  wire        i2c_scl,
    inout  wire        i2c_sda
);

    soc_top #(
        .RESET_PC(32'h0000_0000),
        .ITCM_WORDS(8192),
        .DTCM_WORDS(4096),
        .ITCM_BOOT_BYTES(ITCM_BOOT_BYTES),
        .ITCM_INIT_FILE(ITCM_INIT_FILE)
    ) u_soc (
        .clk(clk),
        .rstn(rstn),
        .uart_rx(uart_rx),
        .uart_tx(uart_tx),
        .gpio_pin(gpio_pin),
        .spi_sclk(spi_sclk),
        .spi_mosi(spi_mosi),
        .spi_miso(spi_miso),
        .spi_cs_n(spi_cs_n),
        .i2c_scl(i2c_scl),
        .i2c_sda(i2c_sda)
    );

endmodule
