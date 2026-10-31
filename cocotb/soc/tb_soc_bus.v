// SoC-level cocotb testbench.
//
// soc_top has no external bus ports, so this bench replicates its contents
// (core + bus + timer/gpio/uart/spi + GPIO pads) and adds:
//
//   * an external bus master (`ext_master` selects it) with its own ready,
//     which lets the cocotb BusMaster drive the real bus/peripherals while
//     the core is held in reset (`core_rstn`);
//   * a fake slave at 0x3000_xxxx for core back-to-back / slow-ready tests
//     (`hold` stretches the fake slave's ready and therefore the core's
//     bus_ready_i for accesses that hit it);
//   * `core_stall` to stretch the core's bus_ready_i for any access
//     (bus_wait coverage);
//   * observability ports for the core's bus outputs;
//   * GPIO/UART/SPI pins and loopback hooks;
//   * `+ITCM_HEX=<file>` loads an instruction image into u_core.ITCM.
//
// The RTL itself is not modified; the wiring below mirrors rtl/soc_top.v and
// the pad model mirrors sim/tb_app.v.
`timescale 1ns/1ps

module tb_soc_bus #(
    parameter [31:0] RESET_PC   = 32'h0000_0000,
    parameter        ITCM_WORDS = 8192,
    parameter        DTCM_WORDS = 4096
) (
    input  wire        clk,
    input  wire        rstn,

    // SoC reset (peripherals) and core-only reset
    input  wire        core_rstn,

    // external master
    input  wire        ext_master,
    input  wire        ext_ready,
    input  wire        m_req,
    input  wire        m_we,
    input  wire [31:0] m_addr,
    input  wire [31:0] m_wdata,
    input  wire [3:0]  m_wstrb,
    output wire [31:0] m_rdata,
    output wire        m_ready,

    // fake slave delay + core bus-ready stretch
    input  wire        hold,
    input  wire        core_stall,

    // pads
    inout  wire [31:0] gpio_pin,
    input  wire [31:0] gpio_ext_val,
    input  wire        gpio_ext_en,
    input  wire        uart_rx,
    input  wire        uart_loopback,
    output wire        uart_tx,
    output wire        spi_sclk,
    output wire        spi_mosi,
    input  wire        spi_miso,
    input  wire        spi_loopback,
    output wire [3:0]  spi_cs_n,

    // core bus observability
    output wire        core_req,
    output wire        core_we,
    output wire [31:0] core_addr,
    output wire [31:0] core_wdata,
    output wire [3:0]  core_wstrb,

    // fake slave write pulse (registered)
    output wire        fake_wr,
    output wire [31:0] fake_wr_addr,
    output wire [31:0] fake_wr_wdata,
    output wire [3:0]  fake_wr_wstrb,

    // interrupts
    output wire        timer_irq_o,
    output wire        gpio_irq_o,
    output wire        uart_irq_o,
    output wire        spi_irq_o,

    // program halt mailbox: ITCM[ITCM_WORDS-1] changed to 0x5A5A5A5A
    output wire        core_halted
);

    // ------------------------------------------------------------------
    // bus wires (muxed between core and external master)
    // ------------------------------------------------------------------
    wire        bus_req;
    wire        bus_we;
    wire [31:0] bus_addr;
    wire [31:0] bus_wdata;
    wire [3:0]  bus_wstrb;
    wire [31:0] bus_rdata_native;
    wire [31:0] bus_rdata;
    wire        bus_ready_native;

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

    // Duplicate of bus.v's req edge detector.  It uses the same muxed req and
    // the same reset, so `first` lines up with the strobes bus.v generates.
    reg req_q;
    always @(posedge clk or negedge rstn) begin
        if (!rstn) req_q <= 1'b0;
        else       req_q <= bus_req;
    end
    wire first = bus_req && !req_q;

    // ------------------------------------------------------------------
    // core
    // ------------------------------------------------------------------
    wire c_req, c_we;
    wire [31:0] c_addr, c_wdata;
    wire [3:0] c_wstrb;

    wire c_fake_hit = (c_addr[31:28] >= 4'h3);
    wire fake_ready_lv;

    // A core access is slow if the test stretches core_stall, or if the
    // current bus address hits the fake slave while `hold` is asserted.  The
    // muxed bus_addr is used (not c_addr) so the stall follows the slave
    // that actually answers, both for the core and the external master.
    wire fake_hit = (bus_addr[31:28] >= 4'h3);
    wire core_ready = !core_stall && (!fake_hit || fake_ready_lv);

    core #(
        .RESET_PC(RESET_PC),
        .ITCM_WORDS(ITCM_WORDS),
        .DTCM_WORDS(DTCM_WORDS)
    ) u_core (
        .clk(clk),
        .rstn(core_rstn),
        .bus_req_o(c_req),
        .bus_we_o(c_we),
        .bus_addr_o(c_addr),
        .bus_wdata_o(c_wdata),
        .bus_wstrb_o(c_wstrb),
        .bus_rdata_i(bus_rdata),
        .bus_ready_i(core_ready),
        .timer_irq_i(timer_irq),
        .ext_irq_i(ext_irq),
        .soft_irq_i(1'b0)
    );

    // ------------------------------------------------------------------
    // external master mux
    // ------------------------------------------------------------------
    assign bus_req   = ext_master ? m_req   : c_req;
    assign bus_we    = ext_master ? m_we    : c_we;
    assign bus_addr  = ext_master ? m_addr  : c_addr;
    assign bus_wdata = ext_master ? m_wdata : c_wdata;
    assign bus_wstrb = ext_master ? m_wstrb : c_wstrb;

    // ------------------------------------------------------------------
    // bus + peripherals (same instance structure as rtl/soc_top.v)
    // ------------------------------------------------------------------
    bus u_bus (
        .clk(clk),
        .rstn(rstn),
        .req(bus_req),
        .we(bus_we),
        .addr(bus_addr),
        .wdata(bus_wdata),
        .wstrb(bus_wstrb),
        .rdata(bus_rdata_native),
        .ready(bus_ready_native),
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

    // fake slave lives above the real bus; reads from its address window are
    // taken from it, everything else from bus.v (including held read data).
    wire [31:0] fake_rdata;
    assign bus_rdata = fake_hit ? fake_rdata : bus_rdata_native;

    fake_slave u_fake (
        .clk(clk),
        .rstn(rstn),
        .first(first),
        .we(bus_we),
        .hit(fake_hit),
        .hold(hold),
        .addr(bus_addr),
        .wdata(bus_wdata),
        .wstrb(bus_wstrb),
        .rdata(fake_rdata),
        .ready(fake_ready_lv),
        .wr(fake_wr),
        .wr_addr(fake_wr_addr),
        .wr_wdata(fake_wr_wdata),
        .wr_wstrb(fake_wr_wstrb)
    );

    assign m_rdata = bus_rdata;
    assign m_ready = ext_ready && (!fake_hit || fake_ready_lv);

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

    reg uart_tx_d;
    always @(posedge clk or negedge rstn) begin
        if (!rstn) uart_tx_d <= 1'b1;
        else       uart_tx_d <= uart_tx;
    end

    uart u_uart (
        .clk(clk), .rstn(rstn),
        .sel(uart_sel), .we(uart_we), .rd(uart_rd),
        .addr(uart_addr), .wdata(uart_wdata), .wstrb(uart_wstrb),
        .rdata(uart_rdata),
        // The extra flop delays the looped TX by one clock, like a real wire;
        // without it the RX synchronizer would see the start edge 2 clocks
        // late and lose the first data bits at high baud rates.
        .txd(uart_tx), .rxd(uart_loopback ? uart_tx_d : uart_rx),
        .irq(uart_irq)
    );

    wire spi_miso_i = spi_loopback ? spi_mosi : spi_miso;

    spi u_spi (
        .clk(clk), .rstn(rstn),
        .sel(spi_sel), .we(spi_we), .rd(spi_rd),
        .addr(spi_addr), .wdata(spi_wdata), .wstrb(spi_wstrb),
        .rdata(spi_rdata),
        .sclk(spi_sclk), .mosi(spi_mosi), .miso(spi_miso_i),
        .cs_n(spi_cs_n), .irq(spi_irq)
    );

    // ------------------------------------------------------------------
    // GPIO pads - external drive only on pins configured as input
    // ------------------------------------------------------------------
    genvar gi;
    generate
        for (gi = 0; gi < 32; gi = gi + 1) begin : g_gpio_pad
            assign gpio_pin[gi] = gpio_oe[gi] ? gpio_o[gi] :
                                  (gpio_ext_en ? gpio_ext_val[gi] : 1'bz);
            assign gpio_i[gi]   = gpio_pin[gi];
        end
    endgenerate

    // ------------------------------------------------------------------
    // observability
    // ------------------------------------------------------------------
    assign core_req   = c_req;
    assign core_we    = c_we;
    assign core_addr  = c_addr;
    assign core_wdata = c_wdata;
    assign core_wstrb = c_wstrb;

    assign timer_irq_o = timer_irq;
    assign gpio_irq_o  = gpio_irq;
    assign uart_irq_o  = uart_irq;
    assign spi_irq_o   = spi_irq;

    // ------------------------------------------------------------------
    // program image: one flat image per module, loaded once at time 0
    // ------------------------------------------------------------------
    reg [8*256-1:0] itcm_hex;
    initial begin
        itcm_hex = 0;
        if ($value$plusargs("ITCM_HEX=%s", itcm_hex))
            $readmemh(itcm_hex, u_core.ITCM);
    end

    // Halt detection: programs end with a store of MAGIC into the last ITCM
    // word, then spin.  The reset/boot value of that word is captured once
    // (the ITCM is not reset by core_rstn in this bench), so the flag only
    // rises when software actually wrote the mailbox.
    localparam [31:0] HALT_MAGIC = 32'h5A5A_5A5A;
    // The ITCM is not reset by core_rstn, so clear the mailbox while the core
    // is held in reset.  This lets every cocotb test rerun the same image and
    // observe a fresh halt transition.
    always @(posedge clk) begin
        if (!core_rstn)
            u_core.ITCM[ITCM_WORDS-1] <= 32'h0;
    end
    assign core_halted = (u_core.ITCM[ITCM_WORDS-1] == HALT_MAGIC);

endmodule
