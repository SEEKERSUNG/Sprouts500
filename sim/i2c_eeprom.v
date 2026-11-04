`timescale 1ns/1ps

// Simulation-only 256-byte I2C EEPROM, fixed 7-bit address 0x50 by default.
// The first byte after a write address sets the memory pointer. Subsequent
// writes increment it; reads return sequential bytes. SDA changes only while
// SCL is low, and the pad is driven open-drain.
module i2c_eeprom #(
    parameter [6:0] ADDRESS = 7'h50
)(
    input  wire clk,
    input  wire rstn,
    inout  wire scl,
    inout  wire sda
);

    reg [7:0] mem [0:255];
    reg [7:0] ptr;
    reg [7:0] rx_shift, tx_shift;
    reg [3:0] bit_cnt;
    reg       active, addressed, sending, wait_stop;
    reg       expect_pointer, read_after_ack, ack_pending;
    reg       sda_low;
    reg       prev_scl, prev_sda;
    reg [7:0] received;
    integer   i;

    wire start_det = prev_sda && !sda && scl;
    wire stop_det  = !prev_sda && sda && scl;
    wire scl_rise  = !prev_scl && scl;
    wire scl_fall  = prev_scl && !scl;

    assign sda = sda_low ? 1'b0 : 1'bz;

    initial begin
        for (i = 0; i < 256; i = i + 1)
            mem[i] = 8'b0;
    end

    always @(posedge clk or negedge rstn) begin
        if (!rstn) begin
            ptr            <= 8'b0;
            rx_shift       <= 8'b0;
            tx_shift       <= 8'b0;
            bit_cnt        <= 4'b0;
            active         <= 1'b0;
            addressed      <= 1'b0;
            sending        <= 1'b0;
            wait_stop      <= 1'b0;
            expect_pointer <= 1'b0;
            read_after_ack <= 1'b0;
            ack_pending    <= 1'b0;
            sda_low        <= 1'b0;
            prev_scl       <= 1'b1;
            prev_sda       <= 1'b1;
        end else begin
            prev_scl <= scl;
            prev_sda <= sda;

            if (start_det) begin
                active         <= 1'b1;
                addressed      <= 1'b0;
                sending        <= 1'b0;
                wait_stop      <= 1'b0;
                expect_pointer <= 1'b0;
                read_after_ack <= 1'b0;
                ack_pending    <= 1'b0;
                bit_cnt        <= 4'b0;
                rx_shift       <= 8'b0;
                sda_low        <= 1'b0;
            end else if (stop_det) begin
                active         <= 1'b0;
                addressed      <= 1'b0;
                sending        <= 1'b0;
                wait_stop      <= 1'b0;
                expect_pointer <= 1'b0;
                read_after_ack <= 1'b0;
                ack_pending    <= 1'b0;
                bit_cnt        <= 4'b0;
                sda_low        <= 1'b0;
            end else begin
                if (active && scl_rise) begin
                    if (!sending) begin
                        if (bit_cnt < 4'd8) begin
                            received = {rx_shift[6:0], sda};
                            rx_shift <= received;
                            bit_cnt  <= bit_cnt + 4'd1;
                            if (bit_cnt == 4'd7) begin
                                if (!addressed) begin
                                    if (received[7:1] == ADDRESS) begin
                                        addressed   <= 1'b1;
                                        ack_pending <= 1'b1;
                                        if (received[0])
                                            read_after_ack <= 1'b1;
                                        else
                                            expect_pointer <= 1'b1;
                                    end else begin
                                        wait_stop <= 1'b1;
                                    end
                                end else if (expect_pointer) begin
                                    ptr            <= received;
                                    expect_pointer <= 1'b0;
                                    ack_pending    <= 1'b1;
                                end else begin
                                    mem[ptr]    <= received;
                                    ptr         <= ptr + 8'd1;
                                    ack_pending <= 1'b1;
                                end
                            end
                        end else if (bit_cnt == 4'd8) begin
                            ack_pending <= 1'b0;
                            if (read_after_ack) begin
                                sending        <= 1'b1;
                                read_after_ack <= 1'b0;
                                tx_shift       <= mem[ptr];
                                bit_cnt        <= 4'b0;
                            end else begin
                                bit_cnt <= wait_stop ? 4'd9 : 4'd0;
                            end
                        end
                    end else begin
                        if (bit_cnt < 4'd8) begin
                            bit_cnt <= bit_cnt + 4'd1;
                        end else if (bit_cnt == 4'd8) begin
                            if (!sda) begin
                                ptr      <= ptr + 8'd1;
                                tx_shift <= mem[ptr + 8'd1];
                                bit_cnt  <= 4'b0;
                            end else begin
                                sending   <= 1'b0;
                                wait_stop <= 1'b1;
                                bit_cnt   <= 4'd9;
                            end
                        end
                    end
                end

                if (scl_fall) begin
                    if (!active) begin
                        sda_low <= 1'b0;
                    end else if (sending) begin
                        sda_low <= (bit_cnt < 4'd8) ?
                                   !tx_shift[4'd7 - bit_cnt] : 1'b0;
                    end else begin
                        sda_low <= (bit_cnt == 4'd8) && ack_pending;
                    end
                end
            end
        end
    end

endmodule
