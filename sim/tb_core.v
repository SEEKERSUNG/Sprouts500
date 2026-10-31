`timescale 1ns/1ps

// Core-only testbench for riscv-arch-test I/M signatures.
// plusargs:
//   +ITCM_HEX=<file>   one 32-bit word per line, loaded at ITCM address 0
//   +SIG_ADDR=<hex>    byte address of begin_signature
//   +SIG_LEN=<dec>     number of signature words
//   +REF_FILE=<file>   reference signature, one word per line
//   +MAX_CYCLES=<dec>  timeout (default 5000000)

module tb_core;
    localparam ITCM_WORDS = 1048576; // 4 MiB (jal-01 has ~1.7 MiB alignment padding)
    localparam HALT_IDX   = ITCM_WORDS - 4; // 0xFFFFC -> byte 0x3FFFF0

    reg clk = 1'b0;
    reg rstn = 1'b0;

    wire        bus_req;
    wire        bus_we;
    wire [31:0] bus_addr;
    wire [31:0] bus_wdata;
    wire [3:0]  bus_wstrb;
    reg  [31:0] bus_rdata = 32'b0;
    reg         bus_ready = 1'b1;

    reg timer_irq = 1'b0;
    reg ext_irq   = 1'b0;
    reg soft_irq  = 1'b0;

    core #(
        .RESET_PC(32'h0000_0000),
        .ITCM_WORDS(ITCM_WORDS),
        .DTCM_WORDS(4096)
    ) dut (
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
        .soft_irq_i(soft_irq)
    );

    reg [1023:0] itcm_file;
    reg [1023:0] ref_file;
    integer sig_addr;
    integer sig_len;
    integer max_cycles;
    integer cycles;
    integer errors;
    integer i;
    reg [31:0] ref_mem [0:8191];
    reg done;
    reg halt_seen;
    reg halt_store_seen;
    reg timed_out;

    reg trace_en;
    integer trace_fd;
    integer trace_n;
    reg pipe_trace_en;
    integer pipe_fd;

    always #5 clk = ~clk;

    initial begin
        if (!$value$plusargs("ITCM_HEX=%s", itcm_file)) begin
            $display("ERROR: missing +ITCM_HEX"); $finish;
        end
        if (!$value$plusargs("SIG_ADDR=%h", sig_addr)) begin
            $display("ERROR: missing +SIG_ADDR"); $finish;
        end
        if (!$value$plusargs("SIG_LEN=%d", sig_len)) begin
            $display("ERROR: missing +SIG_LEN"); $finish;
        end
        if (!$value$plusargs("REF_FILE=%s", ref_file)) begin
            $display("ERROR: missing +REF_FILE"); $finish;
        end
        if (!$value$plusargs("MAX_CYCLES=%d", max_cycles))
            max_cycles = 5000000;

        if ($test$plusargs("VCD")) begin
            $dumpfile("build/tb_core.vcd");
            $dumpvars(0, tb_core);
        end

        cycles = 0;
        errors = 0;
        done = 1'b0;
        halt_seen = 1'b0;
        halt_store_seen = 1'b0;
        timed_out = 1'b0;
        trace_n = 0;
        trace_en = $test$plusargs("TRACE");
        trace_fd = $fopen("build/trace.txt", "w");
        pipe_trace_en = $test$plusargs("TRACE_PIPE");
        pipe_fd = $fopen("build/pipe.txt", "w");
        $readmemh(itcm_file, dut.ITCM);
        $readmemh(ref_file, ref_mem);

        rstn = 1'b0;
        repeat (8) @(posedge clk);
        rstn = 1'b1;
    end

    always @(posedge clk) begin
        if (rstn)
            cycles <= cycles + 1;
    end

    always @(posedge clk) begin
        if (rstn && pipe_trace_en && (cycles < 120)) begin
            $fdisplay(pipe_fd,
                "cyc=%0d IF=%h IFq=%h IFrd=%h | IDpc=%h IDir=%h IDv=%b fl=%b | EXpc=%h EXv=%b EXrd=x%0d we=%b | MEMpc=%h MEMv=%b | WBpc=%h WBv=%b | stall=%b fz=%b",
                cycles, dut.IF_PC, dut.IF_PC_q, dut.if_rdata_q,
                dut.ID_PC, dut.ID_IR, dut.ID_valid, dut.id_flush_q,
                dut.EX_PC, dut.EX_valid, dut.EX_rd, dut.EX_rd_write_req,
                dut.MEM_PC, dut.MEM_valid, dut.WB_PC, dut.WB_valid,
                dut.raw_stall, dut.pipe_freeze);
        end
    end

    always @(posedge clk) begin
        if (rstn && trace_en && (trace_n < 20000)) begin
            if (dut.WB_valid) begin
                $fdisplay(trace_fd, "RET  pc=%08x ir=%08x rd=x%0d we=%0d data=%08x",
                          dut.WB_PC, dut.WB_IR, dut.WB_rd, dut.WB_rd_write_req,
                          dut.wb_rd_data);
                trace_n = trace_n + 1;
            end
            if (dut.MEM_valid && dut.MEM_mem_write) begin
                $fdisplay(trace_fd, "STOR pc=%08x addr=%08x data=%08x strb=%x",
                          dut.MEM_PC, dut.MEM_addr, dut.MEM_wdata, dut.MEM_wstrb);
            end
        end
    end

    always @(posedge clk) begin
        if (rstn) begin
            if (dut.MEM_valid && dut.MEM_mem_write &&
                (dut.MEM_addr == 32'h003FFFF0)) begin
                if (!halt_store_seen)
                    $display("HALT_STORE data=%h wstrb=%h", dut.MEM_wdata, dut.MEM_wstrb);
                halt_store_seen <= 1'b1;
                if (dut.MEM_wdata == 32'h5A5A5A5A)
                    halt_seen <= 1'b1;
            end
            if (dut.ITCM[HALT_IDX] === 32'h5A5A5A5A)
                halt_seen <= 1'b1;
        end
    end

    initial begin
        wait (rstn === 1'b1);
        fork
            begin
                wait (halt_seen === 1'b1);
                done = 1'b1;
            end
            begin
                wait (cycles > max_cycles);
                $display("TEST_TIMEOUT cycles=%0d pc=%h halt=%h",
                         cycles, dut.IF_PC, dut.ITCM[HALT_IDX]);
            end
        join_any
        disable fork;

        if (cycles > max_cycles)
            timed_out = 1'b1;

        #100;
        $display("HALT_WORD=%h pc=%h cycles=%0d", dut.ITCM[HALT_IDX], dut.IF_PC, cycles);
        for (i = 0; i < sig_len; i = i + 1) begin
            if (dut.ITCM[(sig_addr >> 2) + i] !== ref_mem[i]) begin
                $display("SIG_MISMATCH idx=%0d addr=%h got=%h exp=%h",
                         i, sig_addr + 4*i,
                         dut.ITCM[(sig_addr >> 2) + i], ref_mem[i]);
                errors = errors + 1;
                if (errors >= 16)
                    i = sig_len;
            end
        end

        if (errors == 0) begin
            $display("TEST_PASS cycles=%0d", cycles);
        end else begin
            $display("TEST_FAIL errors=%0d", errors);
        end
        $finish;
    end

endmodule
