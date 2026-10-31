// Simple single-outstanding bus / address decoder for Sprouts500.
//
// Memory map:
//   0x2000_0000  timer
//   0x2000_1000  gpio
//   0x2000_2000  uart
//   0x2000_3000  spi
//
// Protocol:
//   req is held by the core until the access completes. A transaction starts
//   on the rising edge of req; the core inserts a bubble between back-to-back
//   bus accesses so every access has a clean edge. Peripherals see a one-cycle
//   we/rd strobe. Read data is held while req stays high, so a core stalled in
//   front of the bus (e.g. during an iterative divide) still reads the data of
//   the transaction it started.
//
// All peripheral registers are 32-bit and word addressed. wstrb selects the
// byte lanes for writes.

module bus (
    input  wire        clk,
    input  wire        rstn,

    input  wire        req,
    input  wire        we,
    input  wire [31:0] addr,
    input  wire [31:0] wdata,
    input  wire [3:0]  wstrb,
    output wire [31:0] rdata,
    output wire        ready,

    // timer
    output wire        timer_sel,
    output wire        timer_we,
    output wire        timer_rd,
    output wire [11:0] timer_addr,
    output wire [31:0] timer_wdata,
    output wire [3:0]  timer_wstrb,
    input  wire [31:0] timer_rdata,

    // gpio
    output wire        gpio_sel,
    output wire        gpio_we,
    output wire        gpio_rd,
    output wire [11:0] gpio_addr,
    output wire [31:0] gpio_wdata,
    output wire [3:0]  gpio_wstrb,
    input  wire [31:0] gpio_rdata,

    // uart
    output wire        uart_sel,
    output wire        uart_we,
    output wire        uart_rd,
    output wire [11:0] uart_addr,
    output wire [31:0] uart_wdata,
    output wire [3:0]  uart_wstrb,
    input  wire [31:0] uart_rdata,

    // spi
    output wire        spi_sel,
    output wire        spi_we,
    output wire        spi_rd,
    output wire [11:0] spi_addr,
    output wire [31:0] spi_wdata,
    output wire [3:0]  spi_wstrb,
    input  wire [31:0] spi_rdata
);

    localparam [19:0] BASE_TIMER = 20'h20000;
    localparam [19:0] BASE_GPIO  = 20'h20001;
    localparam [19:0] BASE_UART  = 20'h20002;
    localparam [19:0] BASE_SPI   = 20'h20003;

    wire sel_timer = (addr[31:12] == BASE_TIMER);
    wire sel_gpio  = (addr[31:12] == BASE_GPIO);
    wire sel_uart  = (addr[31:12] == BASE_UART);
    wire sel_spi   = (addr[31:12] == BASE_SPI);

    reg req_q;
    always @(posedge clk or negedge rstn) begin
        if (!rstn)
            req_q <= 1'b0;
        else
            req_q <= req;
    end

    wire first = req && !req_q;      // one-cycle transaction strobe

    // Reads are combinationally selected; the value is registered at the start
    // of the transaction and held while req remains asserted.
    wire [31:0] rdata_mux = sel_timer ? timer_rdata :
                            sel_gpio  ? gpio_rdata  :
                            sel_uart  ? uart_rdata  :
                            sel_spi   ? spi_rdata   : 32'b0;

    reg [31:0] rdata_hold;
    always @(posedge clk or negedge rstn) begin
        if (!rstn)
            rdata_hold <= 32'b0;
        else if (first && !we)
            rdata_hold <= rdata_mux;
    end

    assign rdata = first ? rdata_mux : rdata_hold;
    assign ready = 1'b1;

    assign timer_sel   = sel_timer;
    assign timer_we    = sel_timer && first &&  we;
    assign timer_rd    = sel_timer && first && !we;
    assign timer_addr  = addr[11:0];
    assign timer_wdata = wdata;
    assign timer_wstrb = wstrb;

    assign gpio_sel   = sel_gpio;
    assign gpio_we    = sel_gpio && first &&  we;
    assign gpio_rd    = sel_gpio && first && !we;
    assign gpio_addr  = addr[11:0];
    assign gpio_wdata = wdata;
    assign gpio_wstrb = wstrb;

    assign uart_sel   = sel_uart;
    assign uart_we    = sel_uart && first &&  we;
    assign uart_rd    = sel_uart && first && !we;
    assign uart_addr  = addr[11:0];
    assign uart_wdata = wdata;
    assign uart_wstrb = wstrb;

    assign spi_sel   = sel_spi;
    assign spi_we    = sel_spi && first &&  we;
    assign spi_rd    = sel_spi && first && !we;
    assign spi_addr  = addr[11:0];
    assign spi_wdata = wdata;
    assign spi_wstrb = wstrb;

endmodule
