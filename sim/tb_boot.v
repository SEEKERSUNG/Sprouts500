`timescale 1ns/1ps

// End-to-end UART boot test.  Only the UART baud divider is accelerated; all
// application bytes, frames, CRCs and acknowledgements cross the real RTL UART.
module tb_boot;
    localparam CLKS_PER_BIT = 32;
    localparam APP_HEADER   = 32'h0000_0800;
    localparam APP_ENTRY    = 32'h0000_0810;
    localparam HALT_IDX     = 32'h0000_7FFC >> 2;
    localparam RESULT_IDX   = 32'h0000_7FF8 >> 2;

    reg clk = 1'b0;
    reg rstn = 1'b0;
    reg boot0 = 1'b0;
    reg host_tx = 1'b1;
    always #5 clk = ~clk;

    wire [31:0] gpio_pin;
    wire uart_tx;
    wire spi_sclk, spi_mosi;
    wire [3:0] spi_cs_n;
    assign gpio_pin[0] = boot0;
    assign gpio_pin[31:1] = 31'bz;

    soc_fpga_top #(.ITCM_INIT_FILE("")) dut (
        .clk(clk), .rstn(rstn),
        .uart_rx(host_tx), .uart_tx(uart_tx),
        .gpio_pin(gpio_pin),
        .spi_sclk(spi_sclk), .spi_mosi(spi_mosi), .spi_miso(1'b0),
        .spi_cs_n(spi_cs_n)
    );

    // ------------------------------------------------------------------
    // DUT UART receiver, including binary protocol responses.
    // ------------------------------------------------------------------
    localparam [1:0] U_IDLE = 2'd0, U_START = 2'd1, U_DATA = 2'd2, U_STOP = 2'd3;
    reg [1:0] u_state = U_IDLE;
    integer u_cnt = 0;
    integer u_bit = 0;
    reg [7:0] u_data = 0;
    integer rx_count = 0;
    reg [7:0] rx_buf [0:65535];

    always @(posedge clk or negedge rstn) begin
        if (!rstn) begin
            u_state <= U_IDLE;
            u_cnt <= 0;
            u_bit <= 0;
        end else begin
            case (u_state)
                U_IDLE: begin
                    u_cnt <= 0;
                    if (!uart_tx) begin
                        u_state <= U_START;
                        u_cnt <= CLKS_PER_BIT / 2;
                    end
                end
                U_START: begin
                    if (u_cnt == 0) begin
                        if (!uart_tx) begin
                            u_state <= U_DATA;
                            u_cnt <= CLKS_PER_BIT - 1;
                            u_bit <= 0;
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
                        if (u_bit == 7)
                            u_state <= U_STOP;
                        else
                            u_bit <= u_bit + 1;
                    end else begin
                        u_cnt <= u_cnt - 1;
                    end
                end
                U_STOP: begin
                    if (u_cnt == 0) begin
                        if (uart_tx) begin
                            rx_buf[rx_count] <= u_data;
                            rx_count <= rx_count + 1;
                        end
                        u_state <= U_IDLE;
                    end else begin
                        u_cnt <= u_cnt - 1;
                    end
                end
            endcase
        end
    end

    // ------------------------------------------------------------------
    // Host UART transmitter and the same framed protocol as uart_boot.py.
    // ------------------------------------------------------------------
    task host_byte;
        input [7:0] b;
        integer k;
        begin
            host_tx = 1'b0;
            repeat (CLKS_PER_BIT) @(posedge clk);
            for (k = 0; k < 8; k = k + 1) begin
                host_tx = b[k];
                repeat (CLKS_PER_BIT) @(posedge clk);
            end
            host_tx = 1'b1;
            repeat (CLKS_PER_BIT) @(posedge clk);
        end
    endtask

    function [31:0] crc_next;
        input [31:0] crc_in;
        input [7:0] b;
        reg [31:0] c;
        integer k;
        begin
            c = crc_in ^ b;
            for (k = 0; k < 8; k = k + 1)
                c = (c >> 1) ^ ((c & 1) ? 32'hedb88320 : 32'b0);
            crc_next = c;
        end
    endfunction

    task body_byte;
        input [7:0] b;
        inout [31:0] crc;
        begin
            host_byte(b);
            crc = crc_next(crc, b);
        end
    endtask

    reg [7:0] tx_payload [0:255];
    task send_frame;
        input [7:0] cmd;
        input [7:0] seq;
        input [31:0] address;
        input integer length;
        input integer corrupt;
        reg [31:0] crc;
        integer k;
        begin
            crc = 32'hffffffff;
            host_byte(8'h53);
            host_byte(8'h42);
            body_byte(cmd, crc);
            body_byte(seq, crc);
            for (k = 0; k < 4; k = k + 1)
                body_byte(address >> (k * 8), crc);
            body_byte(length[7:0], crc);
            body_byte(length >> 8, crc);
            body_byte(8'h00, crc);
            body_byte(8'h00, crc);
            for (k = 0; k < length; k = k + 1)
                body_byte(tx_payload[k], crc);
            crc = ~crc;
            if (corrupt)
                crc = crc ^ 32'h0000_0001;
            for (k = 0; k < 4; k = k + 1)
                host_byte(crc >> (k * 8));
        end
    endtask

    integer errors = 0;
    integer response_start = 0;
    integer response_status = 0;
    task call_boot;
        input [7:0] cmd;
        input [7:0] seq;
        input [31:0] address;
        input integer length;
        input integer expected_payload;
        input integer expected_status;
        input integer corrupt;
        integer target, timeout, k, n;
        reg [31:0] crc, received;
        begin
            response_start = rx_count;
            send_frame(cmd, seq, address, length, corrupt);
            target = response_start + 16 + expected_payload;
            timeout = 0;
            while ((rx_count < target) && (timeout < 100000)) begin
                @(posedge clk);
                timeout = timeout + 1;
            end
            #1;
            if (rx_count < target) begin
                $display("BOOT_ERROR response timeout cmd=%02x seq=%02x rx=%0d target=%0d",
                         cmd, seq, rx_count, target);
                errors = errors + 1;
            end else begin
                n = response_start;
                if ((rx_buf[n] !== 8'h53) || (rx_buf[n+1] !== 8'h42) ||
                    (rx_buf[n+2] !== (cmd | 8'h80)) || (rx_buf[n+3] !== seq) ||
                    (rx_buf[n+8] !== expected_payload[7:0]) ||
                    (rx_buf[n+9] !== (expected_payload >> 8))) begin
                    $display("BOOT_ERROR bad response header cmd=%02x seq=%02x", cmd, seq);
                    errors = errors + 1;
                end
                response_status = {rx_buf[n+15], rx_buf[n+14], rx_buf[n+13], rx_buf[n+12]};
                if (response_status !== expected_status) begin
                    $display("BOOT_ERROR cmd=%02x status=%0d expected=%0d",
                             cmd, response_status, expected_status);
                    errors = errors + 1;
                end
                crc = 32'hffffffff;
                for (k = 2; k < 12 + expected_payload; k = k + 1)
                    crc = crc_next(crc, rx_buf[n+k]);
                crc = ~crc;
                received = {rx_buf[n+15+expected_payload], rx_buf[n+14+expected_payload],
                            rx_buf[n+13+expected_payload], rx_buf[n+12+expected_payload]};
                if (crc !== received) begin
                    $display("BOOT_ERROR response CRC cmd=%02x got=%08x expected=%08x",
                             cmd, received, crc);
                    errors = errors + 1;
                end
            end
        end
    endtask

    // ------------------------------------------------------------------
    // Test image and run control.
    // ------------------------------------------------------------------
    reg [1023:0] boot_file, image_file;
    reg [7:0] image [0:32751];
    integer image_len;
    integer cycles = 0;
    integer i, j, offset, chunk, found;
    integer tmo;
    reg [31:0] saved_crc_word;
    reg [31:0] saved_boot_word, actual_word, expected_crc;
    integer banner_start;

    always @(posedge clk) begin
        if (rstn) begin
            cycles <= cycles + 1;
            if (cycles > 2000000) begin
                $display("BOOT_TEST_FAIL global timeout pc=%08x", dut.u_soc.u_core.IF_PC);
                $finish;
            end
        end
    end

    task wait_app;
        integer timeout;
        begin
            timeout = 0;
            while ((dut.u_soc.u_core.ITCM[HALT_IDX] !== 32'h5a5a5a5a) &&
                   (timeout < 100000)) begin
                @(posedge clk);
                timeout = timeout + 1;
            end
            if (dut.u_soc.u_core.ITCM[HALT_IDX] !== 32'h5a5a5a5a) begin
                $display("BOOT_ERROR application did not halt, pc=%08x",
                         dut.u_soc.u_core.IF_PC);
                errors = errors + 1;
            end
            if (dut.u_soc.u_core.ITCM[RESULT_IDX] !== 0) begin
                $display("BOOT_ERROR application result=%08x",
                         dut.u_soc.u_core.ITCM[RESULT_IDX]);
                errors = errors + 1;
            end
            if (dut.u_soc.u_core.ITCM[0] !== saved_boot_word) begin
                $display("BOOT_ERROR protected Boot word changed");
                errors = errors + 1;
            end
        end
    endtask

    initial begin
        if (!$value$plusargs("BOOT_HEX=%s", boot_file) ||
            !$value$plusargs("IMAGE_HEX=%s", image_file) ||
            !$value$plusargs("IMAGE_LEN=%d", image_len)) begin
            $display("BOOT_TEST_FAIL missing BOOT_HEX/IMAGE_HEX/IMAGE_LEN");
            $finish;
        end
        $readmemh(boot_file, dut.u_soc.u_core.ITCM, 0, 511);
        $readmemh(image_file, image, 0, image_len - 1);
        for (i = 512; i < 8192; i = i + 1)
            dut.u_soc.u_core.ITCM[i] = 32'b0;
        saved_boot_word = dut.u_soc.u_core.ITCM[0];

        // Production uses 867; 32 clocks/bit is safe with nibble-table CRC.
        force dut.u_soc.u_uart.baud_div = 32'd31;
        rstn = 1'b0;
        boot0 = 1'b0;
        repeat (16) @(posedge clk);
        rstn = 1'b1;

        // Forced download mode emits the ten-byte S500BOOT\r\n banner.
        while (rx_count < 10) @(posedge clk);
        if ({rx_buf[0], rx_buf[1], rx_buf[2], rx_buf[3]} !== "S500") begin
            $display("BOOT_ERROR missing boot banner");
            errors = errors + 1;
        end

        call_boot(8'h01, 8'h01, 0, 0, 20, 0, 0);  // INFO
        if ({rx_buf[response_start+19], rx_buf[response_start+18],
             rx_buf[response_start+17], rx_buf[response_start+16]} !== 1) begin
            $display("BOOT_ERROR INFO version");
            errors = errors + 1;
        end

        // A bad CRC and an out-of-range write must be rejected without a reset.
        for (i = 0; i < 4; i = i + 1) tx_payload[i] = i;
        call_boot(8'h02, 8'h02, APP_ENTRY, 4, 4, 1, 1);
        call_boot(8'h02, 8'h03, 0, 4, 4, 2, 0);
        for (j = 0; j < 32; j = j + 1) tx_payload[j] = 0;
        call_boot(8'h02, 8'h7a, 32'h0000_7ff0, 1, 4, 2, 0);
        call_boot(8'h02, 8'h7b, 32'hffff_fff0, 32, 4, 2, 0);
        call_boot(8'h55, 8'h04, 0, 0, 4, 3, 0);

        // Invalidate first, write unaligned chunks, then commit the header.
        for (i = 0; i < 4; i = i + 1) tx_payload[i] = 0;
        call_boot(8'h02, 8'h05, APP_HEADER, 4, 4, 0, 0);
        offset = 0;
        i = 6;
        while (offset < image_len - 16) begin
            chunk = ((image_len - 16 - offset) > 37) ? 37 : image_len - 16 - offset;
            for (j = 0; j < chunk; j = j + 1)
                tx_payload[j] = image[16 + offset + j];
            call_boot(8'h02, i[7:0], APP_ENTRY + offset, chunk, 4, 0, 0);
            offset = offset + chunk;
            i = i + 1;
        end
        for (j = 0; j < 16; j = j + 1) tx_payload[j] = image[j];
        call_boot(8'h02, i[7:0], APP_HEADER, 16, 4, 0, 0);
        $display("BOOT_STAGE image_written rx=%0d", rx_count);
        call_boot(8'h03, 8'h70, 0, 0, 8, 0, 0);  // VERIFY
        $display("BOOT_STAGE verified status=%0d", response_status);
        expected_crc = {image[15], image[14], image[13], image[12]};
        if ({rx_buf[response_start+19], rx_buf[response_start+18],
             rx_buf[response_start+17], rx_buf[response_start+16]} !== expected_crc) begin
            $display("BOOT_ERROR VERIFY CRC mismatch");
            errors = errors + 1;
        end
        call_boot(8'h04, 8'h71, 0, 0, 4, 0, 0);  // RUN
        $display("BOOT_STAGE run_ack status=%0d", response_status);
        wait_app();
        $display("BOOT_STAGE first_app_done result=%08x",
                 dut.u_soc.u_core.ITCM[RESULT_IDX]);

        // Verify every downloaded byte, not just the application's pass code.
        for (i = 0; i < image_len - 16; i = i + 1) begin
            actual_word = dut.u_soc.u_core.ITCM[(APP_ENTRY + i) >> 2];
            actual_word = actual_word >> (((APP_ENTRY + i) & 3) * 8);
            if (actual_word[7:0] !== image[16+i]) begin
                if (errors < 20)
                    $display("BOOT_ERROR image byte %0d got=%02x expected=%02x",
                             i, actual_word[7:0], image[16+i]);
                errors = errors + 1;
            end
        end
        found = 0;
        for (i = 0; i < rx_count - 6; i = i + 1)
            if ((rx_buf[i] == "A") && (rx_buf[i+1] == "P") &&
                (rx_buf[i+2] == "P") && (rx_buf[i+3] == "_") &&
                (rx_buf[i+4] == "O") && (rx_buf[i+5] == "K"))
                found = 1;
        if (!found) begin
            $display("BOOT_ERROR application UART output missing");
            errors = errors + 1;
        end

        // BOOT0 high with a corrupted stored image CRC must not run the stale
        // application; it must fall into the downloader instead.
        dut.u_soc.u_core.ITCM[HALT_IDX] = 0;
        dut.u_soc.u_core.ITCM[RESULT_IDX] = 32'hffff_ffff;
        saved_crc_word = dut.u_soc.u_core.ITCM[(APP_HEADER + 12) >> 2];
        dut.u_soc.u_core.ITCM[(APP_HEADER + 12) >> 2] = saved_crc_word ^ 32'h1;
        banner_start = rx_count;
        rstn = 1'b0;
        boot0 = 1'b1;
        repeat (16) @(posedge clk);
        rstn = 1'b1;
        tmo = 0;
        while ((rx_count < banner_start + 10) && (tmo < 100000)) begin
            @(posedge clk);
            tmo = tmo + 1;
        end
        if ((rx_count < banner_start + 10) ||
            ({rx_buf[banner_start], rx_buf[banner_start+1],
              rx_buf[banner_start+2], rx_buf[banner_start+3]} !== "S500")) begin
            $display("BOOT_ERROR invalid image did not enter downloader");
            errors = errors + 1;
        end
        if (dut.u_soc.u_core.ITCM[HALT_IDX] !== 0) begin
            $display("BOOT_ERROR invalid image was executed");
            errors = errors + 1;
        end
        dut.u_soc.u_core.ITCM[(APP_HEADER + 12) >> 2] = saved_crc_word;

        // Normal reset with BOOT0 high must verify and run the retained image.
        dut.u_soc.u_core.ITCM[HALT_IDX] = 0;
        dut.u_soc.u_core.ITCM[RESULT_IDX] = 32'hffff_ffff;
        rstn = 1'b0;
        boot0 = 1'b1;
        repeat (16) @(posedge clk);
        rstn = 1'b1;
        wait_app();
        $display("BOOT_STAGE normal_boot_done result=%08x",
                 dut.u_soc.u_core.ITCM[RESULT_IDX]);

        // With a valid app, holding BOOT0 low must still select the downloader.
        dut.u_soc.u_core.ITCM[HALT_IDX] = 0;
        dut.u_soc.u_core.ITCM[RESULT_IDX] = 32'hffff_ffff;
        banner_start = rx_count;
        rstn = 1'b0;
        boot0 = 1'b0;
        repeat (16) @(posedge clk);
        rstn = 1'b1;
        while (rx_count < banner_start + 10) @(posedge clk);
        $display("BOOT_STAGE forced_boot_banner");
        call_boot(8'h01, 8'h72, 0, 0, 20, 0, 0);
        if (dut.u_soc.u_core.ITCM[HALT_IDX] !== 0) begin
            $display("BOOT_ERROR forced BOOT0 launched the application");
            errors = errors + 1;
        end

        if (errors == 0)
            $display("BOOT_TEST_PASS cycles=%0d app_bytes=%0d uart_bytes=%0d",
                     cycles, image_len - 16, rx_count);
        else
            $display("BOOT_TEST_FAIL errors=%0d", errors);
        $finish;
    end

endmodule
