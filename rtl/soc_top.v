// Sprouts500 SoC top: core + simple bus + timer/gpio/uart/spi.
//
// Memory map:
//   0x0000_0000  ITCM (32 KiB)
//   0x1000_0000  DTCM (16 KiB)
//   0x2000_0000  timer
//   0x2000_1000  gpio
//   0x2000_2000  uart
//   0x2000_3000  spi

module soc_top #(
    parameter [31:0] RESET_PC   = 32'h0000_0000,
    parameter        ITCM_WORDS = 8192,
    parameter        DTCM_WORDS = 4096
)(
    input  wire        clk,
    input  wire        rstn,

    input  wire        uart_rx,
    output wire        uart_tx,

    inout  wire [31:0] gpio_pin,

    output wire        spi_sclk,
    output wire        spi_mosi,
    input  wire        spi_miso,
    output wire [3:0]  spi_cs_n
);

    // ------------------------------------------------------------------
    // bus
    // ------------------------------------------------------------------
    wire        bus_req;
    wire        bus_we;
    wire [31:0] bus_addr;
    wire [31:0] bus_wdata;
    wire [3:0]  bus_wstrb;
    wire [31:0] bus_rdata;
    wire        bus_ready;

    wire        timer_sel, timer_we, timer_rd;
    wire [11:0] timer_addr;
    wire [31:0] timer_wdata;
    wire [3:0]  timer_wstrb;
    wire [31:0] timer_rdata;
    wire        timer_irq;

    wire        gpio_sel, gpio_we, gpio_rd;
    wire [11:0] gpio_addr;
    wire [31:0] gpio_wdata;
    wire [3:0]  gpio_wstrb;
    wire [31:0] gpio_rdata;
    wire [31:0] gpio_i, gpio_o, gpio_oe;
    wire        gpio_irq;

    wire        uart_sel, uart_we, uart_rd;
    wire [11:0] uart_addr;
    wire [31:0] uart_wdata;
    wire [3:0]  uart_wstrb;
    wire [31:0] uart_rdata;
    wire        uart_irq;

    wire        spi_sel, spi_we, spi_rd;
    wire [11:0] spi_addr;
    wire [31:0] spi_wdata;
    wire [3:0]  spi_wstrb;
    wire [31:0] spi_rdata;
    wire        spi_irq;

    wire        ext_irq = gpio_irq | uart_irq | spi_irq;

    // ------------------------------------------------------------------
    // core
    // ------------------------------------------------------------------
    core #(
        .RESET_PC(RESET_PC),
        .ITCM_WORDS(ITCM_WORDS),
        .DTCM_WORDS(DTCM_WORDS)
    ) u_core (
        .clk(clk),
        .rstn(rstn),
        .bus_req_o(bus_req),
        .bus_we_o(bus_we),
        .bus_addr_o(bus_addr),
        .bus_wdata_o(bus_wdata),
        .bus_wstrb_o(bus_wstrb),
        .bus_rdata_i(bus_rdata),
        .bus_ready_i(bus_ready),
        .timer_irq_i(timer_irq),
        .ext_irq_i(ext_irq),
        .soft_irq_i(1'b0)
    );

    // ------------------------------------------------------------------
    // bus + peripherals
    // ------------------------------------------------------------------
    bus u_bus (
        .clk(clk),
        .rstn(rstn),
        .req(bus_req),
        .we(bus_we),
        .addr(bus_addr),
        .wdata(bus_wdata),
        .wstrb(bus_wstrb),
        .rdata(bus_rdata),
        .ready(bus_ready),
        .timer_sel(timer_sel), .timer_we(timer_we), .timer_rd(timer_rd),
        .timer_addr(timer_addr), .timer_wdata(timer_wdata),
        .timer_wstrb(timer_wstrb), .timer_rdata(timer_rdata),
        .gpio_sel(gpio_sel), .gpio_we(gpio_we), .gpio_rd(gpio_rd),
        .gpio_addr(gpio_addr), .gpio_wdata(gpio_wdata),
        .gpio_wstrb(gpio_wstrb), .gpio_rdata(gpio_rdata),
        .uart_sel(uart_sel), .uart_we(uart_we), .uart_rd(uart_rd),
        .uart_addr(uart_addr), .uart_wdata(uart_wdata),
        .uart_wstrb(uart_wstrb), .uart_rdata(uart_rdata),
        .spi_sel(spi_sel), .spi_we(spi_we), .spi_rd(spi_rd),
        .spi_addr(spi_addr), .spi_wdata(spi_wdata),
        .spi_wstrb(spi_wstrb), .spi_rdata(spi_rdata)
    );

    timer u_timer (
        .clk(clk), .rstn(rstn),
        .sel(timer_sel), .we(timer_we), .rd(timer_rd),
        .addr(timer_addr), .wdata(timer_wdata), .wstrb(timer_wstrb),
        .rdata(timer_rdata), .irq(timer_irq)
    );

    gpio u_gpio (
        .clk(clk), .rstn(rstn),
        .sel(gpio_sel), .we(gpio_we), .rd(gpio_rd),
        .addr(gpio_addr), .wdata(gpio_wdata), .wstrb(gpio_wstrb),
        .rdata(gpio_rdata),
        .gpio_i(gpio_i), .gpio_o(gpio_o), .gpio_oe(gpio_oe),
        .irq(gpio_irq)
    );

    uart u_uart (
        .clk(clk), .rstn(rstn),
        .sel(uart_sel), .we(uart_we), .rd(uart_rd),
        .addr(uart_addr), .wdata(uart_wdata), .wstrb(uart_wstrb),
        .rdata(uart_rdata),
        .txd(uart_tx), .rxd(uart_rx), .irq(uart_irq)
    );

    spi u_spi (
        .clk(clk), .rstn(rstn),
        .sel(spi_sel), .we(spi_we), .rd(spi_rd),
        .addr(spi_addr), .wdata(spi_wdata), .wstrb(spi_wstrb),
        .rdata(spi_rdata),
        .sclk(spi_sclk), .mosi(spi_mosi), .miso(spi_miso),
        .cs_n(spi_cs_n), .irq(spi_irq)
    );

    // ------------------------------------------------------------------
    // GPIO pads
    // ------------------------------------------------------------------
    genvar gi;
    generate
        for (gi = 0; gi < 32; gi = gi + 1) begin : g_gpio_pad
            assign gpio_pin[gi] = gpio_oe[gi] ? gpio_o[gi] : 1'bz;
            assign gpio_i[gi]   = gpio_pin[gi];
        end
    endgenerate

endmodule
