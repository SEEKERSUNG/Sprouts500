`timescale 1ns/1ps

// Lockstep (riscv-dv <-> Sail) testbench for the Sprouts500 core.
//
// Runs a riscv-dv generated program from the ITCM and writes a retirement
// trace that verif/riscv_dv/compare.py aligns against the Sail execution of
// the same image.
//
// plusargs:
//   +ITCM_HEX=<file>    one 32-bit word per line, loaded at ITCM address 0
//   +DTCM_HEX=<file>    one 32-bit word per line, loaded at DTCM offset 0
//   +STOP_PC=<dec>      PC whose fetch ends the run (the `test_done` symbol);
//                       that instruction and everything fetched after it are
//                       not traced, matching Sail's --stop-at-pc
//   +TRACE=<file>       trace output (required)
//   +DTCM_DUMP=<file>   "addr data" for every DTCM word written (optional)
//   +MAX_CYCLES=<dec>   timeout (default 2000000)
//   +HALT_ADDR=<dec>    halt mailbox (default 0x10003ff0, link symbol)
//
// Trace lines, in execution (program) order - the pipeline is in-order and
// writes back in order, so MEM stores and WB retirements interleave exactly:
//   I <pc> <ir> <rd> <wdata>   retired instruction (rd=0 for non-writes)
//   S <addr> <pc> <ir> <data> <strb>   store executed by the instruction
//   X <pc> <ir> <mcause> <mtval>       exception at EX (instruction not retired)
// mcause/mtval are the EX-stage values (ex_exc_cause/ex_exc_tval); mepc is
// not needed because the exception PC is on the X line.
// All stores are word-sized (wstrb = f).
//
// Termination: the halt mailbox store, a fetch outside the ITCM/DTCM, or the
// cycle limit.  ITCM/DTCM contents are preserved across bus waits, so an
// access to unmapped space shows up as stale data and must fail the compare.

module tb_lockstep;
    localparam ITCM_WORDS = 1048576;         // 4 MiB, matches tb_core.v
    localparam DTCM_WORDS = 4096;            // 16 KiB, matches tb_core.v
    localparam HALT_ADDR_DEFAULT = 32'h10003ff0;

    reg clk = 1'b0;
    reg rstn = 1'b0;

    wire        bus_req;
    wire        bus_we;
    wire [31:0] bus_addr;
    wire [31:0] bus_wdata;
    wire [3:0]  bus_wstrb;
    reg  [31:0] bus_rdata = 32'b0;
    reg         bus_ready = 1'b1;

    core #(
        .RESET_PC(32'h0000_0000),
        .ITCM_WORDS(ITCM_WORDS),
        .DTCM_WORDS(DTCM_WORDS)
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
        .timer_irq_i(1'b0),
        .ext_irq_i(1'b0),
        .soft_irq_i(1'b0)
    );

    reg [1023:0] itcm_file;
    reg [1023:0] dtcm_file;
    reg [1023:0] trace_file;
    reg [1023:0] dtcm_file_out;
    integer trace_fd;
    integer dtcm_fd;
    integer stop_pc;
    integer halt_addr;
    integer max_cycles;
    integer cycles;
    integer i;
    reg done;
    reg timed_out;
    reg unmapped;
    reg [31:0] unmapped_pc;
    reg wb_new_q;
    reg x_seen_q;
    reg [31:0] x_pc_q;
    reg [31:0] x_ir_q;
    reg [31:0] dtcm_prev [0:DTCM_WORDS-1];

    always #5 clk = ~clk;

    initial begin
        if (!$value$plusargs("ITCM_HEX=%s", itcm_file)) begin
            $display("ERROR: missing +ITCM_HEX"); $finish;
        end
        if (!$value$plusargs("STOP_PC=%d", stop_pc)) begin
            $display("ERROR: missing +STOP_PC"); $finish;
        end
        if (!$value$plusargs("TRACE=%s", trace_file)) begin
            $display("ERROR: missing +TRACE"); $finish;
        end
        dtcm_fd = 0;
        if ($value$plusargs("DTCM_DUMP=%s", dtcm_file_out))
            dtcm_fd = $fopen(dtcm_file_out, "w");
        if (!$value$plusargs("MAX_CYCLES=%d", max_cycles))
            max_cycles = 2000000;
        if (!$value$plusargs("HALT_ADDR=%d", halt_addr))
            halt_addr = HALT_ADDR_DEFAULT;

        cycles = 0;
        done = 1'b0;
        timed_out = 1'b0;
        unmapped = 1'b0;
        unmapped_pc = 32'b0;
        wb_new_q = 1'b0;
        x_seen_q = 1'b0;
        x_pc_q = 32'b0;
        x_ir_q = 32'b0;
        trace_fd = $fopen(trace_file, "w");

        // deterministic starting memory: NOPs and zeros (Sail RAM is zeroed
        // and only the loaded segments differ)
        for (i = 0; i < ITCM_WORDS; i = i + 1)
            dut.ITCM[i] = 32'h00000013;
        for (i = 0; i < DTCM_WORDS; i = i + 1)
            dut.DTCM[i] = 32'h00000000;
        $readmemh(itcm_file, dut.ITCM);
        if ($value$plusargs("DTCM_HEX=%s", dtcm_file))
            $readmemh(dtcm_file, dut.DTCM);
        for (i = 0; i < DTCM_WORDS; i = i + 1)
            dtcm_prev[i] = dut.DTCM[i];

        rstn = 1'b0;
        repeat (8) @(posedge clk);
        rstn = 1'b1;
    end

    always @(posedge clk) begin
        if (rstn)
            cycles <= cycles + 1;
        // WB receives a new instruction this edge when MEM is valid and the
        // pipeline does not hold it.  The flag makes each instruction appear
        // exactly once in the trace, even while a divider/bus stall freezes
        // the pipeline.
        wb_new_q <= (rstn && !done && !dut.pipe_freeze && dut.MEM_valid);
    end

    always @(posedge clk) begin
        if (rstn && !done) begin
            // one line per completed store
            if (dut.MEM_valid && dut.MEM_mem_write &&
                    (!dut.mem_bus_access || dut.bus_ready_i) && !dut.pipe_freeze) begin
                if (dut.MEM_addr == halt_addr) begin
                    done <= 1'b1;
                end else begin
                    $fwrite(trace_fd, "S %08x %08x %08x %08x %x\n",
                            dut.MEM_addr, dut.MEM_PC, dut.MEM_IR,
                            dut.MEM_wdata, dut.MEM_wstrb);
                end
            end
            // The pipeline is in-order, so stopping when the instruction at
            // `test_done` reaches WB traces exactly the instructions Sail
            // executes before --stop-at-pc: every earlier instruction has
            // already retired, and later ones cannot retire before it.  The
            // stop word itself is not traced.
            if (wb_new_q && dut.WB_valid) begin
                if (dut.WB_PC == stop_pc) begin
                    done <= 1'b1;
                end else begin
                    // The core's WB_rd field is only meaningful together
                    // with WB_rd_write_req; branches and stores can carry a
                    // stale rd, so report rd=0 unless the instruction writes.
                    $fwrite(trace_fd, "I %08x %08x x%0d %08x\n",
                            dut.WB_PC, dut.WB_IR,
                            (dut.WB_rd_write_req && dut.WB_rd != 5'd0) ?
                                dut.WB_rd : 5'd0,
                            (dut.WB_rd_write_req && dut.WB_rd != 5'd0) ?
                                dut.wb_rd_data : 32'b0);
                end
            end
        end
    end

    // Exception events at EX.  The core does not retire a trapping
    // instruction, so these `X <pc> <ir> <mcause> <mtval>` lines are the RTL
    // side of the trap comparison; the edge detector keeps one line per
    // exception while EX is frozen.
    always @(posedge clk) begin
        if (rstn && !done) begin
            if (dut.EX_valid && dut.ex_exception) begin
                if (!(x_seen_q && x_pc_q == dut.EX_PC && x_ir_q == dut.EX_IR)) begin
                    $fwrite(trace_fd, "X %08x %08x %08x %08x\n",
                            dut.EX_PC, dut.EX_IR,
                            dut.ex_exc_cause, dut.ex_exc_tval);
                    x_seen_q <= 1'b1;
                    x_pc_q <= dut.EX_PC;
                    x_ir_q <= dut.EX_IR;
                end
            end else begin
                x_seen_q <= 1'b0;
            end
        end
    end

    // unmapped fetch detection
    always @(posedge clk) begin
        if (rstn && !done && dut.IF_PC[31:24] != 8'h00 && dut.IF_PC[31:24] != 8'h10) begin
            unmapped <= 1'b1;
            unmapped_pc <= dut.IF_PC;
            done <= 1'b1;
        end
    end

    // DTCM write tracking: the generated program only stores to addresses
    // below 0x10004000; a store elsewhere would alias inside the DTCM and be
    // visible here as an unexpected word.
    always @(posedge clk) begin
        if (rstn && !done) begin
            for (i = 0; i < DTCM_WORDS; i = i + 1) begin
                if (dut.DTCM[i] !== dtcm_prev[i]) begin
                    if (dtcm_fd != 0)
                        $fwrite(dtcm_fd, "%08x %08x\n",
                                32'h10000000 + 4 * i, dut.DTCM[i]);
                    dtcm_prev[i] = dut.DTCM[i];
                end
            end
        end
    end

    initial begin
        wait (rstn === 1'b1);
        fork
            begin
                wait (done === 1'b1);
            end
            begin
                wait (cycles > max_cycles);
                timed_out = 1'b1;
            end
        join_any
        disable fork;

        #50;
        if (dtcm_fd != 0) $fclose(dtcm_fd);
        $fclose(trace_fd);
        if (timed_out)
            $display("LOCKSTEP_TIMEOUT cycles=%0d pc=%08x", cycles, dut.IF_PC);
        if (unmapped)
            $display("LOCKSTEP_UNMAPPED_PC pc=%08x", unmapped_pc);
        $display("LOCKSTEP_DONE cycles=%0d pc=%08x timeout=%0d",
                 cycles, dut.IF_PC, timed_out);
        $finish;
    end

endmodule
