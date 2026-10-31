// Sprouts500 - minimal 5-stage RV32IM core
// - IF/ID/EX/MEM/WB, stall-based hazards (no forwarding), ITCM/DTCM inside
// - Simple data bus for MMIO (mem_ready handshake, byte strobes)
// - Machine-mode traps/interrupts, M extension (1-cycle mul, iterative div)
//
// Memory map (tag = addr[31:28]):
//   0x0xxxxxxx : ITCM (instruction + data), ITCM_WORDS words
//   0x1xxxxxxx : DTCM (data),                 DTCM_WORDS words
//   0x2..0xFxxx: external simple bus

module core #(
    parameter [31:0] RESET_PC   = 32'h0000_0000,
    parameter        ITCM_WORDS = 8192,
    parameter        DTCM_WORDS = 4096
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

    reg [31:0] if_rdata_q;
    reg [31:0] mem_itcm_rdata_q;
    reg [31:0] mem_dtcm_rdata_q;
    reg [31:0] bus_rdata_q;

    //--------------------------------------------------------------------------
    // IF
    //--------------------------------------------------------------------------
    reg [31:0] IF_PC;
    reg [31:0] IF_PC_q;          // address of the word currently on if_rdata_q

    //--------------------------------------------------------------------------
    // ID
    //--------------------------------------------------------------------------
    reg [31:0] ID_PC;
    reg [31:0] ID_IR;
    reg        ID_valid;
    reg        id_flush_q;

    // During a stall the instruction already presented by IF (IF_PC_q /
    // if_rdata_q) must be preserved; otherwise IF would overwrite it with the
    // next fetch while ID is held.
    reg [31:0] IF_ID_IR_hold;
    reg [31:0] IF_ID_PC_hold;
    reg        IF_ID_hold;
    wire [31:0] id_capture_ir = IF_ID_hold ? IF_ID_IR_hold : if_rdata_q;
    wire [31:0] id_capture_pc = IF_ID_hold ? IF_ID_PC_hold : IF_PC_q;

    wire [31:0] id_ir = ID_valid ? ID_IR : NOP;

    wire [6:0]  id_opcode = id_ir[6:0];
    wire [2:0]  id_funct3 = id_ir[14:12];
    wire [6:0]  id_funct7 = id_ir[31:25];
    wire [4:0]  id_rs1    = id_ir[19:15];
    wire [4:0]  id_rs2    = id_ir[24:20];
    wire [4:0]  id_rd     = id_ir[11:7];

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
                12'h300, 12'h304, 12'h305, 12'h340,
                12'h341, 12'h342, 12'h343, 12'h344: csr_writable = 1'b1;
                default: csr_writable = 1'b0;
            endcase
        end
    endfunction

    reg        id_rd_write_req;
    reg        id_uses_rs1;
    reg        id_uses_rs2;
    reg        id_mem_read;
    reg        id_mem_write;
    reg [2:0]  id_mem_size;
    reg        id_mem_unsigned;
    reg        id_branch;
    reg        id_jump;
    reg        id_jalr;
    reg        id_csr_read;
    reg        id_csr_write;
    reg [4:0]  id_csr_uimm;
    reg        id_illegal;
    reg        id_ecall;
    reg        id_ebreak;
    reg        id_mret;
    reg        id_fence;
    reg [31:0] id_imm;
    reg [4:0]  id_shamt;

    always @(*) begin
        id_rd_write_req = 1'b0;
        id_uses_rs1     = 1'b0;
        id_uses_rs2     = 1'b0;
        id_mem_read     = 1'b0;
        id_mem_write    = 1'b0;
        id_mem_size     = 3'd0;
        id_mem_unsigned = 1'b0;
        id_branch       = 1'b0;
        id_jump         = 1'b0;
        id_jalr         = 1'b0;
        id_csr_read     = 1'b0;
        id_csr_write    = 1'b0;
        id_csr_uimm     = id_ir[19:15];
        id_illegal      = 1'b1;
        id_ecall        = 1'b0;
        id_ebreak       = 1'b0;
        id_mret         = 1'b0;
        id_fence        = 1'b0;
        id_imm          = 32'b0;
        id_shamt        = id_ir[24:20];

        case (id_opcode)
            OP_LUI: begin
                id_rd_write_req = 1'b1;
                id_imm = id_imm_u;
                id_illegal = 1'b0;
            end
            OP_AUIPC: begin
                id_rd_write_req = 1'b1;
                id_imm = id_imm_u;
                id_illegal = 1'b0;
            end
            OP_JAL: begin
                id_rd_write_req = 1'b1;
                id_jump = 1'b1;
                id_imm = id_imm_j;
                id_illegal = 1'b0;
            end
            OP_JALR: begin
                id_uses_rs1 = 1'b1;
                id_rd_write_req = 1'b1;
                id_jump = 1'b1;
                id_jalr = 1'b1;
                id_imm = id_imm_i;
                id_illegal = (id_funct3 == 3'b000) ? 1'b0 : 1'b1;
            end
            OP_BRANCH: begin
                id_uses_rs1 = 1'b1;
                id_uses_rs2 = 1'b1;
                id_branch = 1'b1;
                id_imm = id_imm_b;
                case (id_funct3)
                    3'b000, 3'b001, 3'b100, 3'b101, 3'b110, 3'b111: id_illegal = 1'b0;
                    default: id_illegal = 1'b1;
                endcase
            end
            OP_LOAD: begin
                id_uses_rs1 = 1'b1;
                id_rd_write_req = 1'b1;
                id_mem_read = 1'b1;
                id_imm = id_imm_i;
                case (id_funct3)
                    3'b000: begin id_mem_size = 3'd1; id_mem_unsigned = 1'b0; id_illegal = 1'b0; end
                    3'b001: begin id_mem_size = 3'd2; id_mem_unsigned = 1'b0; id_illegal = 1'b0; end
                    3'b010: begin id_mem_size = 3'd4; id_mem_unsigned = 1'b0; id_illegal = 1'b0; end
                    3'b100: begin id_mem_size = 3'd1; id_mem_unsigned = 1'b1; id_illegal = 1'b0; end
                    3'b101: begin id_mem_size = 3'd2; id_mem_unsigned = 1'b1; id_illegal = 1'b0; end
                    default: id_illegal = 1'b1;
                endcase
            end
            OP_STORE: begin
                id_uses_rs1 = 1'b1;
                id_uses_rs2 = 1'b1;
                id_mem_write = 1'b1;
                id_imm = id_imm_s;
                case (id_funct3)
                    3'b000: begin id_mem_size = 3'd1; id_illegal = 1'b0; end
                    3'b001: begin id_mem_size = 3'd2; id_illegal = 1'b0; end
                    3'b010: begin id_mem_size = 3'd4; id_illegal = 1'b0; end
                    default: id_illegal = 1'b1;
                endcase
            end
            OP_IMM: begin
                id_uses_rs1 = 1'b1;
                id_rd_write_req = 1'b1;
                id_imm = id_imm_i;
                case (id_funct3)
                    3'b000, 3'b010, 3'b011, 3'b100, 3'b110, 3'b111: id_illegal = 1'b0;
                    3'b001: id_illegal = (id_funct7 == 7'b0000000) ? 1'b0 : 1'b1;
                    3'b101: id_illegal = ((id_funct7 == 7'b0000000) ||
                                          (id_funct7 == 7'b0100000)) ? 1'b0 : 1'b1;
                    default: id_illegal = 1'b1;
                endcase
            end
            OP: begin
                id_uses_rs1 = 1'b1;
                id_uses_rs2 = 1'b1;
                id_rd_write_req = 1'b1;
                case (id_funct3)
                    3'b000: id_illegal = ((id_funct7 == 7'b0000000) ||
                                          (id_funct7 == 7'b0100000) ||
                                          (id_funct7 == 7'b0000001)) ? 1'b0 : 1'b1;
                    3'b001, 3'b010, 3'b011: id_illegal =
                        ((id_funct7 == 7'b0000000) ||
                         (id_funct7 == 7'b0000001)) ? 1'b0 : 1'b1;
                    3'b100, 3'b110, 3'b111: id_illegal =
                        ((id_funct7 == 7'b0000000) ||
                         (id_funct7 == 7'b0000001)) ? 1'b0 : 1'b1;
                    3'b101: id_illegal = ((id_funct7 == 7'b0000000) ||
                                          (id_funct7 == 7'b0100000) ||
                                          (id_funct7 == 7'b0000001)) ? 1'b0 : 1'b1;
                    default: id_illegal = 1'b1;
                endcase
            end
            OP_MISC: begin
                if ((id_funct3 == 3'b000) || (id_funct3 == 3'b001)) begin
                    id_fence = 1'b1;
                    id_illegal = 1'b0;
                end
            end
            OP_SYSTEM: begin
                case (id_funct3)
                    3'b000: begin
                        if ((id_ir[31:20] == 12'h000) && (id_rs1 == 5'd0) && (id_rd == 5'd0)) begin
                            id_ecall = 1'b1;
                            id_illegal = 1'b0;
                        end else if ((id_ir[31:20] == 12'h001) && (id_rs1 == 5'd0) && (id_rd == 5'd0)) begin
                            id_ebreak = 1'b1;
                            id_illegal = 1'b0;
                        end else if ((id_ir[31:20] == 12'h302) && (id_rs1 == 5'd0) && (id_rd == 5'd0)) begin
                            id_mret = 1'b1;
                            id_illegal = 1'b0;
                        end else if ((id_ir[31:20] == 12'h105) && (id_rs1 == 5'd0) && (id_rd == 5'd0)) begin
                            id_fence = 1'b1;      // WFI treated as NOP
                            id_illegal = 1'b0;
                        end
                    end
                    3'b001, 3'b010, 3'b011: begin
                        id_uses_rs1 = 1'b1;
                        id_rd_write_req = 1'b1;
                        id_csr_read = 1'b1;
                        id_csr_write = 1'b1;
                        if (((id_funct3 == 3'b010) || (id_funct3 == 3'b011)) && (id_rs1 == 5'd0))
                            id_csr_write = 1'b0;
                        if (!csr_valid(id_ir[31:20]))
                            id_illegal = 1'b1;
                        else if (id_csr_write && !csr_writable(id_ir[31:20]))
                            id_illegal = 1'b1;
                        else
                            id_illegal = 1'b0;
                    end
                    3'b101, 3'b110, 3'b111: begin
                        id_rd_write_req = 1'b1;
                        id_csr_read = 1'b1;
                        id_csr_write = 1'b1;
                        if (((id_funct3 == 3'b110) || (id_funct3 == 3'b111)) && (id_ir[19:15] == 5'd0))
                            id_csr_write = 1'b0;
                        if (!csr_valid(id_ir[31:20]))
                            id_illegal = 1'b1;
                        else if (id_csr_write && !csr_writable(id_ir[31:20]))
                            id_illegal = 1'b1;
                        else
                            id_illegal = 1'b0;
                    end
                    default: id_illegal = 1'b1;
                endcase
            end
            default: id_illegal = 1'b1;
        endcase
    end

    //--------------------------------------------------------------------------
    // EX
    //--------------------------------------------------------------------------
    reg [31:0] EX_PC;
    reg [31:0] EX_IR;
    reg        EX_valid;
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

    wire [31:0] ex_rs1 = EX_rs1_data;
    wire [31:0] ex_rs2 = EX_rs2_data;

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
    wire ex_control = EX_valid && (EX_jump || (EX_branch && ex_branch_taken));
    wire ex_target_misaligned = ex_control && (ex_target[1:0] != 2'b00);

    // memory address / store strobes
    wire ex_mem_access = EX_valid && (EX_mem_read || EX_mem_write);
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
                    EX_wdata = {24'b0, ex_rs2[7:0]};
                    EX_wstrb = 4'b0001;
                    case (EX_addr[1:0])
                        2'b00: begin EX_wdata = {24'b0, ex_rs2[7:0]};              EX_wstrb = 4'b0001; end
                        2'b01: begin EX_wdata = {16'b0, ex_rs2[7:0], 8'b0};        EX_wstrb = 4'b0010; end
                        2'b10: begin EX_wdata = {8'b0, ex_rs2[7:0], 16'b0};        EX_wstrb = 4'b0100; end
                        default: begin EX_wdata = {ex_rs2[7:0], 24'b0};            EX_wstrb = 4'b1000; end
                    endcase
                end
                3'd2: begin
                    EX_wdata = {16'b0, ex_rs2[15:0]};
                    EX_wstrb = 4'b0011;
                    case (EX_addr[1])
                        1'b0: begin EX_wdata = {16'b0, ex_rs2[15:0]};              EX_wstrb = 4'b0011; end
                        default: begin EX_wdata = {ex_rs2[15:0], 16'b0};           EX_wstrb = 4'b1100; end
                    endcase
                end
                default: begin
                    EX_wdata = ex_rs2;
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

    wire [31:0] mip_comb = {20'b0, ext_irq_i, 3'b0, timer_irq_i, 3'b0, soft_irq_i, 3'b0};

    always @(*) begin
        case (EX_csr_addr)
            12'h300: csr_rdata = mstatus;
            12'h301: csr_rdata = 32'h4000_1100;   // MXL=1, I, M
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
            OP_JAL:   ex_alu_result = EX_PC + 32'd4;
            OP_JALR:  ex_alu_result = EX_PC + 32'd4;
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

    reg [31:0] ex_result;
    always @(*) begin
        if (ex_is_mul)
            ex_result = ex_mul_result;
        else if (ex_is_div)
            ex_result = (div_state == DIV_DONE) ? div_result_comb : 32'b0;
        else
            ex_result = ex_alu_result;
    end

    //--------------------------------------------------------------------------
    // traps / interrupts
    //--------------------------------------------------------------------------
    wire ex_illegal_trap  = EX_valid && EX_illegal;
    wire ex_ecall_trap    = EX_valid && EX_ecall;
    wire ex_ebreak_trap   = EX_valid && EX_ebreak;
    wire ex_mem_misalign  = ex_mem_access && ex_misaligned;
    wire ex_fetch_misalign = ex_target_misaligned;

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
                        ex_mem_misalign || ex_fetch_misalign;

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
        end else if (ex_mem_misalign) begin
            ex_exc_cause = EX_mem_read ? 32'd4 : 32'd6;
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
    reg [31:0] MEM_wdata;
    reg [3:0]  MEM_wstrb;
    reg [1:0]  MEM_load_src;

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

    // bus / div freeze
    wire mem_bus_access = MEM_valid && (MEM_mem_read || MEM_mem_write) &&
                          (MEM_addr[31:28] >= 4'h2);
    wire ex_bus_access = EX_valid && (EX_mem_read || EX_mem_write) &&
                         (EX_addr[31:28] >= 4'h2);
    // Back-to-back bus accesses would keep req high; insert one bubble so the
    // bus sees a fresh req edge for every transaction.
    wire bus_conflict = mem_bus_access && ex_bus_access;
    wire bus_wait = mem_bus_access && !bus_ready_i;
    wire pipe_freeze = bus_wait || div_busy;

    wire ex_interrupt = EX_valid && !ex_exception && irq_pending &&
                        !bus_wait && !div_busy;
    wire trap_taken = !bus_wait && !div_busy &&
                      (ex_exception || ex_interrupt);
    wire trap_is_interrupt = !ex_exception;
    wire [31:0] trap_cause = ex_exception ? ex_exc_cause : irq_cause;
    wire [31:0] trap_tval  = ex_exception ? ex_exc_tval : 32'b0;

    wire [31:0] mtvec_base = {mtvec[31:2], 2'b00};
    wire [31:0] trap_vector = (mtvec[1:0] == 2'b01 && trap_is_interrupt) ?
                              (mtvec_base + {trap_cause[29:0], 2'b00}) : mtvec_base;

    wire mret_taken = EX_valid && EX_mret && !trap_taken && !pipe_freeze;
    wire control_redirect = ex_control && !ex_target_misaligned &&
                            !ex_exception && !trap_taken && !pipe_freeze;

    wire redirect = trap_taken || mret_taken || control_redirect;
    wire [31:0] redirect_pc = trap_taken ? trap_vector :
                              mret_taken ? mepc : ex_target;

    // register file
    reg [31:0] regfiles [0:31];
    reg [31:0] wb_rd_data;

    wire [31:0] wb_raw_data = (WB_load_src == 2'd1) ? mem_itcm_rdata_q :
                              (WB_load_src == 2'd2) ? mem_dtcm_rdata_q :
                              (WB_load_src == 2'd3) ? bus_rdata_q : 32'b0;

    always @(*) begin
        wb_rd_data = WB_result;
        if (WB_load_src != 2'd0) begin
            case (WB_load_size)
                3'd1: begin
                    case (WB_addr_low)
                        2'b00: wb_rd_data = WB_load_unsigned ? {24'b0, wb_raw_data[7:0]}  : {{24{wb_raw_data[7]}},  wb_raw_data[7:0]};
                        2'b01: wb_rd_data = WB_load_unsigned ? {24'b0, wb_raw_data[15:8]} : {{24{wb_raw_data[15]}}, wb_raw_data[15:8]};
                        2'b10: wb_rd_data = WB_load_unsigned ? {24'b0, wb_raw_data[23:16]} : {{24{wb_raw_data[23]}}, wb_raw_data[23:16]};
                        default: wb_rd_data = WB_load_unsigned ? {24'b0, wb_raw_data[31:24]} : {{24{wb_raw_data[31]}}, wb_raw_data[31:24]};
                    endcase
                end
                3'd2: begin
                    if (WB_addr_low[1])
                        wb_rd_data = WB_load_unsigned ? {16'b0, wb_raw_data[31:16]} : {{16{wb_raw_data[31]}}, wb_raw_data[31:16]};
                    else
                        wb_rd_data = WB_load_unsigned ? {16'b0, wb_raw_data[15:0]}  : {{16{wb_raw_data[15]}}, wb_raw_data[15:0]};
                end
                default: wb_rd_data = wb_raw_data;
            endcase
        end
    end

    // read operands with WB bypass (x0 is hardwired)
    wire [31:0] id_rs1_data = (id_rs1 == 5'd0) ? 32'b0 :
                              (WB_valid && WB_rd_write_req && (WB_rd == id_rs1)) ?
                              wb_rd_data : regfiles[id_rs1];
    wire [31:0] id_rs2_data = (id_rs2 == 5'd0) ? 32'b0 :
                              (WB_valid && WB_rd_write_req && (WB_rd == id_rs2)) ?
                              wb_rd_data : regfiles[id_rs2];

    // hazard: stall ID while an older instruction has not reached WB
    wire id_dep_ex = ID_valid && EX_valid && EX_rd_write_req && (EX_rd != 5'd0) &&
                     ((id_uses_rs1 && (id_rs1 == EX_rd)) ||
                      (id_uses_rs2 && (id_rs2 == EX_rd)));
    wire id_dep_mem = ID_valid && MEM_valid && MEM_rd_write_req && (MEM_rd != 5'd0) &&
                      ((id_uses_rs1 && (id_rs1 == MEM_rd)) ||
                       (id_uses_rs2 && (id_rs2 == MEM_rd)));
    wire raw_stall = id_dep_ex || id_dep_mem;

    //--------------------------------------------------------------------------
    // TCM / bus access
    //--------------------------------------------------------------------------
    wire mem_itcm_we = MEM_valid && MEM_mem_write && (MEM_addr[31:28] == 4'h0) && !pipe_freeze;
    wire mem_dtcm_we = MEM_valid && MEM_mem_write && (MEM_addr[31:28] == 4'h1) && !pipe_freeze;

    assign bus_req_o   = mem_bus_access;
    assign bus_we_o    = MEM_mem_write;
    assign bus_addr_o  = {MEM_addr[31:2], 2'b00};
    assign bus_wdata_o = MEM_wdata;
    assign bus_wstrb_o = MEM_wstrb;

    integer i_itcm;
    integer i_dtcm;

    always @(posedge clk) begin
        if (mem_itcm_we) begin
            for (i_itcm = 0; i_itcm < 4; i_itcm = i_itcm + 1) begin
                if (MEM_wstrb[i_itcm])
                    ITCM[MEM_addr[ITCM_AW+1:2]][i_itcm*8 +: 8] <= MEM_wdata[i_itcm*8 +: 8];
            end
        end
    end

    always @(posedge clk) begin
        if (mem_dtcm_we) begin
            for (i_dtcm = 0; i_dtcm < 4; i_dtcm = i_dtcm + 1) begin
                if (MEM_wstrb[i_dtcm])
                    DTCM[MEM_addr[DTCM_AW+1:2]][i_dtcm*8 +: 8] <= MEM_wdata[i_dtcm*8 +: 8];
            end
        end
    end

    always @(posedge clk) begin
        if_rdata_q      <= ITCM[IF_PC[ITCM_AW+1:2]];
        mem_itcm_rdata_q <= ITCM[MEM_addr[ITCM_AW+1:2]];
        mem_dtcm_rdata_q <= DTCM[MEM_addr[DTCM_AW+1:2]];
    end

    always @(posedge clk) begin
        if (!pipe_freeze && mem_bus_access && MEM_mem_read && bus_ready_i)
            bus_rdata_q <= bus_rdata_i;
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
    always @(posedge clk or negedge rstn) begin
        if (!rstn) begin
            IF_PC   <= RESET_PC;
            IF_PC_q <= RESET_PC;
        end else begin
            IF_PC_q <= IF_PC;
            if (redirect)
                IF_PC <= redirect_pc;
            else if (pipe_freeze || raw_stall || bus_conflict)
                IF_PC <= IF_PC;
            else
                IF_PC <= IF_PC + 32'd4;
        end
    end

    always @(posedge clk or negedge rstn) begin
        if (!rstn) begin
            ID_PC        <= 32'b0;
            ID_IR        <= NOP;
            ID_valid     <= 1'b0;
            id_flush_q   <= 1'b1;
            IF_ID_IR_hold <= NOP;
            IF_ID_PC_hold <= 32'b0;
            IF_ID_hold   <= 1'b0;
        end else if (redirect) begin
            ID_PC         <= 32'b0;
            ID_IR         <= NOP;
            ID_valid      <= 1'b0;
            id_flush_q    <= 1'b1;
            IF_ID_hold    <= 1'b0;
        end else if (id_flush_q) begin
            ID_PC         <= 32'b0;
            ID_IR         <= NOP;
            ID_valid      <= 1'b0;
            id_flush_q    <= 1'b0;
            IF_ID_hold    <= 1'b0;
        end else if (pipe_freeze || raw_stall || bus_conflict) begin
            if (!IF_ID_hold) begin
                IF_ID_IR_hold <= id_capture_ir;
                IF_ID_PC_hold <= id_capture_pc;
                IF_ID_hold    <= 1'b1;
            end
        end else begin
            ID_PC     <= id_capture_pc;
            ID_IR     <= id_capture_ir;
            ID_valid  <= 1'b1;
            IF_ID_hold <= 1'b0;
        end
    end

    always @(posedge clk or negedge rstn) begin
        if (!rstn) begin
            EX_PC           <= 32'b0;
            EX_IR           <= NOP;
            EX_valid        <= 1'b0;
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
        end else if (pipe_freeze) begin
            // hold
        end else if (trap_taken) begin
            // the faulting/interrupted instruction must not enter MEM
            EX_valid <= 1'b0;
        end else if (bus_conflict) begin
            // hold the younger bus access in EX while the older one drains
        end else begin
            EX_PC           <= ID_PC;
            EX_IR           <= ID_IR;
            EX_valid        <= ID_valid && !redirect && !raw_stall;
            EX_opcode       <= id_opcode;
            EX_funct3       <= id_funct3;
            EX_funct7       <= id_funct7;
            EX_rs1          <= id_rs1;
            EX_rs2          <= id_rs2;
            EX_rd           <= id_rd;
            EX_rs1_data     <= id_rs1_data;
            EX_rs2_data     <= id_rs2_data;
            EX_imm          <= id_imm;
            EX_shamt        <= id_shamt;
            EX_rd_write_req <= id_rd_write_req;
            EX_mem_read     <= id_mem_read;
            EX_mem_write    <= id_mem_write;
            EX_mem_size     <= id_mem_size;
            EX_mem_unsigned <= id_mem_unsigned;
            EX_branch       <= id_branch;
            EX_jump         <= id_jump;
            EX_jalr         <= id_jalr;
            EX_csr_read     <= id_csr_read;
            EX_csr_write    <= id_csr_write;
            EX_csr_addr     <= id_ir[31:20];
            EX_csr_uimm     <= id_csr_uimm;
            EX_illegal      <= id_illegal;
            EX_ecall        <= id_ecall;
            EX_ebreak       <= id_ebreak;
            EX_mret         <= id_mret;
            EX_fence        <= id_fence;
        end
    end

    always @(posedge clk or negedge rstn) begin
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
        end else if (pipe_freeze) begin
            // hold
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
        end
    end

    always @(posedge clk or negedge rstn) begin
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
        end else if (pipe_freeze) begin
            // hold
        end else begin
            WB_PC            <= MEM_PC;
            WB_IR            <= MEM_IR;
            WB_valid         <= MEM_valid;
            WB_rd            <= MEM_rd;
            WB_rd_write_req  <= MEM_rd_write_req;
            WB_result        <= MEM_result;
            WB_load_size     <= MEM_mem_size;
            WB_load_unsigned <= MEM_mem_unsigned;
            WB_addr_low      <= MEM_addr[1:0];
            WB_load_src      <= MEM_load_src;
        end
    end

    // write-back
    always @(posedge clk) begin
        if (WB_valid && WB_rd_write_req && (WB_rd != 5'd0))
            regfiles[WB_rd] <= wb_rd_data;
    end

    //--------------------------------------------------------------------------
    // divider FSM
    //--------------------------------------------------------------------------
    always @(posedge clk or negedge rstn) begin
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
                  !pipe_freeze;

    always @(posedge clk or negedge rstn) begin
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
        end else begin
            cycle_cnt <= cycle_cnt + 64'd1;

            if (!pipe_freeze && MEM_valid)
                instret_cnt <= instret_cnt + 64'd1;

            if (trap_taken) begin
                mepc        <= EX_PC & 32'hffff_fffc;
                mcause      <= trap_cause;
                mtval       <= trap_tval;
                mstatus[7]  <= mstatus[3];
                mstatus[3]  <= 1'b0;
                mstatus[12:11] <= 2'b11;
            end else if (mret_taken) begin
                mstatus[3]  <= mstatus[7];
                mstatus[7]  <= 1'b1;
                mstatus[12:11] <= 2'b11;
            end else if (csr_we) begin
                case (EX_csr_addr)
                    12'h300: mstatus  <= csr_wdata;
                    12'h304: mie      <= csr_wdata;
                    12'h305: mtvec    <= csr_wdata;
                    12'h340: mscratch <= csr_wdata;
                    12'h341: mepc     <= {csr_wdata[31:2], 2'b00};
                    12'h342: mcause   <= csr_wdata;
                    12'h343: mtval    <= csr_wdata;
                    default: ;
                endcase
            end
        end
    end

endmodule
