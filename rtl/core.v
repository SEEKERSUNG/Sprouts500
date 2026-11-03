// Sprouts500 - minimal 5-stage RV32IMACF core
// - IF/ID/EX/MEM/WB, EX/MEM->ID forwarding and IF-stage branch prediction
// - RVC: compressed instructions are expanded to canonical 32-bit IR in IF;
//   IALIGN=16.  RV32A: LR.W/SC.W and the nine AMO*.W operations, TCM only.
// - Simple data bus for MMIO (mem_ready handshake, byte strobes)
// - Machine-mode traps/interrupts, M extension (1-cycle mul, iterative div)
// - RV32F: FLW/FSW, a 32x32 FP register file, fcsr (frm, fflags), mstatus.FS
//   and FS==Off traps.  FP arithmetic lives in rtl/fp_unit.v: a staged unit
//   (unpack -> arithmetic -> round) that freezes EX until its result is ready,
//   like the iterative divider.  FMA, FDIV and FSQRT are fused/exact.
//
// Memory map (tag = addr[31:28]):
//   0x0xxxxxxx : ITCM (instruction + data), ITCM_WORDS words
//   0x1xxxxxxx : DTCM (data),                 DTCM_WORDS words
//   0x2..0xFxxx: external simple bus

module core #(
    parameter [31:0] RESET_PC        = 32'h0000_0000,
    parameter        ITCM_WORDS      = 8192,
    parameter        DTCM_WORDS      = 4096,
    parameter [31:0] ITCM_BOOT_BYTES = 32'd0,
    parameter        ITCM_INIT_FILE  = ""
)(
    input  wire        clk,
    input  wire        rstn,

    // simple data bus (word based, byte strobes)
    output wire        bus_req_o,
    output wire        bus_we_o,
    output wire [31:0] bus_addr_o,
    output wire [31:0] bus_wdata_o,
    output wire [3:0]  bus_wstrb_o,
    input  wire [31:0] bus_rdata_i,
    input  wire        bus_ready_i,

    // interrupt inputs (level)
    input  wire        timer_irq_i,
    input  wire        ext_irq_i,
    input  wire        soft_irq_i
);

    localparam [31:0] NOP = 32'h0000_0013;

    // opcodes
    localparam [6:0] OP_LUI    = 7'b0110111;
    localparam [6:0] OP_AUIPC  = 7'b0010111;
    localparam [6:0] OP_JAL    = 7'b1101111;
    localparam [6:0] OP_JALR   = 7'b1100111;
    localparam [6:0] OP_BRANCH = 7'b1100011;
    localparam [6:0] OP_LOAD   = 7'b0000011;
    localparam [6:0] OP_STORE  = 7'b0100011;
    localparam [6:0] OP_IMM    = 7'b0010011;
    localparam [6:0] OP        = 7'b0110011;
    localparam [6:0] OP_MISC   = 7'b0001111;
    localparam [6:0] OP_SYSTEM = 7'b1110011;
    localparam [6:0] OP_AMO    = 7'b0101111;
    localparam [6:0] OP_LOAD_FP  = 7'b0000111;
    localparam [6:0] OP_STORE_FP = 7'b0100111;
    localparam [6:0] OP_OP_FP    = 7'b1010011;
    localparam [6:0] OP_MADD     = 7'b1000011;
    localparam [6:0] OP_MSUB     = 7'b1000111;
    localparam [6:0] OP_NMSUB    = 7'b1001011;
    localparam [6:0] OP_NMADD    = 7'b1001111;

    localparam [1:0] DIV_IDLE = 2'd0;
    localparam [1:0] DIV_BUSY = 2'd1;
    localparam [1:0] DIV_DONE = 2'd2;

    localparam ITCM_AW = $clog2(ITCM_WORDS);
    localparam DTCM_AW = $clog2(DTCM_WORDS);

    //--------------------------------------------------------------------------
    // TCM arrays - ITCM has two read ports (IF + data), DTCM has one data port
    //--------------------------------------------------------------------------
    reg [31:0] ITCM [0:ITCM_WORDS-1];
    reg [31:0] DTCM [0:DTCM_WORDS-1];

    // FPGA configurations can initialize a resident UART bootloader directly
    // from the bitstream.  The empty default leaves all existing simulators in
    // charge of loading ITCM through their testbenches.  FPGA block RAM powers
    // up zeroed, so mirror that in simulation before loading the image; an
    // absent application header must compare as zero, not X, on the first boot.
    integer i_itcm_init;
    initial begin
        if (ITCM_INIT_FILE != "") begin
            for (i_itcm_init = 0; i_itcm_init < ITCM_WORDS; i_itcm_init = i_itcm_init + 1)
                ITCM[i_itcm_init] = 32'b0;
            $readmemh(ITCM_INIT_FILE, ITCM);
        end
    end

    reg [31:0] if_rdata_q;
    reg [31:0] mem_itcm_rdata_q;
    reg [31:0] mem_dtcm_rdata_q;
    reg [31:0] bus_rdata_q;

    //--------------------------------------------------------------------------
    // Branch prediction: tagged BTB with 2-bit saturating direction counters,
    // predicted in IF so a correctly predicted taken branch/jump has no EX
    // penalty.  This is performance-only: a wrong or stale prediction is
    // corrected in EX through the normal redirect, and a wrong-path instruction
    // can never reach MEM (EX is in-order).  Only valid bits and counters are
    // reset: valid implies tag/target were written together, and the untagged
    // EX-side counter read needs X-free counters in simulation.
    // With IALIGN=16 the index must include PC[1] and exclude PC[0], so the
    // two halfword lanes of one word are distinct entries, and the tag runs
    // PC[31:7].  Training uses the presented instruction PC (EX_PC).
    //--------------------------------------------------------------------------
    localparam        BTB_IBITS = 6;              // 64 entries
    localparam        BTB_NUM   = (1 << BTB_IBITS);

    reg [31:BTB_IBITS+1] btb_tag    [0:BTB_NUM-1];
    reg [31:0]           btb_target [0:BTB_NUM-1];
    reg [1:0]            btb_cnt    [0:BTB_NUM-1];
    reg [BTB_NUM-1:0]    btb_valid;

    // Four-entry return-address stack.  It is updated only by real instructions
    // in EX, never by speculative IF/ID instructions, so a redirect cannot
    // leave wrong-path pushes or pops behind.  The top is always entry 0.
    reg [31:0] ras_stack [0:3];
    reg [2:0]  ras_count;

    // Return {count, s0, s1, s2, s3} after one older in-flight operation.
    // Used only to predict the current IF return; architectural state is still
    // updated in EX.  A flushed older operation also flushes this IF consumer.
    function [130:0] ras_step;
        input [2:0]  count;
        input [31:0] s0, s1, s2, s3;
        input [31:0] link;
        input        is_call, is_return;
        reg [2:0] c;
        reg [31:0] t0, t1, t2, t3;
        begin
            c = count; t0 = s0; t1 = s1; t2 = s2; t3 = s3;
            if (is_call) begin
                t3 = s2; t2 = s1; t1 = s0; t0 = link;
                c = (count == 3'd4) ? 3'd4 : count + 3'd1;
            end else if (is_return && (count != 0)) begin
                t0 = s1; t1 = s2; t2 = s3;
                c = count - 3'd1;
            end
            ras_step = {c, t0, t1, t2, t3};
        end
    endfunction

    //--------------------------------------------------------------------------
    // IF
    //--------------------------------------------------------------------------
    // IF_PC_q is the PC of the instruction being presented to ID (2-byte
    // granular).  Two words are read every cycle: the word containing
    // IF_PC_q and the following word, so a 32-bit instruction in lane 1 is
    // assembled immediately (one extra ITCM read port) instead of paying a
    // bubble while the next word is fetched.  IF_PC is the address to fetch
    // next.
    reg [31:0] IF_PC_q;
    reg [31:0] if_rdata_hi_q;    // word after the one on if_rdata_q
    reg        IF_pred_taken_q;  // prediction for the presented instruction
    reg [31:0] IF_pred_target_q;

    wire [15:0] if_cur_half       = IF_PC_q[1] ? if_rdata_q[31:16] : if_rdata_q[15:0];
    wire        if_cur_compressed = (if_cur_half[1:0] != 2'b11);
    wire [32:0] if_exp            = rvc_expand(if_cur_half);      // {illegal, insn32}
    wire [31:0] if_raw_insn       = IF_PC_q[1] ?
                                    {if_rdata_hi_q[15:0], if_rdata_q[31:16]} :
                                    if_rdata_q;
    wire [31:0] if_insn_ir        = if_cur_compressed ? if_exp[31:0] : if_raw_insn;
    wire        if_insn_compressed = if_cur_compressed;
    wire [31:0] if_insn_pc         = IF_PC_q;

    // RVC has already been expanded, so this also covers C.JR/C.JALR.  A RAS
    // hit overrides the BTB for returns; an empty stack falls back to the BTB.
    wire if_is_jalr = (if_insn_ir[6:0] == OP_JALR) && (if_insn_ir[14:12] == 3'b000);
    wire if_is_return = if_is_jalr && (if_insn_ir[11:7] == 5'd0) &&
                        ((if_insn_ir[19:15] == 5'd1) || (if_insn_ir[19:15] == 5'd5)) &&
                        (if_insn_ir[31:20] == 12'd0);
    // Forward the effects of older in-flight operations (EX, then ID) so a
    // short function's return sees its call even though the RAS commits in EX.
    wire ex_ras_call_any = EX_valid && EX_jump &&
                           ((EX_opcode == OP_JAL) ||
                            ((EX_opcode == OP_JALR) && (EX_funct3 == 3'b000))) &&
                           ((EX_rd == 5'd1) || (EX_rd == 5'd5));
    wire ex_ras_return_any = EX_valid && EX_jump && (EX_opcode == OP_JALR) &&
                             (EX_funct3 == 3'b000) && (EX_rd == 5'd0) &&
                             ((EX_rs1 == 5'd1) || (EX_rs1 == 5'd5)) &&
                             (EX_imm == 32'd0);
    wire id_ras_call_any = ID_valid &&
                           ((id_opcode == OP_JAL) ||
                            ((id_opcode == OP_JALR) && (id_funct3 == 3'b000))) &&
                           ((id_rd == 5'd1) || (id_rd == 5'd5));
    wire id_ras_return_any = ID_valid && (id_opcode == OP_JALR) &&
                             (id_funct3 == 3'b000) && (id_rd == 5'd0) &&
                             ((id_rs1 == 5'd1) || (id_rs1 == 5'd5)) &&
                             (id_ir[31:20] == 12'd0);
    wire [130:0] ras_ex_view = ras_step(ras_count, ras_stack[0], ras_stack[1],
                                        ras_stack[2], ras_stack[3], EX_PC + ex_insn_len,
                                        ex_ras_call_any, ex_ras_return_any);
    wire [130:0] ras_if_view = ras_step(ras_ex_view[130:128], ras_ex_view[127:96],
                                        ras_ex_view[95:64], ras_ex_view[63:32],
                                        ras_ex_view[31:0],
                                        ID_PC + (ID_is_compressed ? 32'd2 : 32'd4),
                                        id_ras_call_any, id_ras_return_any);
    wire        if_ras_hit = if_is_return && (ras_if_view[130:128] != 0);
    wire        if_pred_taken_eff = if_ras_hit ? 1'b1 : IF_pred_taken_q;
    wire [31:0] if_pred_target_eff = if_ras_hit ? ras_if_view[127:96] : IF_pred_target_q;

    wire        if_hold = pipe_freeze || raw_stall || bus_conflict || mem_amo_active ||
                          id_flush_q;
    wire [31:0] IF_PC =
        redirect ? redirect_pc :
        if_hold ? IF_PC_q :
        if_pred_taken_eff ? if_pred_target_eff :
        IF_PC_q + (if_cur_compressed ? 32'd2 : 32'd4);

    wire [BTB_IBITS-1:0]  if_pc_index = IF_PC[BTB_IBITS:1];
    wire [31:BTB_IBITS+1] if_pc_tag   = IF_PC[31:BTB_IBITS+1];

    wire btb_lu_hit = btb_valid[if_pc_index] && (btb_tag[if_pc_index] == if_pc_tag);
    wire [1:0] btb_lu_cnt = btb_lu_hit ? btb_cnt[if_pc_index] : 2'b00;

    wire        btb_pred_taken  = btb_lu_hit && btb_lu_cnt[1];
    wire [31:0] btb_pred_target = btb_lu_hit ? btb_target[if_pc_index] : 32'b0;

    //--------------------------------------------------------------------------
    // ID
    //--------------------------------------------------------------------------
    reg [31:0] ID_PC;
    reg [31:0] ID_IR;
    reg        ID_valid;
    reg        id_flush_q;
    reg        ID_pred_taken;
    reg [31:0] ID_pred_target;
    reg        ID_is_compressed;  // length 2 (canonical IR is still 32 bits)

    // During a stall the instruction already presented by IF (IF_PC_q /
    // if_rdata_q) must be preserved; otherwise IF would overwrite it with the
    // next fetch while ID is held.  The prediction metadata moves with it, and
    // must come from the registered IF_pred_*_q (the lookup of IF_PC, which is
    // one instruction ahead), never from the live lookup.  With RVC the held
    // entry is the canonical IR, the presented PC, and the compressed flag.
    reg [31:0] IF_ID_IR_hold;
    reg [31:0] IF_ID_PC_hold;
    reg        IF_ID_compressed_hold;
    reg        IF_ID_hold;
    reg        IF_ID_pred_taken_hold;
    reg [31:0] IF_ID_pred_target_hold;
    wire [31:0] id_capture_ir = IF_ID_hold ? IF_ID_IR_hold : if_insn_ir;
    wire [31:0] id_capture_pc = IF_ID_hold ? IF_ID_PC_hold : if_insn_pc;
    wire        id_capture_compressed = IF_ID_hold ? IF_ID_compressed_hold :
                                        if_insn_compressed;
    wire        id_capture_pred_taken  = IF_ID_hold ? IF_ID_pred_taken_hold  : if_pred_taken_eff;
    wire [31:0] id_capture_pred_target = IF_ID_hold ? IF_ID_pred_target_hold : if_pred_target_eff;

    wire [31:0] id_ir = ID_valid ? ID_IR : NOP;

    wire [6:0]  id_opcode = id_ir[6:0];
    wire [2:0]  id_funct3 = id_ir[14:12];
    wire [6:0]  id_funct7 = id_ir[31:25];
    wire [4:0]  id_rs1    = id_ir[19:15];
    wire [4:0]  id_rs2    = id_ir[24:20];
    wire [4:0]  id_rd     = id_ir[11:7];
    wire [4:0]  id_amo_op = id_ir[31:27];   // funct5 for OP_AMO

    wire [31:0] id_imm_i = {{20{id_ir[31]}}, id_ir[31:20]};
    wire [31:0] id_imm_s = {{20{id_ir[31]}}, id_ir[31:25], id_ir[11:7]};
    wire [31:0] id_imm_b = {{19{id_ir[31]}}, id_ir[31], id_ir[7], id_ir[30:25], id_ir[11:8], 1'b0};
    wire [31:0] id_imm_u = {id_ir[31:12], 12'b0};
    wire [31:0] id_imm_j = {{11{id_ir[31]}}, id_ir[31], id_ir[19:12], id_ir[20], id_ir[30:21], 1'b0};

    // CSR address legality
    function csr_valid;
        input [11:0] a;
        begin
            case (a)
                12'h001, 12'h002, 12'h003,          // fflags, frm, fcsr
                12'h300, 12'h301, 12'h304, 12'h305, 12'h310, 12'h340,
                12'h341, 12'h342, 12'h343, 12'h344,
                12'hc00, 12'hc01, 12'hc02, 12'hc80, 12'hc81, 12'hc82,
                12'hf11, 12'hf12, 12'hf13, 12'hf14: csr_valid = 1'b1;
                default: csr_valid = 1'b0;
            endcase
        end
    endfunction

    function csr_writable;
        input [11:0] a;
        begin
            case (a)
                12'h001, 12'h002, 12'h003,          // fflags, frm, fcsr
                12'h300, 12'h304, 12'h305, 12'h340,
                12'h341, 12'h342, 12'h343, 12'h344: csr_writable = 1'b1;
                default: csr_writable = 1'b0;
            endcase
        end
    endfunction

    //--------------------------------------------------------------------------
    // RVC expansion to the canonical 32-bit instruction.  ID/EX/MEM only ever
    // see canonical IR, so the rest of the pipeline is width-agnostic.  A
    // reserved/unsupported compressed encoding expands to {16'b0, ci}: legal
    // 32-bit opcodes all end in 2'b11, so ID's default case raises cause 2
    // with mtval = the zero-extended halfword.  HINTs expand to their harmless
    // 32-bit form (write x0, or rewrite a register with its own value).
    //--------------------------------------------------------------------------
    function [31:0] c_enc_i;
        input [6:0] opc; input [4:0] rd; input [2:0] f3;
        input [4:0] rs1; input [11:0] imm;
        begin c_enc_i = {imm, rs1, f3, rd, opc}; end
    endfunction

    function [31:0] c_enc_s;
        input [4:0] rs2, rs1; input [2:0] f3; input [11:0] imm;
        begin c_enc_s = {imm[11:5], rs2, rs1, f3, imm[4:0], 7'b0100011}; end
    endfunction

    function [31:0] c_enc_s_fp;            // FSW immediate form (opcode 0x27)
        input [4:0] rs2, rs1; input [2:0] f3; input [11:0] imm;
        begin c_enc_s_fp = {imm[11:5], rs2, rs1, f3, imm[4:0], 7'b0100111}; end
    endfunction

    function [31:0] c_enc_b;
        input [4:0] rs1, rs2; input [2:0] f3; input [12:0] imm;
        begin
            c_enc_b = {imm[12], imm[10:5], rs2, rs1, f3,
                       imm[4:1], imm[11], 7'b1100011};
        end
    endfunction

    function [31:0] c_enc_j;
        input [4:0] rd; input [20:0] imm;
        begin
            c_enc_j = {imm[20], imm[10:1], imm[11], imm[19:12], rd, 7'b1101111};
        end
    endfunction

    function [31:0] c_enc_u;
        input [4:0] rd; input [19:0] imm20;
        begin c_enc_u = {imm20, rd, 7'b0110111}; end
    endfunction

    function [31:0] c_enc_r;
        input [4:0] rd, rs1, rs2; input [2:0] f3; input [6:0] f7;
        begin c_enc_r = {f7, rs2, rs1, f3, rd, 7'b0110011}; end
    endfunction

    function [32:0] rvc_expand;                 // {illegal, insn32}
        input [15:0] ci;
        reg [4:0] rd, rs1, rs2;
        reg [31:0] imm;
        reg [19:0] imm20;
        reg [31:0] insn;
        reg        ill;
        begin
            rd = 5'd0; rs1 = 5'd0; rs2 = 5'd0;
            imm = 32'd0; imm20 = 20'd0; insn = 32'd0; ill = 1'b0;
            case (ci[1:0])
            2'b00: case (ci[15:13])
                3'b000: begin                                   // C.ADDI4SPN
                    rd  = {2'b01, ci[4:2]};
                    imm = {22'b0, ci[10:7], ci[12:11], ci[5], ci[6], 2'b00};
                    ill = (imm == 32'd0);
                    insn = c_enc_i(7'b0010011, rd, 3'b000, 5'd2, imm[11:0]);
                end
                3'b010: begin                                   // C.LW
                    rd  = {2'b01, ci[4:2]};
                    rs1 = {2'b01, ci[9:7]};
                    imm = {25'b0, ci[5], ci[12:10], ci[6], 2'b00};
                    insn = c_enc_i(7'b0000011, rd, 3'b010, rs1, imm[11:0]);
                end
                3'b110: begin                                   // C.SW
                    rs1 = {2'b01, ci[9:7]};
                    rs2 = {2'b01, ci[4:2]};
                    imm = {25'b0, ci[5], ci[12:10], ci[6], 2'b00};
                    insn = c_enc_s(rs2, rs1, 3'b010, imm[11:0]);
                end
                3'b011: begin                                   // C.FLW
                    rd  = {2'b01, ci[4:2]};
                    rs1 = {2'b01, ci[9:7]};
                    imm = {25'b0, ci[5], ci[12:10], ci[6], 2'b00};
                    insn = c_enc_i(7'b0000111, rd, 3'b010, rs1, imm[11:0]);
                end
                3'b111: begin                                   // C.FSW
                    rs1 = {2'b01, ci[9:7]};
                    rs2 = {2'b01, ci[4:2]};
                    imm = {25'b0, ci[5], ci[12:10], ci[6], 2'b00};
                    insn = c_enc_s_fp(rs2, rs1, 3'b010, imm[11:0]);
                end
                default: ill = 1'b1;              // C.FLD/C.FSD (no D)
            endcase
            2'b01: case (ci[15:13])
                3'b000: begin                                   // C.NOP/C.ADDI
                    rd = ci[11:7];
                    imm = {{26{ci[12]}}, ci[12], ci[6:2]};
                    // rd=x0 is C.NOP (imm=0) or a HINT: expand to a literal
                    // nop rather than "addi x0, x0, imm", whose write enable on
                    // rd=x0 some assemblers route to a different destination.
                    insn = (rd == 5'd0) ? NOP :
                           c_enc_i(7'b0010011, rd, 3'b000, rd, imm[11:0]);
                end
                3'b001: begin                                   // C.JAL (RV32)
                    imm = {{20{ci[12]}}, ci[12], ci[8], ci[10:9], ci[6], ci[7],
                           ci[2], ci[11], ci[5:3], 1'b0};
                    insn = c_enc_j(5'd1, imm[20:0]);
                end
                3'b010: begin                                   // C.LI
                    rd = ci[11:7];
                    imm = {{26{ci[12]}}, ci[12], ci[6:2]};
                    insn = c_enc_i(7'b0010011, rd, 3'b000, 5'd0, imm[11:0]);
                end
                3'b011: begin
                    if (ci[11:7] == 5'd2) begin                 // C.ADDI16SP
                        imm = {{22{ci[12]}}, ci[12], ci[4:3], ci[5], ci[2],
                               ci[6], 4'b0000};
                        ill = (imm == 32'd0);
                        insn = c_enc_i(7'b0010011, 5'd2, 3'b000, 5'd2, imm[11:0]);
                    end else begin                              // C.LUI
                        imm20 = {{14{ci[12]}}, ci[12], ci[6:2]};
                        // imm=0 is reserved; rd=x0 (imm!=0) is a HINT.
                        ill = ({ci[12], ci[6:2]} == 6'd0);
                        insn = c_enc_u(ci[11:7], imm20);
                    end
                end
                3'b100: begin
                    rd  = {2'b01, ci[9:7]};
                    rs2 = {2'b01, ci[4:2]};
                    case (ci[11:10])
                        2'b00: begin                            // C.SRLI
                            ill = ci[12];
                            insn = c_enc_i(7'b0010011, rd, 3'b101, rd,
                                           {7'b0000000, ci[6:2]});
                        end
                        2'b01: begin                            // C.SRAI
                            ill = ci[12];
                            insn = c_enc_i(7'b0010011, rd, 3'b101, rd,
                                           {7'b0100000, ci[6:2]});
                        end
                        2'b10: begin                            // C.ANDI
                            imm = {{26{ci[12]}}, ci[12], ci[6:2]};
                            insn = c_enc_i(7'b0010011, rd, 3'b111, rd, imm[11:0]);
                        end
                        default: begin
                            if (ci[12]) ill = 1'b1;   // RV64 C.SUBW/ADDW/reserved
                            else case (ci[6:5])
                                2'b00: insn = c_enc_r(rd, rd, rs2, 3'b000, 7'b0100000); // C.SUB
                                2'b01: insn = c_enc_r(rd, rd, rs2, 3'b100, 7'b0000000); // C.XOR
                                2'b10: insn = c_enc_r(rd, rd, rs2, 3'b110, 7'b0000000); // C.OR
                                default: insn = c_enc_r(rd, rd, rs2, 3'b111, 7'b0000000); // C.AND
                            endcase
                        end
                    endcase
                end
                3'b101: begin                                   // C.J
                    imm = {{20{ci[12]}}, ci[12], ci[8], ci[10:9], ci[6], ci[7],
                           ci[2], ci[11], ci[5:3], 1'b0};
                    insn = c_enc_j(5'd0, imm[20:0]);
                end
                default: begin                                  // C.BEQZ/C.BNEZ
                    rs1 = {2'b01, ci[9:7]};
                    imm = {{23{ci[12]}}, ci[12], ci[6:5], ci[2], ci[11:10],
                           ci[4:3], 1'b0};
                    insn = c_enc_b(rs1, 5'd0,
                                   (ci[15:13] == 3'b110) ? 3'b000 : 3'b001,
                                   imm[12:0]);
                end
            endcase
            2'b10: case (ci[15:13])
                3'b000: begin                                   // C.SLLI
                    rd = ci[11:7];
                    ill = ci[12];
                    insn = c_enc_i(7'b0010011, rd, 3'b001, rd,
                                   {7'b0000000, ci[6:2]});
                end
                3'b010: begin                                   // C.LWSP
                    rd = ci[11:7];
                    imm = {24'b0, ci[3:2], ci[12], ci[6:4], 2'b00};
                    ill = (rd == 5'd0);
                    insn = c_enc_i(7'b0000011, rd, 3'b010, 5'd2, imm[11:0]);
                end
                3'b100: begin                                   // CR format
                    rd = ci[11:7]; rs2 = ci[6:2];
                    if (!ci[12]) begin
                        if (rs2 == 5'd0) begin                  // C.JR
                            ill = (rd == 5'd0);
                            insn = c_enc_i(7'b1100111, 5'd0, 3'b000, rd, 12'b0);
                        end else begin                          // C.MV (rd=0 HINT)
                            insn = c_enc_r(rd, 5'd0, rs2, 3'b000, 7'b0000000);
                        end
                    end else if (rs2 != 5'd0) begin             // C.ADD (rd=0 HINT)
                        insn = c_enc_r(rd, rd, rs2, 3'b000, 7'b0000000);
                    end else if (rd == 5'd0) begin
                        insn = 32'h0010_0073;                   // C.EBREAK
                    end else begin                              // C.JALR
                        insn = c_enc_i(7'b1100111, 5'd1, 3'b000, rd, 12'b0);
                    end
                end
                3'b110: begin                                   // C.SWSP
                    rs2 = ci[6:2];
                    imm = {24'b0, ci[8:7], ci[12:9], 2'b00};
                    insn = c_enc_s(rs2, 5'd2, 3'b010, imm[11:0]);
                end
                3'b011: begin                                   // C.FLWSP
                    rd = ci[11:7];
                    imm = {24'b0, ci[3:2], ci[12], ci[6:4], 2'b00};
                    insn = c_enc_i(7'b0000111, rd, 3'b010, 5'd2, imm[11:0]);
                end
                3'b111: begin                                   // C.FSWSP
                    rs2 = ci[6:2];
                    imm = {24'b0, ci[8:7], ci[12:9], 2'b00};
                    insn = c_enc_s_fp(rs2, 5'd2, 3'b010, imm[11:0]);
                end
                default: ill = 1'b1;              // C.FLDSP/C.FSDSP (no D)
            endcase
            default: ill = 1'b1;                  // ci[1:0]=11: not compressed
            endcase
            rvc_expand = ill ? {1'b1, 16'b0, ci} : {1'b0, insn};
        end
    endfunction

    reg        dec_rd_write_req;
    reg        dec_uses_rs1;
    reg        dec_uses_rs2;
    reg        dec_mem_read;
    reg        dec_mem_write;
    reg [2:0]  dec_mem_size;
    reg        dec_mem_unsigned;
    reg        dec_branch;
    reg        dec_jump;
    reg        dec_jalr;
    reg        dec_csr_read;
    reg        dec_csr_write;
    reg [4:0]  dec_csr_uimm;
    reg        dec_illegal;
    reg        dec_ecall;
    reg        dec_ebreak;
    reg        dec_mret;
    reg        dec_fence;
    reg        dec_amo;        // LR.W/SC.W/AMO*.W: load-like result at MEM/WB
    reg        dec_amo_rmw;    // read-modify-write AMO (not LR/SC)
    reg        dec_is_lr;
    reg        dec_is_sc;
    reg [31:0] dec_imm;
    reg [4:0]  dec_shamt;

    // RV32F decode metadata.  dec_fp      : any FP instruction (including a
    //                                         legal one that traps on FS==Off)
    //              dec_fp_op   : OP-FP (0x53); FMA opcodes are tracked too
    //              dec_fp_writes_i : result goes to an integer register (only
    //                                then is dec_rd_write_req set for FP)
    //              dec_fp_rd_write : result goes to the FP file (frd = ir[11:7])
    //              dec_fp_slow : any OP-FP/FMA instruction; rtl/fp_unit.v
    //                            needs start/busy/done (EX freezes meanwhile)
    reg        dec_fp;
    reg        dec_fp_op;
    reg        dec_fp_writes_i;
    reg        dec_fp_rd_write;
    reg        dec_fp_slow;
    reg [4:0]  dec_fp_frd;
    reg        dec_fp_uses_frs1;
    reg        dec_fp_uses_frs2;
    reg        dec_fp_uses_frs3;
    reg        dec_fp_uses_rm;
    reg        dec_fp_fflags_en;  // this instruction updates fflags
    reg        dec_fcsr_access;   // CSR read/write of fflags/frm/fcsr

    // rm legality for rm-dependent ops: rm == 7 (111) is DYN and must select a
    // real frm mode; 5/6 (101/110) are reserved and illegal.
    wire dec_fp_rm_ok = (id_ir[14:12] == 3'd7) ? (frm < 3'd5)
                                               : (id_ir[14:12] <= 3'd4);

    always @(*) begin
        dec_rd_write_req = 1'b0;
        dec_uses_rs1     = 1'b0;
        dec_uses_rs2     = 1'b0;
        dec_mem_read     = 1'b0;
        dec_mem_write    = 1'b0;
        dec_mem_size     = 3'd0;
        dec_mem_unsigned = 1'b0;
        dec_branch       = 1'b0;
        dec_jump         = 1'b0;
        dec_jalr         = 1'b0;
        dec_csr_read     = 1'b0;
        dec_csr_write    = 1'b0;
        dec_csr_uimm     = id_ir[19:15];
        dec_illegal      = 1'b1;
        dec_ecall        = 1'b0;
        dec_ebreak       = 1'b0;
        dec_mret         = 1'b0;
        dec_fence        = 1'b0;
        dec_amo          = 1'b0;
        dec_amo_rmw      = 1'b0;
        dec_is_lr        = 1'b0;
        dec_is_sc        = 1'b0;
        dec_imm          = 32'b0;
        dec_shamt        = id_ir[24:20];
        dec_fp           = 1'b0;
        dec_fp_op        = 1'b0;
        dec_fp_writes_i  = 1'b0;
        dec_fp_rd_write  = 1'b0;
        dec_fp_slow      = 1'b0;
        dec_fp_frd       = id_ir[11:7];
        dec_fp_uses_frs1 = 1'b0;
        dec_fp_uses_frs2 = 1'b0;
        dec_fp_uses_frs3 = 1'b0;
        dec_fp_uses_rm   = 1'b0;
        dec_fp_fflags_en = 1'b0;
        dec_fcsr_access  = 1'b0;

        case (id_opcode)
            OP_LUI: begin
                dec_rd_write_req = 1'b1;
                dec_imm = id_imm_u;
                dec_illegal = 1'b0;
            end
            OP_AUIPC: begin
                dec_rd_write_req = 1'b1;
                dec_imm = id_imm_u;
                dec_illegal = 1'b0;
            end
            OP_JAL: begin
                dec_rd_write_req = 1'b1;
                dec_jump = 1'b1;
                dec_imm = id_imm_j;
                dec_illegal = 1'b0;
            end
            OP_JALR: begin
                dec_uses_rs1 = 1'b1;
                dec_rd_write_req = 1'b1;
                dec_jump = 1'b1;
                dec_jalr = 1'b1;
                dec_imm = id_imm_i;
                dec_illegal = (id_funct3 == 3'b000) ? 1'b0 : 1'b1;
            end
            OP_BRANCH: begin
                dec_uses_rs1 = 1'b1;
                dec_uses_rs2 = 1'b1;
                dec_branch = 1'b1;
                dec_imm = id_imm_b;
                case (id_funct3)
                    3'b000, 3'b001, 3'b100, 3'b101, 3'b110, 3'b111: dec_illegal = 1'b0;
                    default: dec_illegal = 1'b1;
                endcase
            end
            OP_LOAD: begin
                dec_uses_rs1 = 1'b1;
                dec_rd_write_req = 1'b1;
                dec_mem_read = 1'b1;
                dec_imm = id_imm_i;
                case (id_funct3)
                    3'b000: begin dec_mem_size = 3'd1; dec_mem_unsigned = 1'b0; dec_illegal = 1'b0; end
                    3'b001: begin dec_mem_size = 3'd2; dec_mem_unsigned = 1'b0; dec_illegal = 1'b0; end
                    3'b010: begin dec_mem_size = 3'd4; dec_mem_unsigned = 1'b0; dec_illegal = 1'b0; end
                    3'b100: begin dec_mem_size = 3'd1; dec_mem_unsigned = 1'b1; dec_illegal = 1'b0; end
                    3'b101: begin dec_mem_size = 3'd2; dec_mem_unsigned = 1'b1; dec_illegal = 1'b0; end
                    default: dec_illegal = 1'b1;
                endcase
            end
            OP_STORE: begin
                dec_uses_rs1 = 1'b1;
                dec_uses_rs2 = 1'b1;
                dec_mem_write = 1'b1;
                dec_imm = id_imm_s;
                case (id_funct3)
                    3'b000: begin dec_mem_size = 3'd1; dec_illegal = 1'b0; end
                    3'b001: begin dec_mem_size = 3'd2; dec_illegal = 1'b0; end
                    3'b010: begin dec_mem_size = 3'd4; dec_illegal = 1'b0; end
                    default: dec_illegal = 1'b1;
                endcase
            end
            OP_IMM: begin
                dec_uses_rs1 = 1'b1;
                dec_rd_write_req = 1'b1;
                dec_imm = id_imm_i;
                case (id_funct3)
                    3'b000, 3'b010, 3'b011, 3'b100, 3'b110, 3'b111: dec_illegal = 1'b0;
                    3'b001: dec_illegal = (id_funct7 == 7'b0000000) ? 1'b0 : 1'b1;
                    3'b101: dec_illegal = ((id_funct7 == 7'b0000000) ||
                                          (id_funct7 == 7'b0100000)) ? 1'b0 : 1'b1;
                    default: dec_illegal = 1'b1;
                endcase
            end
            OP: begin
                dec_uses_rs1 = 1'b1;
                dec_uses_rs2 = 1'b1;
                dec_rd_write_req = 1'b1;
                case (id_funct3)
                    3'b000: dec_illegal = ((id_funct7 == 7'b0000000) ||
                                          (id_funct7 == 7'b0100000) ||
                                          (id_funct7 == 7'b0000001)) ? 1'b0 : 1'b1;
                    3'b001, 3'b010, 3'b011: dec_illegal =
                        ((id_funct7 == 7'b0000000) ||
                         (id_funct7 == 7'b0000001)) ? 1'b0 : 1'b1;
                    3'b100, 3'b110, 3'b111: dec_illegal =
                        ((id_funct7 == 7'b0000000) ||
                         (id_funct7 == 7'b0000001)) ? 1'b0 : 1'b1;
                    3'b101: dec_illegal = ((id_funct7 == 7'b0000000) ||
                                          (id_funct7 == 7'b0100000) ||
                                          (id_funct7 == 7'b0000001)) ? 1'b0 : 1'b1;
                    default: dec_illegal = 1'b1;
                endcase
            end
            OP_LOAD_FP, OP_STORE_FP: begin
                // Only FLW/FSW (funct3=010); every other funct3 is a Zfh/Q form
                // or a reserved encoding.  The FP file value of rs2 is what
                // FP-unit/EX selects when EX_fp_store; dec_uses_rs2 stays 0 so
                // integer forwarding never looks at it.
                dec_fp        = 1'b1;
                dec_uses_rs1  = 1'b1;
                dec_mem_size  = 3'd4;
                dec_imm       = (id_opcode == OP_LOAD_FP) ? id_imm_i : id_imm_s;
                if (id_funct3 != 3'b010) begin
                    dec_illegal = 1'b1;
                end else if (id_opcode == OP_LOAD_FP) begin
                    dec_mem_read = 1'b1;
                    dec_fp_rd_write = 1'b1;
                    dec_illegal  = 1'b0;
                end else begin
                    dec_mem_write = 1'b1;
                    dec_fp_uses_frs2 = 1'b1;          // FSW stores an FP register
                    dec_illegal  = 1'b0;
                end
            end
            OP_OP_FP: begin
                // OP-FP is decoded by funct7 (the S-format encoding); funct3
                // is the rounding mode for arithmetic/conversion forms and the
                // operation selector for FSGNJ/FMIN/FMV/FCLASS/compare.
                dec_fp    = 1'b1;
                dec_fp_op = 1'b1;
                dec_fp_slow = 1'b1;
                dec_fp_fflags_en = 1'b1;
                case (id_funct7)
                    7'b0000000: begin                   // FADD.S
                        dec_fp_rd_write = 1'b1;
                        dec_fp_uses_frs1 = 1'b1;
                        dec_fp_uses_frs2 = 1'b1;
                        dec_fp_uses_rm   = 1'b1;
                        dec_illegal = !dec_fp_rm_ok;
                    end
                    7'b0000100: begin                   // FSUB.S
                        dec_fp_rd_write = 1'b1;
                        dec_fp_uses_frs1 = 1'b1;
                        dec_fp_uses_frs2 = 1'b1;
                        dec_fp_uses_rm   = 1'b1;
                        dec_illegal = !dec_fp_rm_ok;
                    end
                    7'b0001000: begin                   // FMUL.S
                        dec_fp_rd_write = 1'b1;
                        dec_fp_uses_frs1 = 1'b1;
                        dec_fp_uses_frs2 = 1'b1;
                        dec_fp_uses_rm   = 1'b1;
                        dec_illegal = !dec_fp_rm_ok;
                    end
                    7'b0001100: begin                   // FDIV.S
                        dec_fp_rd_write = 1'b1;
                        dec_fp_uses_frs1 = 1'b1;
                        dec_fp_uses_frs2 = 1'b1;
                        dec_fp_uses_rm   = 1'b1;
                        dec_fp_slow     = 1'b1;
                        dec_illegal = !dec_fp_rm_ok;
                    end
                    7'b0101100: begin                   // FSQRT.S
                        dec_fp_rd_write = 1'b1;
                        dec_fp_uses_frs1 = 1'b1;
                        dec_fp_uses_rm   = 1'b1;
                        dec_fp_slow     = 1'b1;
                        dec_illegal = (id_rs2 != 5'd0) || !dec_fp_rm_ok;
                    end
                    7'b0010000: begin                   // FSGNJ/N/X
                        dec_fp_rd_write = 1'b1;
                        dec_fp_uses_frs1 = 1'b1;
                        dec_fp_uses_frs2 = 1'b1;
                        dec_fp_fflags_en = 1'b0;
                        dec_illegal = (id_funct3 > 3'b010);
                    end
                    7'b0010100: begin                   // FMIN/FMAX
                        dec_fp_rd_write = 1'b1;
                        dec_fp_uses_frs1 = 1'b1;
                        dec_fp_uses_frs2 = 1'b1;
                        dec_illegal = (id_funct3 > 3'b001);
                    end
                    7'b1010000: begin                   // FEQ/FLT/FLE
                        dec_fp_writes_i  = 1'b1;
                        dec_fp_uses_frs1 = 1'b1;
                        dec_fp_uses_frs2 = 1'b1;
                        dec_illegal = (id_funct3 > 3'b010);
                    end
                    7'b1100000: begin                   // FCVT.W/WU.S (rs2)
                        dec_fp_writes_i  = 1'b1;
                        dec_fp_uses_frs1 = 1'b1;
                        dec_fp_uses_rm   = 1'b1;
                        dec_illegal = (id_rs2 > 5'd1) || !dec_fp_rm_ok;
                    end
                    7'b1101000: begin                   // FCVT.S.W/WU (rs2)
                        dec_fp_rd_write = 1'b1;
                        dec_uses_rs1    = 1'b1;         // integer source
                        dec_fp_uses_rm  = 1'b1;
                        dec_illegal = (id_rs2 > 5'd1) || !dec_fp_rm_ok;
                    end
                    7'b1110000: begin                   // FMV.X.W / FCLASS.S
                        dec_fp_writes_i  = 1'b1;
                        dec_fp_uses_frs1 = 1'b1;
                        dec_fp_fflags_en = 1'b0;
                        dec_illegal = (id_rs2 != 5'd0) || (id_funct3 > 3'b001);
                    end
                    7'b1111000: begin                   // FMV.W.X
                        dec_fp_rd_write = 1'b1;
                        dec_uses_rs1    = 1'b1;         // integer source
                        dec_fp_fflags_en = 1'b0;
                        dec_illegal = (id_rs2 != 5'd0) || (id_funct3 != 3'b000);
                    end
                    default: dec_illegal = 1'b1;
                endcase
                if (dec_fp_writes_i)
                    dec_rd_write_req = 1'b1;
            end
            OP_MADD, OP_MSUB, OP_NMSUB, OP_NMADD: begin
                // FMADD/FMSUB/FNMSUB/FNMADD.S: rd = rs1*rs2 +- rs3 in funct3=rm.
                // The FP unit computes the sign of the product from the
                // opcode, so nothing extra is decoded here.
                dec_fp           = 1'b1;
                dec_fp_rd_write  = 1'b1;
                dec_fp_uses_frs1 = 1'b1;
                dec_fp_uses_frs2 = 1'b1;
                dec_fp_uses_frs3 = 1'b1;
                dec_fp_uses_rm   = 1'b1;
                dec_fp_fflags_en = 1'b1;
                dec_fp_slow      = 1'b1;
                dec_illegal      = (id_ir[26:25] != 2'b00) || !dec_fp_rm_ok;
            end
            OP_AMO: begin
                // RV32A, .W only (funct3=010); aq/rl are accepted and ignored
                // (single hart, in-order memory).  AMO/SC set dec_mem_write so
                // the generic 4-byte misalignment check raises cause 6; LR uses
                // the load path (cause 4).
                dec_uses_rs1     = 1'b1;
                dec_rd_write_req = 1'b1;
                dec_amo          = 1'b1;
                dec_mem_size     = 3'd4;
                if (id_funct3 != 3'b010) begin
                    dec_illegal = 1'b1;
                end else case (id_amo_op)
                    5'b00010: begin                     // LR.W
                        dec_is_lr   = 1'b1;
                        dec_mem_read = 1'b1;
                        dec_illegal = (id_rs2 != 5'd0);
                    end
                    5'b00011: begin                     // SC.W
                        dec_is_sc    = 1'b1;
                        dec_uses_rs2 = 1'b1;
                        dec_mem_write = 1'b1;
                        dec_illegal  = 1'b0;
                    end
                    5'b00000, 5'b00001, 5'b00100, 5'b01000, 5'b01100,
                    5'b10000, 5'b10100, 5'b11000, 5'b11100: begin
                        dec_amo_rmw  = 1'b1;
                        dec_uses_rs2 = 1'b1;
                        dec_mem_write = 1'b1;
                        dec_illegal  = 1'b0;
                    end
                    default: dec_illegal = 1'b1;         // incl. AMOCAS.W (Zacas)
                endcase
            end
            OP_MISC: begin
                if ((id_funct3 == 3'b000) || (id_funct3 == 3'b001)) begin
                    dec_fence = 1'b1;
                    dec_illegal = 1'b0;
                end
            end
            OP_SYSTEM: begin
                case (id_funct3)
                    3'b000: begin
                        if ((id_ir[31:20] == 12'h000) && (id_rs1 == 5'd0) && (id_rd == 5'd0)) begin
                            dec_ecall = 1'b1;
                            dec_illegal = 1'b0;
                        end else if ((id_ir[31:20] == 12'h001) && (id_rs1 == 5'd0) && (id_rd == 5'd0)) begin
                            dec_ebreak = 1'b1;
                            dec_illegal = 1'b0;
                        end else if ((id_ir[31:20] == 12'h302) && (id_rs1 == 5'd0) && (id_rd == 5'd0)) begin
                            dec_mret = 1'b1;
                            dec_illegal = 1'b0;
                        end else if ((id_ir[31:20] == 12'h105) && (id_rs1 == 5'd0) && (id_rd == 5'd0)) begin
                            dec_fence = 1'b1;      // WFI treated as NOP
                            dec_illegal = 1'b0;
                        end
                    end
                    3'b001, 3'b010, 3'b011: begin
                        dec_uses_rs1 = 1'b1;
                        dec_rd_write_req = 1'b1;
                        dec_csr_read = 1'b1;
                        dec_csr_write = 1'b1;
                        if (((id_funct3 == 3'b010) || (id_funct3 == 3'b011)) && (id_rs1 == 5'd0))
                            dec_csr_write = 1'b0;
                        // Any access to fcsr/fflags/frm is an FP instruction for
                        // the mstatus.FS==Off illegal-instruction check.
                        if ((id_ir[31:20] == 12'h001) || (id_ir[31:20] == 12'h002) ||
                            (id_ir[31:20] == 12'h003))
                            dec_fcsr_access = 1'b1;
                        if (!csr_valid(id_ir[31:20]))
                            dec_illegal = 1'b1;
                        else if (dec_csr_write && !csr_writable(id_ir[31:20]))
                            dec_illegal = 1'b1;
                        else
                            dec_illegal = 1'b0;
                    end
                    3'b101, 3'b110, 3'b111: begin
                        dec_rd_write_req = 1'b1;
                        dec_csr_read = 1'b1;
                        dec_csr_write = 1'b1;
                        if (((id_funct3 == 3'b110) || (id_funct3 == 3'b111)) && (id_ir[19:15] == 5'd0))
                            dec_csr_write = 1'b0;
                        if ((id_ir[31:20] == 12'h001) || (id_ir[31:20] == 12'h002) ||
                            (id_ir[31:20] == 12'h003))
                            dec_fcsr_access = 1'b1;
                        if (!csr_valid(id_ir[31:20]))
                            dec_illegal = 1'b1;
                        else if (dec_csr_write && !csr_writable(id_ir[31:20]))
                            dec_illegal = 1'b1;
                        else
                            dec_illegal = 1'b0;
                    end
                    default: dec_illegal = 1'b1;
                endcase
            end
            default: dec_illegal = 1'b1;
        endcase
    end

    //--------------------------------------------------------------------------
    // EX
    //--------------------------------------------------------------------------
    reg [31:0] EX_PC;
    reg [31:0] EX_IR;
    reg        EX_valid;
    reg        EX_pred_taken;    // prediction this instruction was fetched with
    reg [31:0] EX_pred_target;
    reg [6:0]  EX_opcode;
    reg [2:0]  EX_funct3;
    reg [6:0]  EX_funct7;
    reg [4:0]  EX_rs1;
    reg [4:0]  EX_rs2;
    reg [4:0]  EX_rd;
    reg [31:0] EX_rs1_data;
    reg [31:0] EX_rs2_data;
    reg [31:0] EX_imm;
    reg [4:0]  EX_shamt;
    reg        EX_is_compressed;
    reg        EX_amo;
    reg        EX_amo_rmw;
    reg        EX_is_lr;
    reg        EX_is_sc;
    reg [4:0]  EX_amo_op;
    reg        EX_rd_write_req;
    reg        EX_mem_read;
    reg        EX_mem_write;
    reg [2:0]  EX_mem_size;
    reg        EX_mem_unsigned;
    reg        EX_branch;
    reg        EX_jump;
    reg        EX_jalr;
    reg        EX_csr_read;
    reg        EX_csr_write;
    reg [11:0] EX_csr_addr;
    reg [4:0]  EX_csr_uimm;
    reg        EX_illegal;
    reg        EX_ecall;
    reg        EX_ebreak;
    reg        EX_mret;
    reg        EX_fence;
    reg [31:0] EX_addr;
    reg [31:0] EX_wdata;
    reg [3:0]  EX_wstrb;
    reg [1:0]  EX_load_src;

    // FP execution metadata carried with the instruction.  EX_fp_rd_write marks
    // a write to the FP file at WB (frd = EX_frd); it is deliberately separate
    // from EX_rd_write_req, which stays 0 for those instructions so integer
    // forwarding/hazards never see an FP-file write.
    reg        EX_fp;
    reg        EX_fp_rd_write;
    reg        EX_fp_slow;
    reg        EX_fp_uses_rm;
    reg        EX_fp_fflags_en;
    reg        EX_fcsr_access;
    reg [4:0]  EX_frd;
    reg [31:0] EX_frs1_q;
    reg [31:0] EX_frs2_q;
    reg [31:0] EX_frs3_q;

    wire       EX_fp_load  = EX_fp && EX_mem_read;    // FLW
    wire       EX_fp_store = EX_fp && EX_mem_write;   // FSW

    wire [31:0] ex_rs1 = EX_rs1_data;
    wire [31:0] ex_rs2 = EX_rs2_data;

    //--------------------------------------------------------------------------
    // FP unit hook
    //--------------------------------------------------------------------------
    // FP unit hook: rtl/fp_unit.v is combinational for all ops except FDIV/FSQRT,
    // which use start/busy/done and freeze the pipeline.  It decodes the operation
    // from the canonical instruction word itself (EX_IR), so core.v only supplies
    // operands and the effective rounding mode.  fflags are valid with the result.
    wire [31:0] fp_result;      // F32 pattern, or 32-bit integer result
    wire [4:0]  fp_fflags;      // accrued exception bits for this instruction
    wire        fp_busy, fp_done;
    wire [2:0]  ex_fp_rm;       // effective rm (register form -> frm; 101..111 illegal)
    wire [31:0] EX_frs1, EX_frs2, EX_frs3;   // FP operands (FMA rs3 = EX_IR[31:27])
    reg         EX_fp_launched_q;
    wire        fp_start;

    assign EX_frs1 = EX_frs1_q;
    assign EX_frs2 = EX_frs2_q;
    assign EX_frs3 = EX_frs3_q;
    assign ex_fp_rm = (EX_IR[14:12] == 3'b111) ? frm : EX_IR[14:12];

    // One launch pulse per slow (FDIV/FSQRT) instruction.  The unit holds
    // done/result until the next start, so an unrelated bus/divider freeze
    // cannot lose the completed result.  Illegal and FS==Off instructions must
    // not start the unit: their trap must not wait for an iteration that would
    // never be architecturally visible.
    assign fp_start = EX_valid && EX_fp && EX_fp_slow && !EX_fp_launched_q &&
                      !EX_illegal && !ex_fp_fs_off && !ex_fp_rm_bad;

    fp_unit u_fp (
        .clk(clk), .rstn(rstn),
        .insn(EX_IR), .rm(ex_fp_rm),
        .a(EX_frs1), .b(EX_frs2), .c(EX_frs3),
        .start(fp_start),
        .busy(fp_busy), .done(fp_done),
        .result(fp_result), .fflags(fp_fflags)
    );

    wire [31:0] ex_store_data = EX_fp_store ? EX_frs2 : ex_rs2;

    // Explicit arithmetic right shift (avoid signedness surprises in
    // conditional expressions).
    wire [4:0]  ex_shamt = EX_shamt;
    wire [4:0]  ex_shamt_r = ex_rs2[4:0];
    wire [31:0] ex_sra_i = (ex_rs1 >> ex_shamt) |
                           ({32{ex_rs1[31]}} & ~(32'hffff_ffff >> ex_shamt));
    wire [31:0] ex_sra_r = (ex_rs1 >> ex_shamt_r) |
                           ({32{ex_rs1[31]}} & ~(32'hffff_ffff >> ex_shamt_r));

    wire ex_div_signed = (EX_funct3 == 3'b100) || (EX_funct3 == 3'b110);
    wire ex_is_muldiv = (EX_opcode == OP) && (EX_funct7 == 7'b0000001);
    wire ex_is_mul    = ex_is_muldiv && !EX_funct3[2];
    wire ex_is_div    = ex_is_muldiv &&  EX_funct3[2];

    // M extension - multiply
    wire signed [63:0] mul_a_s = {{32{ex_rs1[31]}}, ex_rs1};
    wire signed [63:0] mul_b_s = {{32{ex_rs2[31]}}, ex_rs2};
    wire signed [63:0] mul_ss  = mul_a_s * mul_b_s;
    wire signed [63:0] mul_su  = mul_a_s * $signed({32'b0, ex_rs2});
    wire        [63:0] mul_uu  = {32'b0, ex_rs1} * {32'b0, ex_rs2};

    reg [31:0] ex_mul_result;
    always @(*) begin
        case (EX_funct3)
            3'b000: ex_mul_result = mul_ss[31:0];
            3'b001: ex_mul_result = mul_ss[63:32];
            3'b010: ex_mul_result = mul_su[63:32];
            3'b011: ex_mul_result = mul_uu[63:32];
            default: ex_mul_result = 32'b0;
        endcase
    end

    // M extension - sequential divider
    reg [1:0]  div_state;
    reg [4:0]  div_cnt;
    reg [31:0] div_a_orig;
    reg [31:0] div_b_orig;
    reg [31:0] div_dividend;
    reg [31:0] div_divisor;
    reg [31:0] div_quot;
    reg [32:0] div_rem;
    reg        div_sign_q;
    reg        div_sign_r;
    reg        div_by_zero;
    reg        div_overflow;

    wire [31:0] ex_rs1_abs = (ex_div_signed && ex_rs1[31]) ? (~ex_rs1 + 32'd1) : ex_rs1;
    wire [31:0] ex_rs2_abs = (ex_div_signed && ex_rs2[31]) ? (~ex_rs2 + 32'd1) : ex_rs2;

    wire [32:0] div_rem_shift = {div_rem[31:0], div_dividend[31]};
    wire        div_ge        = (div_rem_shift >= {1'b0, div_divisor});
    wire [32:0] div_rem_next  = div_ge ? (div_rem_shift - {1'b0, div_divisor}) : div_rem_shift;
    wire [31:0] div_quot_next = {div_quot[30:0], div_ge};

    wire [31:0] div_q = div_by_zero ? 32'hffff_ffff :
                        div_overflow ? 32'h8000_0000 :
                        (div_sign_q ? (~div_quot + 32'd1) : div_quot);
    wire [31:0] div_r = div_by_zero ? div_a_orig :
                        div_overflow ? 32'b0 :
                        (div_sign_r ? (~div_rem[31:0] + 32'd1) : div_rem[31:0]);
    wire [31:0] div_result_comb = EX_funct3[1] ? div_r : div_q;

    wire div_start = ex_is_div && EX_valid && (div_state == DIV_IDLE);
    wire div_busy  = (div_state == DIV_BUSY) ||
                     ((div_state == DIV_IDLE) && ex_is_div && EX_valid);

    // branch / jump
    reg ex_branch_taken;
    always @(*) begin
        case (EX_funct3)
            3'b000: ex_branch_taken = (ex_rs1 == ex_rs2);
            3'b001: ex_branch_taken = (ex_rs1 != ex_rs2);
            3'b100: ex_branch_taken = ($signed(ex_rs1) <  $signed(ex_rs2));
            3'b101: ex_branch_taken = ($signed(ex_rs1) >= $signed(ex_rs2));
            3'b110: ex_branch_taken = (ex_rs1 <  ex_rs2);
            3'b111: ex_branch_taken = (ex_rs1 >= ex_rs2);
            default: ex_branch_taken = 1'b0;
        endcase
    end

    wire [31:0] ex_target = EX_jalr ? ((ex_rs1 + EX_imm) & ~32'b1)
                                    : (EX_PC + EX_imm);
    // Link value and stale-BTB fall-through must use the real instruction
    // length: a canonical compressed IR still links PC+2.
    wire [31:0] ex_insn_len = EX_is_compressed ? 32'd2 : 32'd4;
    wire ex_control = EX_valid && (EX_jump || (EX_branch && ex_branch_taken));
    // IALIGN=16: only bit 0 can be misaligned (JAL/B offsets are even, JALR
    // clears bit 0), so cause 0 is architecturally unreachable.
    wire ex_target_misaligned = ex_control && ex_target[0];

    // memory address / store strobes
    wire ex_mem_access = EX_valid && (EX_mem_read || EX_mem_write || EX_amo);
    wire ex_misaligned = ex_mem_access &&
                         (((EX_mem_size == 3'd2) && EX_addr[0]) ||
                          ((EX_mem_size == 3'd4) && (EX_addr[1:0] != 2'b00)));

    always @(*) begin
        EX_wdata = 32'b0;
        EX_wstrb = 4'b0000;
        EX_load_src = 2'd0;
        EX_addr = ex_rs1 + EX_imm;

        if (EX_mem_write) begin
            case (EX_mem_size)
                3'd1: begin
                    EX_wdata = {24'b0, ex_store_data[7:0]};
                    EX_wstrb = 4'b0001;
                    case (EX_addr[1:0])
                        2'b00: begin EX_wdata = {24'b0, ex_store_data[7:0]};              EX_wstrb = 4'b0001; end
                        2'b01: begin EX_wdata = {16'b0, ex_store_data[7:0], 8'b0};        EX_wstrb = 4'b0010; end
                        2'b10: begin EX_wdata = {8'b0, ex_store_data[7:0], 16'b0};        EX_wstrb = 4'b0100; end
                        default: begin EX_wdata = {ex_store_data[7:0], 24'b0};            EX_wstrb = 4'b1000; end
                    endcase
                end
                3'd2: begin
                    EX_wdata = {16'b0, ex_store_data[15:0]};
                    EX_wstrb = 4'b0011;
                    case (EX_addr[1])
                        1'b0: begin EX_wdata = {16'b0, ex_store_data[15:0]};              EX_wstrb = 4'b0011; end
                        default: begin EX_wdata = {ex_store_data[15:0], 16'b0};           EX_wstrb = 4'b1100; end
                    endcase
                end
                default: begin
                    EX_wdata = ex_store_data;
                    EX_wstrb = 4'b1111;
                end
            endcase
        end else if (EX_mem_read) begin
            EX_load_src = (EX_addr[31:28] == 4'h0) ? 2'd1 :
                          (EX_addr[31:28] == 4'h1) ? 2'd2 : 2'd3;
        end
    end

    // CSR read / write data
    reg [31:0] csr_rdata;
    reg [31:0] csr_wdata;
    reg [31:0] mstatus, mie, mtvec, mscratch, mepc, mcause, mtval;
    reg [63:0] cycle_cnt;
    reg [63:0] instret_cnt;

    // fcsr 0x003 = {frm[7:5], fflags[4:0]}.  Kept as one 8-bit register so the
    // CSRs read back as one value; only its defined bits are ever written.
    reg [7:0]  fcsr;
    wire [2:0] frm    = fcsr[7:5];
    wire [4:0] fflags = fcsr[4:0];
    wire [1:0] mstatus_fs = mstatus[14:13];

    wire [31:0] mip_comb = {20'b0, ext_irq_i, 3'b0, timer_irq_i, 3'b0, soft_irq_i, 3'b0};

    always @(*) begin
        case (EX_csr_addr)
            12'h001: csr_rdata = {27'b0, fflags};
            12'h002: csr_rdata = {29'b0, frm};
            12'h003: csr_rdata = {24'b0, fcsr};
            12'h300: csr_rdata = mstatus;
            12'h301: csr_rdata = 32'h4000_1125;   // MXL=1, I, M, A, F, C
            12'h304: csr_rdata = mie;
            12'h305: csr_rdata = mtvec;
            12'h310: csr_rdata = 32'b0;           // mstatush
            12'h340: csr_rdata = mscratch;
            12'h341: csr_rdata = mepc;
            12'h342: csr_rdata = mcause;
            12'h343: csr_rdata = mtval;
            12'h344: csr_rdata = mip_comb;
            12'hc00: csr_rdata = cycle_cnt[31:0];
            12'hc01: csr_rdata = cycle_cnt[31:0]; // time = cycle
            12'hc02: csr_rdata = instret_cnt[31:0];
            12'hc80: csr_rdata = cycle_cnt[63:32];
            12'hc81: csr_rdata = cycle_cnt[63:32];
            12'hc82: csr_rdata = instret_cnt[63:32];
            default: csr_rdata = 32'b0;           // f11..f14 are zero
        endcase
    end

    always @(*) begin
        case (EX_funct3)
            3'b001: csr_wdata = ex_rs1;
            3'b010: csr_wdata = csr_rdata | ex_rs1;
            3'b011: csr_wdata = csr_rdata & ~ex_rs1;
            3'b101: csr_wdata = {27'b0, EX_csr_uimm};
            3'b110: csr_wdata = csr_rdata | {27'b0, EX_csr_uimm};
            3'b111: csr_wdata = csr_rdata & ~{27'b0, EX_csr_uimm};
            default: csr_wdata = 32'b0;
        endcase
    end

    // ALU result
    reg [31:0] ex_alu_result;
    always @(*) begin
        case (EX_opcode)
            OP_LUI:   ex_alu_result = EX_imm;
            OP_AUIPC: ex_alu_result = EX_PC + EX_imm;
            OP_JAL:   ex_alu_result = EX_PC + ex_insn_len;
            OP_JALR:  ex_alu_result = EX_PC + ex_insn_len;
            OP_IMM: begin
                case (EX_funct3)
                    3'b000: ex_alu_result = ex_rs1 + EX_imm;
                    3'b010: ex_alu_result = ($signed(ex_rs1) < $signed(EX_imm)) ? 32'd1 : 32'd0;
                    3'b011: ex_alu_result = (ex_rs1 < EX_imm) ? 32'd1 : 32'd0;
                    3'b100: ex_alu_result = ex_rs1 ^ EX_imm;
                    3'b110: ex_alu_result = ex_rs1 | EX_imm;
                    3'b111: ex_alu_result = ex_rs1 & EX_imm;
                    3'b001: ex_alu_result = ex_rs1 << EX_shamt;
                    3'b101: ex_alu_result = (EX_funct7 == 7'b0100000) ?
                                            ex_sra_i : (ex_rs1 >> EX_shamt);
                    default: ex_alu_result = 32'b0;
                endcase
            end
            OP: begin
                case (EX_funct3)
                    3'b000: ex_alu_result = (EX_funct7 == 7'b0100000) ? (ex_rs1 - ex_rs2) : (ex_rs1 + ex_rs2);
                    3'b001: ex_alu_result = ex_rs1 << ex_rs2[4:0];
                    3'b010: ex_alu_result = ($signed(ex_rs1) < $signed(ex_rs2)) ? 32'd1 : 32'd0;
                    3'b011: ex_alu_result = (ex_rs1 < ex_rs2) ? 32'd1 : 32'd0;
                    3'b100: ex_alu_result = ex_rs1 ^ ex_rs2;
                    3'b101: ex_alu_result = (EX_funct7 == 7'b0100000) ?
                                            ex_sra_r : (ex_rs1 >> ex_rs2[4:0]);
                    3'b110: ex_alu_result = ex_rs1 | ex_rs2;
                    3'b111: ex_alu_result = ex_rs1 & ex_rs2;
                    default: ex_alu_result = 32'b0;
                endcase
            end
            OP_SYSTEM: ex_alu_result = csr_rdata;
            default: ex_alu_result = 32'b0;
        endcase
    end

    // FP result plumbing.  fp_unit is combinational for every op except
    // FDIV/FSQRT, whose result is valid while done is asserted.  The unit keeps
    // done/result until the next start, so a completed instruction that is held
    // by an unrelated freeze still presents the same result when it retires.
    wire [31:0] ex_fp_result = fp_result;
    wire [4:0]  fp_flags_eff = fp_fflags;

    reg [31:0] ex_result;
    always @(*) begin
        if (EX_fp)                        // FP->x result, or don't-care for FP-file ops
            ex_result = ex_fp_result;
        else if (EX_amo)                  // LR/SC result; WB supplies AMO old value
            ex_result = EX_is_sc ? (ex_sc_ok ? 32'd0 : 32'd1) : 32'b0;
        else if (ex_is_mul)
            ex_result = ex_mul_result;
        else if (ex_is_div)
            ex_result = (div_state == DIV_DONE) ? div_result_comb : 32'b0;
        else
            ex_result = ex_alu_result;
    end

    //--------------------------------------------------------------------------
    // traps / interrupts
    //--------------------------------------------------------------------------
    // RV32F: any FP instruction (or any access to fcsr/fflags/frm) executed
    // with mstatus.FS==Off is an illegal instruction; so is a CSR write that
    // leaves frm in a reserved mode (5..7), and a DYN rounding mode selected by
    // a reserved frm.  Checking FS in EX also catches instructions already in
    // flight when software clears FS.
    wire ex_fp_fs_off     = EX_valid && (EX_fp || EX_fcsr_access) &&
                            (mstatus_fs == 2'b00);
    wire ex_frm_write_bad = EX_valid && EX_csr_write && !EX_illegal &&
                            (((EX_csr_addr == 12'h003) && (csr_wdata[7:5] > 3'd4)) ||
                             ((EX_csr_addr == 12'h002) && (csr_wdata[2:0] > 3'd4)));
    wire ex_fp_rm_bad     = EX_valid && EX_fp && EX_fp_uses_rm &&
                            (EX_IR[14:12] == 3'b111) && (frm > 3'd4);
    wire ex_illegal_trap  = EX_valid && (EX_illegal || ex_fp_fs_off ||
                                         ex_frm_write_bad || ex_fp_rm_bad);
    wire ex_ecall_trap    = EX_valid && EX_ecall;
    wire ex_ebreak_trap   = EX_valid && EX_ebreak;
    wire ex_mem_misalign  = ex_mem_access && ex_misaligned;
    wire ex_fetch_misalign = ex_target_misaligned;
    // The simple bus has no atomic (RMW) transaction and MMIO reads can have
    // side effects, so atomics are supported on the TCMs only.  Anything on a
    // bus tag faults rather than being emulated as a non-atomic read+write.
    wire ex_amo_unsupported = ex_mem_access && EX_amo &&
                              (EX_addr[31:28] >= 4'h2);

    wire [31:0] pending_irq = mip_comb & mie;
    wire        irq_pending = mstatus[3] && (pending_irq != 32'b0);

    reg [31:0] irq_cause;
    always @(*) begin
        if (pending_irq[11])      irq_cause = 32'h8000_000b; // machine external
        else if (pending_irq[3])  irq_cause = 32'h8000_0003; // machine software
        else if (pending_irq[7])  irq_cause = 32'h8000_0007; // machine timer
        else                      irq_cause = 32'b0;
    end

    wire ex_exception = ex_illegal_trap || ex_ecall_trap || ex_ebreak_trap ||
                        ex_mem_misalign || ex_amo_unsupported ||
                        ex_fetch_misalign;

    reg [31:0] ex_exc_cause;
    reg [31:0] ex_exc_tval;
    always @(*) begin
        ex_exc_cause = 32'b0;
        ex_exc_tval  = 32'b0;
        if (ex_illegal_trap) begin
            ex_exc_cause = 32'd2;
            ex_exc_tval  = EX_IR;
        end else if (ex_ecall_trap) begin
            ex_exc_cause = 32'd11;
        end else if (ex_ebreak_trap) begin
            ex_exc_cause = 32'd3;
            ex_exc_tval  = EX_PC;         // breakpoint address (spec-permitted)
        end else if (ex_mem_misalign) begin
            ex_exc_cause = EX_mem_read ? 32'd4 : 32'd6;
            ex_exc_tval  = EX_addr;
        end else if (ex_amo_unsupported) begin
            ex_exc_cause = EX_is_lr ? 32'd5 : 32'd7;
            ex_exc_tval  = EX_addr;
        end else if (ex_fetch_misalign) begin
            ex_exc_cause = 32'd0;
            ex_exc_tval  = ex_target;
        end
    end

    //--------------------------------------------------------------------------
    // MEM
    //--------------------------------------------------------------------------
    reg [31:0] MEM_PC;
    reg [31:0] MEM_IR;
    reg        MEM_valid;
    reg [4:0]  MEM_rd;
    reg        MEM_rd_write_req;
    reg [31:0] MEM_result;
    reg        MEM_mem_read;
    reg        MEM_mem_write;
    reg [2:0]  MEM_mem_size;
    reg        MEM_mem_unsigned;
    reg [31:0] MEM_addr;
    reg [31:0] MEM_wdata;         // also carries the rs2 operand of an AMO
    reg [3:0]  MEM_wstrb;
    reg [1:0]  MEM_load_src;
    reg        MEM_amo;
    reg        MEM_amo_rmw;
    reg        MEM_is_lr;
    reg        MEM_is_sc;
    reg [4:0]  MEM_amo_op;
    reg        MEM_amo_phase;     // 0 = read cycle, 1 = write cycle (RMW)
    reg        MEM_sc_ok;
    reg        MEM_fp_rd_write;   // write the FP file at WB
    reg [4:0]  MEM_frd;
    reg [31:0] MEM_fp_result;

    //--------------------------------------------------------------------------
    // WB
    //--------------------------------------------------------------------------
    reg [31:0] WB_PC;
    reg [31:0] WB_IR;
    reg        WB_valid;
    reg [4:0]  WB_rd;
    reg        WB_rd_write_req;
    reg [31:0] WB_result;
    reg [2:0]  WB_load_size;
    reg        WB_load_unsigned;
    reg [1:0]  WB_addr_low;
    reg [1:0]  WB_load_src;
    reg        WB_fp_rd_write;
    reg [4:0]  WB_frd;
    reg [31:0] WB_fp_result;

    // bus / div freeze
    wire mem_bus_access = MEM_valid && (MEM_mem_read || MEM_mem_write) &&
                          (MEM_addr[31:28] >= 4'h2);
    wire ex_bus_access = EX_valid && (EX_mem_read || EX_mem_write) &&
                         (EX_addr[31:28] >= 4'h2);
    // Back-to-back bus accesses would keep req high; insert one bubble so the
    // bus sees a fresh req edge for every transaction.
    wire bus_conflict = mem_bus_access && ex_bus_access;
    wire bus_wait = mem_bus_access && !bus_ready_i;
    // FP freeze: the launch cycle and the iterative cycles hold EX; the done
    // cycle does not, so the instruction retires.  A completed unit holds
    // done/result until the next start, hence an unrelated bus/divider freeze
    // can delay retirement without losing the result.
    wire ex_fp_slow_hit = EX_valid && EX_fp && EX_fp_slow;
    wire fp_busy_now    = fp_busy || fp_start;
    wire fp_freeze      = fp_start || (fp_busy && !fp_done);
    wire pipe_freeze = bus_wait || div_busy || fp_freeze;

    //--------------------------------------------------------------------------
    // RV32A: MEM read-modify-write and LR/SC reservation
    //--------------------------------------------------------------------------
    // An AMO occupies the in-order MEM slot for two cycles (read, write) with
    // one bubble after it.  Only TCM addresses reach MEM: EX rejects bus/MMIO
    // atomics, so the single-outstanding bus is never asked for an RMW.
    wire mem_amo_active = MEM_valid && MEM_amo_rmw && !pipe_freeze;
    wire [31:0] mem_amo_old = (MEM_addr[31:28] == 4'h0) ? mem_itcm_rdata_q :
                              (MEM_addr[31:28] == 4'h1) ? mem_dtcm_rdata_q : 32'b0;

    reg [31:0] mem_amo_new;
    always @(*) begin
        case (MEM_amo_op)
            5'b00000: mem_amo_new = mem_amo_old + MEM_wdata;
            5'b00001: mem_amo_new = MEM_wdata;
            5'b00100: mem_amo_new = mem_amo_old ^ MEM_wdata;
            5'b01000: mem_amo_new = mem_amo_old | MEM_wdata;
            5'b01100: mem_amo_new = mem_amo_old & MEM_wdata;
            5'b10000: mem_amo_new = ($signed(mem_amo_old) < $signed(MEM_wdata)) ?
                                    mem_amo_old : MEM_wdata;
            5'b10100: mem_amo_new = ($signed(mem_amo_old) > $signed(MEM_wdata)) ?
                                    mem_amo_old : MEM_wdata;
            5'b11000: mem_amo_new = (mem_amo_old < MEM_wdata) ? mem_amo_old : MEM_wdata;
            5'b11100: mem_amo_new = (mem_amo_old > MEM_wdata) ? mem_amo_old : MEM_wdata;
            default:  mem_amo_new = mem_amo_old;
        endcase
    end

    wire mem_amo_we     = MEM_valid && MEM_amo_rmw && MEM_amo_phase && !pipe_freeze;
    wire mem_lr_commit  = MEM_valid && MEM_is_lr && !pipe_freeze;
    wire mem_sc_commit  = MEM_valid && MEM_is_sc && !pipe_freeze;
    wire mem_normal_we  = MEM_valid && MEM_mem_write && !MEM_amo_rmw &&
                          (!MEM_is_sc || MEM_sc_ok) && !pipe_freeze;
    wire mem_we_any     = mem_normal_we || mem_amo_we;
    wire [31:0] mem_wdata_eff = mem_amo_we ? mem_amo_new : MEM_wdata;
    wire [3:0]  mem_wstrb_eff = mem_amo_we ? 4'hf : MEM_wstrb;
    // Writes below the configured boot boundary are ignored.  This also gates
    // the same-cycle store/load merge, so a rejected store can never become
    // visible speculatively through the MEM forwarding path.
    wire mem_itcm_we    = mem_we_any && (MEM_addr[31:28] == 4'h0) &&
                          ((ITCM_BOOT_BYTES == 0) || (MEM_addr >= ITCM_BOOT_BYTES));
    wire mem_dtcm_we    = mem_we_any && (MEM_addr[31:28] == 4'h1);

    reg        lr_resv_valid;
    reg [31:0] lr_resv_addr;

    // SC checks the reservation against the word, but must also see an older
    // LR committing in MEM (the constrained LR/SC sequence) and an older
    // same-word store/AMO committing on this very edge (which must fail it).
    wire [31:0] ex_word = {EX_addr[31:2], 2'b00};
    wire mem_commit_store = !pipe_freeze && MEM_valid && !MEM_is_lr &&
        ((MEM_mem_write && !MEM_amo_rmw && (!MEM_is_sc || MEM_sc_ok)) ||
         (MEM_amo_rmw && MEM_amo_phase));
    wire resv_eff_valid = mem_sc_commit ? 1'b0 :
                          mem_lr_commit ? 1'b1 : lr_resv_valid;
    wire [31:0] resv_eff_addr = mem_lr_commit ? {MEM_addr[31:2], 2'b00} :
                                lr_resv_addr;
    wire ex_sc_conflict = mem_commit_store && ({MEM_addr[31:2], 2'b00} == ex_word);
    // The boot region is read-only to the application.  A normal store is
    // dropped by mem_itcm_we below, but SC must also report failure instead of
    // claiming a commit whose data never reached the array.
    wire ex_boot_protected = (EX_addr[31:28] == 4'h0) && (ITCM_BOOT_BYTES != 0) &&
                             (EX_addr < ITCM_BOOT_BYTES);
    wire ex_sc_ok = EX_amo && EX_is_sc && !ex_boot_protected && resv_eff_valid &&
                    (resv_eff_addr == ex_word) && !ex_sc_conflict;

    // Interrupts and exceptions wait for an in-flight FDIV/FSQRT so the unit's
    // architectural state (fflags) is committed exactly once.  FP loads/stores
    // are combinational in EX and trap immediately.
    wire ex_interrupt = EX_valid && !ex_exception && irq_pending &&
                        !bus_wait && !div_busy && !fp_busy_now;
    wire trap_taken = !bus_wait && !div_busy && !fp_busy_now &&
                      (ex_exception || ex_interrupt);
    wire trap_is_interrupt = !ex_exception;
    wire [31:0] trap_cause = ex_exception ? ex_exc_cause : irq_cause;
    wire [31:0] trap_tval  = ex_exception ? ex_exc_tval : 32'b0;

    wire [31:0] mtvec_base = {mtvec[31:2], 2'b00};
    wire [31:0] trap_vector = (mtvec[1:0] == 2'b01 && trap_is_interrupt) ?
                              (mtvec_base + {trap_cause[29:0], 2'b00}) : mtvec_base;

    wire mret_taken = EX_valid && EX_mret && !trap_taken && !pipe_freeze &&
                      !mem_amo_active;

    //--------------------------------------------------------------------------
    // Branch prediction resolution
    //--------------------------------------------------------------------------
    wire [BTB_IBITS-1:0]  ex_pc_index = EX_PC[BTB_IBITS:1];
    wire [31:BTB_IBITS+1] ex_pc_tag   = EX_PC[31:BTB_IBITS+1];

    wire ex_ctrl_actual = EX_jump || (EX_branch && ex_branch_taken);
    wire ex_ctrl_class  = EX_jump || EX_branch;

    // Compare against the prediction this instruction was fetched with
    // (EX_pred_*), not a fresh lookup: the entry may have been rewritten.
    wire ex_pred_ok = EX_valid && EX_pred_taken && ex_ctrl_actual &&
                      (ex_target == EX_pred_target);
    // Correct a wrong direction, a wrong target, or a stale entry on a
    // non-control (e.g. patched code).  Not on traps (misaligned targets trap
    // and must not be corrected or trained), not during a freeze.
    wire ex_pred_mispredict = EX_valid && !ex_exception && !trap_taken &&
                              !pipe_freeze && !bus_conflict && !mem_amo_active &&
                              (EX_pred_taken ? !ex_pred_ok : ex_ctrl_actual);

    // Training: one write port.  A taken control stores tag/target/valid; the
    // counter is updated on every resolved conditional branch (saturating, and
    // only when the entry is already valid for a not-taken branch).
    wire ex_ctrl_resolved = EX_valid && ex_ctrl_class && !ex_exception &&
                            !trap_taken && !pipe_freeze && !mem_amo_active;
    wire btb_we = ex_ctrl_resolved &&
                  (ex_ctrl_actual || btb_valid[ex_pc_index]);
    wire [1:0] btb_cnt_inc = (btb_cnt[ex_pc_index] == 2'b11) ? 2'b11 :
                             btb_cnt[ex_pc_index] + 2'd1;
    wire [1:0] btb_cnt_dec = (btb_cnt[ex_pc_index] == 2'b00) ? 2'b00 :
                             btb_cnt[ex_pc_index] - 2'd1;
    wire [1:0] btb_wcnt = EX_jump ? 2'b11 :
                          ex_ctrl_actual ? (btb_valid[ex_pc_index] ?
                                            btb_cnt_inc : 2'b10) :
                          btb_cnt_dec;

    // A stale entry on a non-control redirects to the fall-through address.
    wire redirect = trap_taken || mret_taken || ex_pred_mispredict;
    wire [31:0] redirect_pc = trap_taken ? trap_vector :
                              mret_taken ? mepc :
                              ((EX_pred_taken && !ex_ctrl_actual) ?
                               (EX_PC + ex_insn_len) : ex_target);

    // Commit RAS operations only when the EX instruction really advances.  A
    // mispredicted return still pops (the return itself is on the correct path);
    // a trap cancels its EX instruction.  Trap/mret clear the predictor because
    // the handler may reuse the architectural link registers on another path.
    wire ras_ex_advance = EX_valid && EX_jump && !trap_taken &&
                          !pipe_freeze && !mem_amo_active;
    wire ras_ex_call = ras_ex_advance &&
                       ((EX_opcode == OP_JAL) ||
                        ((EX_opcode == OP_JALR) && (EX_funct3 == 3'b000))) &&
                       ((EX_rd == 5'd1) || (EX_rd == 5'd5));
    wire ras_ex_return = ras_ex_advance && (EX_opcode == OP_JALR) &&
                         (EX_funct3 == 3'b000) && (EX_rd == 5'd0) &&
                         ((EX_rs1 == 5'd1) || (EX_rs1 == 5'd5)) &&
                         (EX_imm == 32'd0) && (ras_count != 0);

    always @(posedge clk) begin
        if (!rstn) begin
            ras_count <= 3'd0;
        end else if (trap_taken || mret_taken) begin
            ras_count <= 3'd0;
        end else if (ras_ex_call) begin
            ras_stack[3] <= ras_stack[2];
            ras_stack[2] <= ras_stack[1];
            ras_stack[1] <= ras_stack[0];
            ras_stack[0] <= EX_PC + ex_insn_len;
            ras_count <= (ras_count == 3'd4) ? 3'd4 : ras_count + 3'd1;
        end else if (ras_ex_return) begin
            ras_stack[0] <= ras_stack[1];
            ras_stack[1] <= ras_stack[2];
            ras_stack[2] <= ras_stack[3];
            ras_count <= ras_count - 3'd1;
        end
    end

    // register file
    reg [31:0] regfiles [0:31];
    reg [31:0] wb_rd_data;

    function [31:0] load_expand;
        input [31:0] raw;
        input [2:0]  size;
        input        uns;
        input [1:0]  addr_low;
        begin
            case (size)
                3'd1: begin
                    case (addr_low)
                        2'b00: load_expand = uns ? {24'b0, raw[7:0]}  : {{24{raw[7]}},  raw[7:0]};
                        2'b01: load_expand = uns ? {24'b0, raw[15:8]} : {{24{raw[15]}}, raw[15:8]};
                        2'b10: load_expand = uns ? {24'b0, raw[23:16]} : {{24{raw[23]}}, raw[23:16]};
                        default: load_expand = uns ? {24'b0, raw[31:24]} : {{24{raw[31]}}, raw[31:24]};
                    endcase
                end
                3'd2: begin
                    if (addr_low[1])
                        load_expand = uns ? {16'b0, raw[31:16]} : {{16{raw[31]}}, raw[31:16]};
                    else
                        load_expand = uns ? {16'b0, raw[15:0]}  : {{16{raw[15]}}, raw[15:0]};
                end
                default: load_expand = raw;
            endcase
        end
    endfunction

    wire [31:0] mem_load_raw = (MEM_load_src == 2'd1) ? mem_itcm_rdata_q :
                               (MEM_load_src == 2'd2) ? mem_dtcm_rdata_q : 32'b0;
    wire [31:0] mem_load_data = load_expand(mem_load_raw, MEM_mem_size,
                                             MEM_mem_unsigned, MEM_addr[1:0]);
    wire [31:0] wb_raw_data = (WB_load_src == 2'd3) ? bus_rdata_q : WB_result;

    always @(*) begin
        wb_rd_data = (WB_load_src != 2'd0) ?
                     load_expand(wb_raw_data, WB_load_size,
                                 WB_load_unsigned, WB_addr_low) : WB_result;
    end

    // Read operands with EX/MEM->ID forwarding, then the WB bypass, and x0
    // hardwired ahead of all of them.  EX is younger than MEM, so EX wins.
    // A normal TCM load read is captured at EX->MEM and can forward from MEM;
    // bus loads and AMO/LR/SC still wait for WB.  A divider in EX has
    // ex_result == 0 until DIV_DONE and is explicitly excluded.
    wire ex_fwd_ok  = EX_valid && EX_rd_write_req && !EX_mem_read && !EX_amo &&
                      !(ex_is_div && (div_state != DIV_DONE));
    wire mem_fwd_ok = MEM_valid && MEM_rd_write_req && !MEM_mem_read && !MEM_amo;
    wire mem_load_fwd_ok = MEM_valid && MEM_rd_write_req && MEM_mem_read &&
                           !MEM_amo && !pipe_freeze && !mem_amo_active &&
                           ((MEM_load_src == 2'd1) || (MEM_load_src == 2'd2));
    wire mem_fwd_any = mem_fwd_ok || mem_load_fwd_ok;
    wire [31:0] mem_fwd_data = mem_load_fwd_ok ? mem_load_data : MEM_result;

    wire [31:0] id_rs1_data = (id_rs1 == 5'd0) ? 32'b0 :
                              (ex_fwd_ok  && (EX_rd  != 5'd0) && (EX_rd  == id_rs1)) ? ex_result :
                              (mem_fwd_any && (MEM_rd != 5'd0) && (MEM_rd == id_rs1)) ? mem_fwd_data :
                              (WB_valid && WB_rd_write_req && (WB_rd == id_rs1)) ?
                              wb_rd_data : regfiles[id_rs1];
    wire [31:0] id_rs2_data = (id_rs2 == 5'd0) ? 32'b0 :
                              (ex_fwd_ok  && (EX_rd  != 5'd0) && (EX_rd  == id_rs2)) ? ex_result :
                              (mem_fwd_any && (MEM_rd != 5'd0) && (MEM_rd == id_rs2)) ? mem_fwd_data :
                              (WB_valid && WB_rd_write_req && (WB_rd == id_rs2)) ?
                              wb_rd_data : regfiles[id_rs2];

    // Hazard: a load in EX always stalls once.  In MEM, only bus loads and
    // AMO/LR/SC still stall; ordinary TCM loads use the MEM bypass above.
    wire id_dep_ex = ID_valid && EX_valid && EX_rd_write_req && (EX_rd != 5'd0) &&
                     (EX_mem_read || EX_amo ||
                      (ex_is_div && (div_state != DIV_DONE))) &&
                     ((dec_uses_rs1 && (id_rs1 == EX_rd)) ||
                      (dec_uses_rs2 && (id_rs2 == EX_rd)));
    wire id_dep_mem = ID_valid && MEM_valid && MEM_rd_write_req &&
                      (MEM_amo || (MEM_mem_read && !mem_load_fwd_ok)) &&
                      (MEM_rd != 5'd0) &&
                      ((dec_uses_rs1 && (id_rs1 == MEM_rd)) ||
                       (dec_uses_rs2 && (id_rs2 == MEM_rd)));
    wire raw_stall = id_dep_ex || id_dep_mem || id_fp_stall;

    //--------------------------------------------------------------------------
    // RV32F register file, scoreboard and hazards
    //--------------------------------------------------------------------------
    // FP-file results are not forwarded from EX/MEM: an FP writer in EX or MEM
    // holds ID (the same two-cycle load-use shape), and the reader then takes
    // the value from the WB bypass in the cycle it enters EX.  f0 is a normal
    // FP register, so unlike x0 it is neither hardwired nor unwritable.
    // fcsr/mstatus reads need no extra scoreboard: CSR writes commit in EX, so
    // an FP instruction can never be in EX in the same cycle as the write that
    // changes FS/frm; it is always one instruction younger.
    reg [31:0] fregs [0:31];

    wire [31:0] wb_fp_data = WB_load_src ? wb_rd_data : WB_fp_result;

    wire id_fp_ex_dep = ID_valid && EX_valid && EX_fp_rd_write &&
                        ((dec_fp_uses_frs1 && (id_rs1 == EX_frd)) ||
                         (dec_fp_uses_frs2 && (id_rs2 == EX_frd)) ||
                         (dec_fp_uses_frs3 && (id_ir[31:27] == EX_frd)));
    wire id_fp_mem_dep = ID_valid && MEM_valid && MEM_fp_rd_write &&
                         ((dec_fp_uses_frs1 && (id_rs1 == MEM_frd)) ||
                          (dec_fp_uses_frs2 && (id_rs2 == MEM_frd)) ||
                          (dec_fp_uses_frs3 && (id_ir[31:27] == MEM_frd)));
    wire id_fp_stall = id_fp_ex_dep || id_fp_mem_dep;

    // One WB bypass per source; a single shared hit flag would feed the WB
    // value to every source (e.g. f1+f2 would read f2 twice).
    wire id_fp_wb_hit1 = WB_valid && WB_fp_rd_write && dec_fp_uses_frs1 &&
                         (id_rs1 == WB_frd);
    wire id_fp_wb_hit2 = WB_valid && WB_fp_rd_write && dec_fp_uses_frs2 &&
                         (id_rs2 == WB_frd);
    wire id_fp_wb_hit3 = WB_valid && WB_fp_rd_write && dec_fp_uses_frs3 &&
                         (id_ir[31:27] == WB_frd);

    wire [31:0] id_frs1_data = id_fp_wb_hit1 ? wb_fp_data : fregs[id_rs1];
    wire [31:0] id_frs2_data = id_fp_wb_hit2 ? wb_fp_data : fregs[id_rs2];
    wire [31:0] id_frs3_data = id_fp_wb_hit3 ? wb_fp_data :
                               fregs[id_ir[31:27]];

    //--------------------------------------------------------------------------
    // TCM / bus access
    //--------------------------------------------------------------------------
    assign bus_req_o   = mem_bus_access;
    assign bus_we_o    = MEM_mem_write;
    assign bus_addr_o  = {MEM_addr[31:2], 2'b00};
    assign bus_wdata_o = MEM_wdata;
    assign bus_wstrb_o = MEM_wstrb;

    integer i_itcm;
    integer i_dtcm;
    integer i_tcm_rd;
    integer i_btb;

    always @(posedge clk) begin
        if (mem_itcm_we) begin
            for (i_itcm = 0; i_itcm < 4; i_itcm = i_itcm + 1) begin
                if (mem_wstrb_eff[i_itcm])
                    ITCM[MEM_addr[ITCM_AW+1:2]][i_itcm*8 +: 8] <= mem_wdata_eff[i_itcm*8 +: 8];
            end
        end
    end

    always @(posedge clk) begin
        if (mem_dtcm_we) begin
            for (i_dtcm = 0; i_dtcm < 4; i_dtcm = i_dtcm + 1) begin
                if (mem_wstrb_eff[i_dtcm])
                    DTCM[MEM_addr[DTCM_AW+1:2]][i_dtcm*8 +: 8] <= mem_wdata_eff[i_dtcm*8 +: 8];
            end
        end
    end

    // Two word-aligned IF reads: the word containing the next presented
    // instruction and the following word, so lane-1 32-bit instructions need
    // no assembly bubble.  if_hold keeps IF_PC equal to the needed word.
    always @(posedge clk) begin
        if (!rstn) begin
            if_rdata_q    <= 32'b0;
            if_rdata_hi_q <= 32'b0;
        end else begin
            if_rdata_q    <= ITCM[IF_PC[ITCM_AW+1:2]];
            if_rdata_hi_q <= ITCM[IF_PC[ITCM_AW+1:2] + 1'b1];
        end
        // Read the EX address so the word is usable during the MEM cycle.
        // If an older store commits at this same edge, merge its byte lanes:
        // an inferred array would otherwise return the pre-store word.
        if (!pipe_freeze && !mem_amo_active) begin
            for (i_tcm_rd = 0; i_tcm_rd < 4; i_tcm_rd = i_tcm_rd + 1) begin
                if (mem_itcm_we && (MEM_addr[31:2] == EX_addr[31:2]) &&
                    mem_wstrb_eff[i_tcm_rd])
                    mem_itcm_rdata_q[i_tcm_rd*8 +: 8] <= mem_wdata_eff[i_tcm_rd*8 +: 8];
                else
                    mem_itcm_rdata_q[i_tcm_rd*8 +: 8] <=
                        ITCM[EX_addr[ITCM_AW+1:2]][i_tcm_rd*8 +: 8];
                if (mem_dtcm_we && (MEM_addr[31:2] == EX_addr[31:2]) &&
                    mem_wstrb_eff[i_tcm_rd])
                    mem_dtcm_rdata_q[i_tcm_rd*8 +: 8] <= mem_wdata_eff[i_tcm_rd*8 +: 8];
                else
                    mem_dtcm_rdata_q[i_tcm_rd*8 +: 8] <=
                        DTCM[EX_addr[DTCM_AW+1:2]][i_tcm_rd*8 +: 8];
            end
        end
    end

    always @(posedge clk) begin
        if (!pipe_freeze && mem_bus_access && MEM_mem_read && bus_ready_i)
            bus_rdata_q <= bus_rdata_i;
    end

    always @(posedge clk) begin
        if (!rstn) begin
            btb_valid <= {BTB_NUM{1'b0}};
            for (i_btb = 0; i_btb < BTB_NUM; i_btb = i_btb + 1)
                btb_cnt[i_btb] <= 2'b00;
        end else if (btb_we) begin
            btb_cnt[ex_pc_index] <= btb_wcnt;
            if (ex_ctrl_actual) begin
                btb_valid[ex_pc_index]  <= 1'b1;
                btb_tag[ex_pc_index]    <= ex_pc_tag;
                btb_target[ex_pc_index] <= ex_target;
            end
        end
    end

    // LR/SC reservation: a 4-byte granule.  A successful LR sets it, any SC
    // clears it, any committed store/AMO write to the same word clears it, and
    // so does taking a trap (a conservative, spec-allowed spurious failure).
    always @(posedge clk) begin
        if (!rstn) begin
            lr_resv_valid <= 1'b0;
            lr_resv_addr  <= 32'b0;
        end else if (trap_taken) begin
            lr_resv_valid <= 1'b0;
        end else if (mem_lr_commit) begin
            lr_resv_valid <= 1'b1;
            lr_resv_addr  <= {MEM_addr[31:2], 2'b00};
        end else if (mem_sc_commit || (mem_commit_store &&
                                       ({MEM_addr[31:2], 2'b00} == lr_resv_addr))) begin
            lr_resv_valid <= 1'b0;
        end
    end

    //--------------------------------------------------------------------------
    // bus monitor trace (+BUS_TRACE=<file>): one line per bus transaction for
    // MMIO-heavy programs (benchmarks), where the core-level trace is too big.
    //--------------------------------------------------------------------------
    // synthesis translate_off
    integer bus_trace_fd;
    reg [1023:0] bus_trace_file;
    initial begin
        bus_trace_fd = 0;
        if ($value$plusargs("BUS_TRACE=%s", bus_trace_file))
            bus_trace_fd = $fopen(bus_trace_file, "w");
    end

    always @(posedge clk) begin
        if (bus_trace_fd != 0 && mem_bus_access) begin
            if (MEM_mem_write)
                $fdisplay(bus_trace_fd, "W %08x %08x %x",
                          {MEM_addr[31:2], 2'b00}, MEM_wdata, MEM_wstrb);
            else if (bus_ready_i)
                $fdisplay(bus_trace_fd, "R %08x %08x",
                          {MEM_addr[31:2], 2'b00}, bus_rdata_i);
        end
    end
    // synthesis translate_on

    //--------------------------------------------------------------------------
    // sequential pipeline
    //--------------------------------------------------------------------------
    // IF_PC is a wire (the next fetch address); this block advances the
    // presented instruction.  A redirect wins over every hold and clears any
    // straddle state; the following id_flush_q cycle holds the target word so
    // ID can capture it.  Prediction metadata is registered only when it is
    // the lookup of the instruction being presented: never during a hold or a
    // straddle-detect cycle.
    always @(posedge clk) begin
        if (!rstn) begin
            IF_PC_q <= RESET_PC;
            IF_pred_taken_q  <= 1'b0;
            IF_pred_target_q <= 32'b0;
        end else if (redirect) begin
            IF_PC_q <= {redirect_pc[31:1], 1'b0};  // presented PC
            IF_pred_taken_q  <= btb_pred_taken;
            IF_pred_target_q <= btb_pred_target;
        end else if (if_hold) begin
            // Hold IF_PC_q and the prediction.  The id_flush_q cycle after a
            // redirect is special: the target word is already presented, but
            // the BTB entry was trained on the redirect edge, so take a fresh
            // lookup now.
            if (id_flush_q) begin
                IF_pred_taken_q  <= btb_pred_taken;
                IF_pred_target_q <= btb_pred_target;
            end
        end else begin
            IF_PC_q <= IF_PC;
            IF_pred_taken_q  <= btb_pred_taken;
            IF_pred_target_q <= btb_pred_target;
        end
    end

    always @(posedge clk) begin
        if (!rstn) begin
            ID_PC        <= 32'b0;
            ID_IR        <= NOP;
            ID_valid     <= 1'b0;
            id_flush_q   <= 1'b1;
            IF_ID_IR_hold <= NOP;
            IF_ID_PC_hold <= 32'b0;
            IF_ID_compressed_hold <= 1'b0;
            IF_ID_hold   <= 1'b0;
            IF_ID_pred_taken_hold  <= 1'b0;
            IF_ID_pred_target_hold <= 32'b0;
            ID_pred_taken  <= 1'b0;
            ID_pred_target <= 32'b0;
            ID_is_compressed <= 1'b0;
        end else if (redirect) begin
            ID_PC         <= 32'b0;
            ID_IR         <= NOP;
            ID_valid      <= 1'b0;
            id_flush_q    <= 1'b1;
            IF_ID_hold    <= 1'b0;
            ID_pred_taken  <= 1'b0;
            ID_pred_target <= 32'b0;
        end else if (id_flush_q) begin
            ID_PC         <= 32'b0;
            ID_IR         <= NOP;
            ID_valid      <= 1'b0;
            id_flush_q    <= 1'b0;
            IF_ID_hold    <= 1'b0;
            ID_pred_taken  <= 1'b0;
            ID_pred_target <= 32'b0;
        end else if (pipe_freeze || raw_stall || bus_conflict || mem_amo_active) begin
            if (!IF_ID_hold) begin
                IF_ID_IR_hold <= id_capture_ir;
                IF_ID_PC_hold <= id_capture_pc;
                IF_ID_compressed_hold <= id_capture_compressed;
                IF_ID_pred_taken_hold  <= if_pred_taken_eff;
                IF_ID_pred_target_hold <= if_pred_target_eff;
                IF_ID_hold    <= 1'b1;
            end
        end else begin
            ID_PC     <= id_capture_pc;
            ID_IR     <= id_capture_ir;
            ID_valid  <= 1'b1;
            ID_is_compressed <= id_capture_compressed;
            ID_pred_taken  <= id_capture_pred_taken;
            ID_pred_target <= id_capture_pred_target;
            IF_ID_hold <= 1'b0;
        end
    end

    always @(posedge clk) begin
        if (!rstn) begin
            EX_PC           <= 32'b0;
            EX_IR           <= NOP;
            EX_valid        <= 1'b0;
            EX_pred_taken   <= 1'b0;
            EX_pred_target  <= 32'b0;
            EX_opcode       <= 7'b0;
            EX_funct3       <= 3'b0;
            EX_funct7       <= 7'b0;
            EX_rs1          <= 5'b0;
            EX_rs2          <= 5'b0;
            EX_rd           <= 5'b0;
            EX_rs1_data     <= 32'b0;
            EX_rs2_data     <= 32'b0;
            EX_imm          <= 32'b0;
            EX_shamt        <= 5'b0;
            EX_is_compressed <= 1'b0;
            EX_amo          <= 1'b0;
            EX_amo_rmw      <= 1'b0;
            EX_is_lr        <= 1'b0;
            EX_is_sc        <= 1'b0;
            EX_amo_op       <= 5'b0;
            EX_rd_write_req <= 1'b0;
            EX_mem_read     <= 1'b0;
            EX_mem_write    <= 1'b0;
            EX_mem_size     <= 3'b0;
            EX_mem_unsigned <= 1'b0;
            EX_branch       <= 1'b0;
            EX_jump         <= 1'b0;
            EX_jalr         <= 1'b0;
            EX_csr_read     <= 1'b0;
            EX_csr_write    <= 1'b0;
            EX_csr_addr     <= 12'b0;
            EX_csr_uimm     <= 5'b0;
            EX_illegal      <= 1'b0;
            EX_ecall        <= 1'b0;
            EX_ebreak       <= 1'b0;
            EX_mret         <= 1'b0;
            EX_fence        <= 1'b0;
            EX_fp           <= 1'b0;
            EX_fp_rd_write  <= 1'b0;
            EX_fp_slow      <= 1'b0;
            EX_fp_uses_rm   <= 1'b0;
            EX_fp_fflags_en <= 1'b0;
            EX_fcsr_access  <= 1'b0;
            EX_frd          <= 5'b0;
            EX_frs1_q       <= 32'b0;
            EX_frs2_q       <= 32'b0;
            EX_frs3_q       <= 32'b0;
            EX_fp_launched_q <= 1'b0;
        end else if (pipe_freeze) begin
            // hold; the launch flag rises on the fp_start cycle so the unit
            // starts exactly once per slow instruction
            EX_fp_launched_q <= EX_fp_launched_q | fp_start;
        end else if (trap_taken) begin
            // the faulting/interrupted instruction must not enter MEM; this
            // also wins over an AMO in MEM, which completes regardless
            EX_valid <= 1'b0;
            EX_fp_launched_q <= 1'b0;
        end else if (bus_conflict) begin
            // hold the younger bus access in EX while the older one drains
            EX_fp_launched_q <= EX_fp_launched_q | fp_start;
        end else if (mem_amo_active) begin
            // an AMO in MEM holds the younger stages for its two cycles
            EX_fp_launched_q <= EX_fp_launched_q | fp_start;
        end else begin
            EX_fp_launched_q <= 1'b0;
            EX_PC           <= ID_PC;
            EX_IR           <= ID_IR;
            EX_valid        <= ID_valid && !redirect && !raw_stall;
            EX_pred_taken   <= ID_pred_taken;
            EX_pred_target  <= ID_pred_target;
            EX_opcode       <= id_opcode;
            EX_funct3       <= id_funct3;
            EX_funct7       <= id_funct7;
            EX_rs1          <= id_rs1;
            EX_rs2          <= id_rs2;
            EX_rd           <= id_rd;
            EX_rs1_data     <= id_rs1_data;
            EX_rs2_data     <= id_rs2_data;
            EX_imm          <= dec_imm;
            EX_shamt        <= dec_shamt;
            EX_is_compressed <= ID_is_compressed;
            EX_amo          <= dec_amo;
            EX_amo_rmw      <= dec_amo_rmw;
            EX_is_lr        <= dec_is_lr;
            EX_is_sc        <= dec_is_sc;
            EX_amo_op       <= id_amo_op;
            EX_rd_write_req <= dec_rd_write_req;
            EX_mem_read     <= dec_mem_read;
            EX_mem_write    <= dec_mem_write;
            EX_mem_size     <= dec_mem_size;
            EX_mem_unsigned <= dec_mem_unsigned;
            EX_branch       <= dec_branch;
            EX_jump         <= dec_jump;
            EX_jalr         <= dec_jalr;
            EX_csr_read     <= dec_csr_read;
            EX_csr_write    <= dec_csr_write;
            EX_csr_addr     <= id_ir[31:20];
            EX_csr_uimm     <= dec_csr_uimm;
            EX_illegal      <= dec_illegal;
            EX_ecall        <= dec_ecall;
            EX_ebreak       <= dec_ebreak;
            EX_mret         <= dec_mret;
            EX_fence        <= dec_fence;
            EX_fp           <= dec_fp;
            EX_fp_rd_write  <= dec_fp_rd_write;
            EX_fp_slow      <= dec_fp_slow;
            EX_fp_uses_rm   <= dec_fp_uses_rm;
            EX_fp_fflags_en <= dec_fp_fflags_en;
            EX_fcsr_access  <= dec_fcsr_access;
            EX_frd          <= dec_fp_frd;
            // FP-file sources vs. the integer source of FCVT.S.W/WU and
            // FMV.W.X: those forms read x[rs1], not f[rs1].
            EX_frs1_q       <= dec_fp_uses_frs1 ? id_frs1_data : id_rs1_data;
            EX_frs2_q       <= id_frs2_data;
            EX_frs3_q       <= id_frs3_data;
        end
    end

    always @(posedge clk) begin
        if (!rstn) begin
            MEM_PC           <= 32'b0;
            MEM_IR           <= NOP;
            MEM_valid        <= 1'b0;
            MEM_rd           <= 5'b0;
            MEM_rd_write_req <= 1'b0;
            MEM_result       <= 32'b0;
            MEM_mem_read     <= 1'b0;
            MEM_mem_write    <= 1'b0;
            MEM_mem_size     <= 3'b0;
            MEM_mem_unsigned <= 1'b0;
            MEM_addr         <= 32'b0;
            MEM_wdata        <= 32'b0;
            MEM_wstrb        <= 4'b0;
            MEM_load_src     <= 2'b0;
            MEM_amo          <= 1'b0;
            MEM_amo_rmw      <= 1'b0;
            MEM_is_lr        <= 1'b0;
            MEM_is_sc        <= 1'b0;
            MEM_amo_op       <= 5'b0;
            MEM_amo_phase    <= 1'b0;
            MEM_sc_ok        <= 1'b0;
            MEM_fp_rd_write  <= 1'b0;
            MEM_frd          <= 5'b0;
            MEM_fp_result    <= 32'b0;
        end else if (pipe_freeze) begin
            // hold; the AMO phase bit also holds, so a divider/bus freeze can
            // delay but never repeat the single write
        end else if (MEM_valid && MEM_amo_rmw && !MEM_amo_phase) begin
            // AMO read cycle finished: now drive the write cycle
            MEM_amo_phase <= 1'b1;
        end else if (MEM_valid && MEM_amo_rmw && MEM_amo_phase) begin
            // write committed at this edge; insert one bubble and resume
            MEM_valid        <= 1'b0;
            MEM_rd_write_req <= 1'b0;
            MEM_mem_read     <= 1'b0;
            MEM_mem_write    <= 1'b0;
            MEM_load_src     <= 2'b0;
            MEM_amo          <= 1'b0;
            MEM_amo_rmw      <= 1'b0;
            MEM_is_lr        <= 1'b0;
            MEM_is_sc        <= 1'b0;
            MEM_amo_phase    <= 1'b0;
        end else if (bus_conflict) begin
            // older bus access (current MEM) drains to WB; insert a bubble
            MEM_valid        <= 1'b0;
            MEM_rd_write_req <= 1'b0;
            MEM_mem_read     <= 1'b0;
            MEM_mem_write    <= 1'b0;
            MEM_load_src     <= 2'b0;
        end else begin
            MEM_PC           <= EX_PC;
            MEM_IR           <= EX_IR;
            MEM_valid        <= EX_valid && !trap_taken;
            MEM_rd           <= EX_rd;
            MEM_rd_write_req <= EX_rd_write_req;
            MEM_result       <= ex_result;
            MEM_mem_read     <= EX_mem_read;
            MEM_mem_write    <= EX_mem_write;
            MEM_mem_size     <= EX_mem_size;
            MEM_mem_unsigned <= EX_mem_unsigned;
            MEM_addr         <= EX_addr;
            MEM_wdata        <= EX_wdata;
            MEM_wstrb        <= EX_wstrb;
            MEM_load_src     <= EX_load_src;
            MEM_amo          <= EX_amo;
            MEM_amo_rmw      <= EX_amo_rmw;
            MEM_is_lr        <= EX_is_lr;
            MEM_is_sc        <= EX_is_sc;
            MEM_amo_op       <= EX_amo_op;
            MEM_amo_phase    <= 1'b0;
            MEM_sc_ok        <= ex_sc_ok;
            MEM_fp_rd_write  <= EX_fp_rd_write;
            MEM_frd          <= EX_frd;
            MEM_fp_result    <= ex_fp_result;
        end
    end

    always @(posedge clk) begin
        if (!rstn) begin
            WB_PC           <= 32'b0;
            WB_IR           <= NOP;
            WB_valid        <= 1'b0;
            WB_rd           <= 5'b0;
            WB_rd_write_req <= 1'b0;
            WB_result       <= 32'b0;
            WB_load_size    <= 3'b0;
            WB_load_unsigned<= 1'b0;
            WB_addr_low     <= 2'b0;
            WB_load_src     <= 2'b0;
            WB_fp_rd_write  <= 1'b0;
            WB_frd          <= 5'b0;
            WB_fp_result    <= 32'b0;
        end else if (pipe_freeze) begin
            // hold
        end else if (MEM_valid && MEM_amo_rmw && !MEM_amo_phase) begin
            // AMO read cycle: WB waits so the old value is captured once
        end else if (MEM_valid && MEM_amo_rmw && MEM_amo_phase) begin
            // AMO write cycle: retire with the old memory word
            WB_PC            <= MEM_PC;
            WB_IR            <= MEM_IR;
            WB_valid         <= 1'b1;
            WB_rd            <= MEM_rd;
            WB_rd_write_req  <= MEM_rd_write_req;
            WB_result        <= mem_amo_old;
            WB_load_size     <= 3'd4;
            WB_load_unsigned <= 1'b0;
            WB_addr_low      <= MEM_addr[1:0];
            WB_load_src      <= 2'd0;
            WB_fp_rd_write   <= 1'b0;
            WB_frd           <= 5'b0;
            WB_fp_result     <= 32'b0;
        end else begin
            WB_PC            <= MEM_PC;
            WB_IR            <= MEM_IR;
            WB_valid         <= MEM_valid;
            WB_rd            <= MEM_rd;
            WB_rd_write_req  <= MEM_rd_write_req;
            WB_result        <= (MEM_mem_read &&
                                 ((MEM_load_src == 2'd1) || (MEM_load_src == 2'd2))) ?
                                mem_load_raw : MEM_result;
            WB_load_size     <= MEM_mem_size;
            WB_load_unsigned <= MEM_mem_unsigned;
            WB_addr_low      <= MEM_addr[1:0];
            WB_load_src      <= MEM_load_src;
            WB_fp_rd_write   <= MEM_fp_rd_write;
            WB_frd           <= MEM_frd;
            WB_fp_result     <= MEM_fp_result;
        end
    end

    // write-back
    always @(posedge clk) begin
        if (WB_valid && WB_rd_write_req && (WB_rd != 5'd0))
            regfiles[WB_rd] <= wb_rd_data;
        // f0 is a normal FP register: no rd != 0 gate here.
        if (WB_valid && WB_fp_rd_write)
            fregs[WB_frd] <= wb_fp_data;
    end

    //--------------------------------------------------------------------------
    // divider FSM
    //--------------------------------------------------------------------------
    always @(posedge clk) begin
        if (!rstn) begin
            div_state    <= DIV_IDLE;
            div_cnt      <= 5'b0;
            div_a_orig   <= 32'b0;
            div_b_orig   <= 32'b0;
            div_dividend <= 32'b0;
            div_divisor  <= 32'b0;
            div_quot     <= 32'b0;
            div_rem      <= 33'b0;
            div_sign_q   <= 1'b0;
            div_sign_r   <= 1'b0;
            div_by_zero  <= 1'b0;
            div_overflow <= 1'b0;
        end else begin
            case (div_state)
                DIV_IDLE: begin
                    if (div_start) begin
                        div_a_orig   <= ex_rs1;
                        div_b_orig   <= ex_rs2;
                        div_by_zero  <= (ex_rs2 == 32'b0);
                        div_overflow <= ex_div_signed &&
                                        (ex_rs1 == 32'h8000_0000) &&
                                        (ex_rs2 == 32'hffff_ffff);
                        div_sign_q   <= ex_div_signed && (ex_rs1[31] ^ ex_rs2[31]);
                        div_sign_r   <= ex_div_signed && ex_rs1[31];
                        div_dividend <= ex_rs1_abs;
                        div_divisor  <= ex_rs2_abs;
                        div_quot     <= 32'b0;
                        div_rem      <= 33'b0;
                        div_cnt      <= 5'b0;
                        div_state    <= DIV_BUSY;
                    end
                end
                DIV_BUSY: begin
                    div_rem      <= div_rem_next;
                    div_quot     <= div_quot_next;
                    div_dividend <= {div_dividend[30:0], 1'b0};
                    if (div_cnt == 5'd31) begin
                        div_state <= DIV_DONE;
                    end else begin
                        div_cnt <= div_cnt + 5'd1;
                    end
                end
                DIV_DONE: begin
                    if (!pipe_freeze || trap_taken)
                        div_state <= DIV_IDLE;
                end
                default: div_state <= DIV_IDLE;
            endcase
        end
    end

    //--------------------------------------------------------------------------
    // CSR / trap state
    //--------------------------------------------------------------------------
    // A frozen EX (slow bus, iterative divide) holds the CSR instruction for
    // several cycles; without !pipe_freeze it would commit once per cycle.
    wire csr_we = EX_valid && EX_csr_write && !trap_taken && !EX_illegal &&
                  !pipe_freeze && !mem_amo_active;

    always @(posedge clk) begin
        if (!rstn) begin
            mstatus     <= 32'b0;
            mie         <= 32'b0;
            mtvec       <= 32'b0;
            mscratch    <= 32'b0;
            mepc        <= 32'b0;
            mcause      <= 32'b0;
            mtval       <= 32'b0;
            cycle_cnt   <= 64'b0;
            instret_cnt <= 64'b0;
            fcsr        <= 8'b0;
        end else begin
            cycle_cnt <= cycle_cnt + 64'd1;

            // Count an AMO once (its phase-0 read cycle is not retirement).
            if (!pipe_freeze && MEM_valid &&
                !(MEM_amo_rmw && !MEM_amo_phase))
                instret_cnt <= instret_cnt + 64'd1;

            if (trap_taken) begin
                mepc        <= EX_PC & 32'hffff_fffe;
                mcause      <= trap_cause;
                mtval       <= trap_tval;
                mstatus[7]  <= mstatus[3];
                mstatus[3]  <= 1'b0;
                mstatus[12:11] <= 2'b11;      // FS is architecturally unchanged
            end else if (mret_taken) begin
                mstatus[3]  <= mstatus[7];
                mstatus[7]  <= 1'b1;
                mstatus[12:11] <= 2'b11;
            end else if (csr_we) begin
                case (EX_csr_addr)
                    12'h001: fcsr[4:0] <= csr_wdata[4:0];
                    12'h002: fcsr[7:5] <= csr_wdata[2:0];
                    12'h003: fcsr      <= csr_wdata[7:0];
                    12'h300: mstatus  <= csr_wdata;
                    12'h304: mie      <= csr_wdata;
                    12'h305: mtvec    <= csr_wdata;
                    12'h340: mscratch <= csr_wdata;
                    12'h341: mepc     <= {csr_wdata[31:1], 1'b0};
                    12'h342: mcause   <= csr_wdata;
                    12'h343: mtval    <= csr_wdata;
                    default: ;
                endcase
            end
            // Writing fcsr/fflags/frm modifies FP state, so mark FS Dirty.
            if (csr_we && ((EX_csr_addr == 12'h001) || (EX_csr_addr == 12'h002) ||
                           (EX_csr_addr == 12'h003)))
                mstatus[14:13] <= 2'b11;

            // FP execution side effects, committed on the retiring cycle
            // (exactly once; pipe_freeze covers every multi-cycle hold).  Any
            // FP instruction that executes marks FS Dirty; the arithmetic,
            // conversion and compare forms accrue fflags.
            if (!pipe_freeze && EX_valid && EX_fp && !trap_taken && !EX_illegal) begin
                mstatus[14:13] <= 2'b11;
                if (EX_fp_fflags_en)
                    fcsr[4:0] <= fcsr[4:0] | fp_flags_eff;
            end
        end
    end

endmodule
