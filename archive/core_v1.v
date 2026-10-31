module core (
    input clk,rstn,

    output core_read_bus_req_o,
    input core_read_bus_busy_i,
    output [31:0] core_read_bus_addr_o,
    input [31:0] core_read_bus_data_i,
    
    output core_write_bus_req_o,
    input core_write_bus_busy_i,
    output [31:0] core_write_bus_addr_o,
    output [31:0] core_write_bus_data_o
);

/*****************************  IF   *********************************/
    reg [31:0] IF_PC;
    always @(posedge clk,negedge rstn) begin
        if (~rstn) begin
            IF_PC <= 0;
        end
        else if (EX_jump_req) begin
            IF_PC <= EX_jump_addr;
        end
        else if (ctrl_ITCM_read_IR_busy || ctrl_rs_wait_rd_hold) begin //hold
            IF_PC <= IF_PC;
        end
        else begin
            IF_PC <= IF_PC+4;
        end
    end

/*******************************  IF ID pipline   ************************************/
    reg [31:0] ID_PC;
    always @(posedge clk or negedge rstn) begin
        if(~rstn || ctrl_EX_jump_RST_pipline || ctrl_ITCM_read_IR_busy)begin
            ID_PC<=0;
        end 
        else if(ctrl_rs_wait_rd_hold) begin     //hold
            ID_PC<=ID_PC;
        end
        else begin
            ID_PC<=IF_PC;
        end
    end

    //when hold, IR while hold ,it can`t change.
    //ID_read_ITCM_IR_hold and IF_ID_hold is used by next clk,
    //because IICM read need one clk
    reg [31:0] ID_read_ITCM_IR_hold;
    reg IF_ID_hold;
    always @(posedge clk or negedge rstn) begin
        if (~rstn || ctrl_EX_jump_RST_pipline) begin
            ID_read_ITCM_IR_hold<=0;
            IF_ID_hold<=0;
        end
        else if(ctrl_rs_wait_rd_hold) begin     //hold
            ID_read_ITCM_IR_hold <= ID_read_ITCM_IR;
            IF_ID_hold<=1;
        end
        else begin
            ID_read_ITCM_IR_hold<=0;
            IF_ID_hold<=0;
        end
    end
    
    //when hold ,the next clk ID reading IR will hold
    //And duo to pipline need a none IR, so ID reading IR will be choosed again.  
    wire [31:0] ID_read_ITCM_IR = IF_ID_hold ? ID_read_ITCM_IR_hold : ((ctrl_add_NOP_IR_to_ID) ? 32'b1 : ITCM_read_data);

/*******************************   ID    ************************************/
    wire [6:0] ID_opcode = ID_read_ITCM_IR[6:0];
    wire [2:0] ID_funct3 = ID_read_ITCM_IR[14:12];
    wire [6:0] ID_funct7 = ID_read_ITCM_IR[31:25];
    wire [4:0] ID_rs1    = ID_read_ITCM_IR[19:15];
    wire [4:0] ID_rs2    = ID_read_ITCM_IR[24:20];
    wire [4:0] ID_rd     = ID_read_ITCM_IR[11:7];

    wire [31:0] ID_imm_type_u = {ID_read_ITCM_IR[31:12],12'b0};
    wire [31:0] ID_imm_type_i = {{20{ID_read_ITCM_IR[31]}},ID_read_ITCM_IR[31:20]};
    wire [31:0] ID_imm_type_s = {{20{ID_read_ITCM_IR[31]}},ID_read_ITCM_IR[31:25],ID_read_ITCM_IR[11:7]};
    wire [5:0] ID_shamt = ID_read_ITCM_IR[25:20];


    //rd reg write req
    reg ID_rd_write_req;
    always @(*) begin  
        case (ID_opcode)
            7'b0110111,7'b0010111,7'b1101111:begin    //lui auipc jal
                ID_rd_write_req   = 1;
            end
            
            7'b1100111:begin
                case (ID_funct3)
                    3'b000:begin    //jalr
                        ID_rd_write_req   = 1;
                    end
                    default:begin
                        ID_rd_write_req   = 0;
                    end
                endcase
            end
            
            7'b0000011:begin
                case (ID_funct3)
                    3'b000,3'b001,3'b010,3'b100,3'b101:begin    //lb lh lw lbu lhu
                        ID_rd_write_req = 1;
                    end
                    default:begin
                        ID_rd_write_req   = 0;
                    end
                endcase
            end
            
            7'b0010011:begin
                case (ID_funct3)
                    3'b000,3'b010,3'b011,3'b100,3'b110,3'b111:begin    //addi slti sltiu xori ori andi
                        ID_rd_write_req   = 1;
                    end
                    3'b001:begin
                        case (ID_funct7)
                            7'b0000000:begin    //slli
                                ID_rd_write_req   = 1;
                            end
                            default:begin
                                ID_rd_write_req   = 0;
                            end
                        endcase
                    end
                    3'b101:begin
                        case (ID_funct7)
                            7'b0000000,7'b0100000:begin    //srli srai
                                ID_rd_write_req   = 1;
                            end
                            default:begin
                                ID_rd_write_req   = 0;
                            end
                        endcase
                    end
                    default:begin
                        ID_rd_write_req   = 0;
                    end
                endcase
            end
            
            7'b0110011:begin
                case (ID_funct3)
                    3'b000:begin
                        case (ID_funct7)
                            7'b0000000,7'b0100000,7'b0000001:begin    //add sub mul
                                ID_rd_write_req   = 1;
                            end
                            default:begin
                                ID_rd_write_req   = 0;
                            end
                        endcase
                    end
                    3'b001:begin
                        case (ID_funct7)
                            7'b0000000,7'b0000001:begin    //sll mulh
                                ID_rd_write_req   = 1;
                            end
                            default:begin
                                ID_rd_write_req   = 0;
                            end
                        endcase
                    end
                    3'b010:begin
                        case (ID_funct7)
                            7'b0000000,7'b0000001:begin    //slt mulhsu
                                ID_rd_write_req   = 1;
                            end
                            default:begin
                                ID_rd_write_req   = 0;
                            end
                        endcase
                    end
                    3'b011:begin
                        case (ID_funct7)
                            7'b0000000,7'b0000001:begin    //sltu mulhu
                                ID_rd_write_req = 1;
                            end
                            default:begin
                                ID_rd_write_req   = 0;
                            end
                        endcase
                    end
                    3'b100:begin
                        case (ID_funct7)
                            7'b0000000,7'b0000001:begin    //xor div
                                ID_rd_write_req   = 1;
                            end
                            default:begin
                                ID_rd_write_req   = 0;
                            end
                        endcase
                    end
                    3'b101:begin
                        case (ID_funct7)
                            7'b0000000,7'b0100000,7'b0000001:begin    //srl sra divu
                                ID_rd_write_req   = 1;
                            end
                            default:begin
                                ID_rd_write_req   = 0;
                            end
                        endcase
                    end
                    3'b110:begin
                        case (ID_funct7)
                            7'b0000000,7'b0000001:begin    //or rem
                                ID_rd_write_req   = 1;
                            end
                            default:begin
                                ID_rd_write_req   = 0;
                            end
                        endcase
                    end
                    3'b111:begin
                        case (ID_funct7)
                            7'b0000000,7'b0000001:begin    //and remu
                                ID_rd_write_req   = 1;
                            end
                            default:begin
                                ID_rd_write_req   = 0;
                            end
                        endcase
                    end
                    default:begin
                        ID_rd_write_req   = 0;
                    end
                endcase
            end
            
            7'b1110011:begin
                case (ID_funct3)
                    3'b001,3'b010,3'b011,3'b101,3'b110,3'b111:begin    //csrrw
                        ID_rd_write_req   = 1;
                    end
                    default:begin
                        ID_rd_write_req   = 0;
                    end
                endcase
            end

            default:begin
                ID_rd_write_req   = 0;
            end
        endcase
    end


    //CSR read and write req and addr
    reg [11:0] ID_CSR_read_write_addr;
    always @(*) begin
        ID_CSR_read_write_addr = ID_read_ITCM_IR[31:20];
    end
    reg ID_CSR_read_req;
    always @(*) begin
        case (ID_opcode)
            7'b1110011:begin
                ID_CSR_read_req = 1;
            end
            default: begin
                ID_CSR_read_req = 0;
            end
        endcase
    end
    reg ID_CSR_write_req;
    always @(*) begin
        case(ID_opcode)
            7'b1110011:begin
                case (ID_funct3)
                    3'b001,3'b010,3'b011,3'b101,3'b110,3'b111:begin    //csrrw csrrs csrrc csrrwi csrrsi csrrci
                        ID_CSR_write_req   = 1;
                    end
                    default:begin
                        ID_CSR_write_req   = 0;
                    end
                endcase
            end
            default:begin
                ID_CSR_write_req   = 0;
            end
        endcase
    end


    //jump address temp
    wire [31:0] ID_jal_jump_addr={{11{ID_read_ITCM_IR[31]}},ID_read_ITCM_IR[31],ID_read_ITCM_IR[19:12],ID_read_ITCM_IR[20],ID_read_ITCM_IR[30:21],1'b0};      //PC jump addr for jal inst
    wire [31:0] ID_inst_B_jump_addr={{19{ID_read_ITCM_IR[31]}},ID_read_ITCM_IR[31],ID_read_ITCM_IR[7],ID_read_ITCM_IR[30:25],ID_read_ITCM_IR[11:8],1'b0};    //PC jump addr for B class inst

/****************************** ID EX pipline  *************************************/

    reg [31:0] EX_IR,EX_PC;
    reg [6:0] EX_opcode,EX_funct7;   
    reg [2:0] EX_funct3;
    reg [4:0] EX_rs1,EX_rs2,EX_rd;
    reg EX_rd_write_req;

    reg [31:0] EX_imm_type_u;
    reg [31:0] EX_imm_type_i;
    reg [31:0] EX_imm_type_s;
    reg [5:0] EX_shamt;

    reg EX_CSR_write_req;
    reg [11:0] EX_CSR_read_write_addr;

    reg [31:0] EX_jal_jump_addr,EX_inst_B_jump_addr;

    always @(posedge clk or negedge rstn) begin
        if(~rstn || ctrl_EX_jump_RST_pipline || ctrl_rs_wait_rd_hold)begin
            EX_PC<=0;
            EX_IR <= 32'b1;
            EX_opcode <= 0;
            EX_funct7<=0;
            EX_funct3 <= 0;

            EX_rs1<=0;
            EX_rs2<=0;

            EX_rd<=0;
            EX_rd_write_req<=0;

            EX_imm_type_u<=0;
            EX_imm_type_i<=0;
            EX_imm_type_s<=0;
            EX_shamt<=0;

            EX_CSR_write_req<=0;
            EX_CSR_read_write_addr<=0;

            EX_jal_jump_addr<=0;
            EX_inst_B_jump_addr<=0;
        end else begin
            EX_PC <= ID_PC;
            EX_IR <= ID_read_ITCM_IR;
            EX_opcode <= ID_opcode;
            EX_funct7<=ID_funct7;
            EX_funct3 <= ID_funct3;

            EX_rs1<=ID_rs1;
            EX_rs2<=ID_rs2;

            EX_rd<=ID_rd;
            EX_rd_write_req<=ID_rd_write_req;

            EX_imm_type_u<=ID_imm_type_u;
            EX_imm_type_i<=ID_imm_type_i;
            EX_imm_type_s<=ID_imm_type_s;
            EX_shamt<=ID_shamt;

            EX_CSR_write_req<=ID_CSR_write_req;
            EX_CSR_read_write_addr<=ID_CSR_read_write_addr;

            EX_jal_jump_addr<=ID_jal_jump_addr;
            EX_inst_B_jump_addr<=ID_inst_B_jump_addr;
        end
    end

/******************************  EX   *************************************/
    reg [31:0] EX_read_rs1_data,EX_read_rs2_data;//from regfiles
    reg [31:0] EX_read_CSR_data;//from CSRs


    //EX result
    reg [31:0] EX_result;
    always @(*) begin
        case (EX_opcode)
            7'b0110111:begin    //lui
                EX_result = EX_imm_type_u;
            end
            
            7'b0010111:begin    //auipc
                EX_result = EX_PC+EX_imm_type_u;
            end
            
            7'b1101111:begin    //jal
                EX_result = EX_PC+4;
            end
            
            7'b1100111:begin
                case (EX_funct3)
                    3'b000:begin    //jalr
                        EX_result = EX_PC+4;
                    end
                    default:begin
                        EX_result = 0;
                    end
                endcase
            end
            
            7'b0010011:begin
                case (EX_funct3)
                    3'b000:begin    //addi
                        EX_result = EX_read_rs1_data + EX_imm_type_i;
                    end
                    3'b010:begin    //slti
                        if ({{~EX_read_rs1_data[31]},EX_read_rs1_data[30:0]}<{{~EX_imm_type_i[31]},EX_imm_type_i[30:0]}) begin
                            EX_result = 1;
                        end
                        else begin
                            EX_result = 0;
                        end
                    end
                    3'b011:begin    //sltiu
                        if (EX_read_rs1_data<EX_imm_type_i) begin
                            EX_result = 1;
                        end
                        else begin
                            EX_result = 0;
                        end
                    end
                    3'b100:begin    //xori
                        EX_result = EX_read_rs1_data ^ EX_imm_type_i;
                    end
                    3'b110:begin    //ori
                        EX_result = EX_read_rs1_data | EX_imm_type_i;
                    end
                    3'b111:begin    //andi
                        EX_result = EX_read_rs1_data & EX_imm_type_i;
                    end
                    3'b001:begin
                        case (EX_funct7)
                            7'b0000000:begin    //slli
                                EX_result = EX_read_rs1_data<<EX_shamt;
                            end
                            default:begin
                                EX_result = 0;
                            end
                        endcase
                    end
                    3'b101:begin
                        case (EX_funct7)
                            7'b0000000:begin    //srli
                                EX_result = EX_read_rs1_data>>EX_shamt;
                            end
                            7'b0100000:begin    //srai
                                EX_result = $signed(EX_read_rs1_data)>>>EX_shamt;//(EX_read_rs1_data>>EX_shamt) | ({32{EX_read_rs1_data[31]}} & (~(32'hffff_ffff>>EX_rs2)));
                            end
                            default:begin
                                EX_result = 0;
                            end
                        endcase
                    end
                    default:begin
                        EX_result = 0;
                    end
                endcase
            end
            
            7'b0110011:begin
                case (EX_funct3)
                    3'b000:begin
                        case (EX_funct7)
                            7'b0000000:begin    //add
                                EX_result = EX_read_rs1_data + EX_read_rs2_data;
                            end
                            7'b0100000:begin    //sub
                                EX_result = EX_read_rs1_data - EX_read_rs2_data;
                            end/*
                            7'b0000001:begin    //mul
                                EX_result = EX_read_rs1_data * EX_read_rs2_data;
                            end*/
                            default:begin
                                EX_result = 0;
                            end
                        endcase
                    end
                    3'b001:begin
                        case (EX_funct7)
                            7'b0000000:begin    //sll
                                EX_result = EX_read_rs1_data<<EX_read_rs2_data[4:0];
                            end/*
                            7'b0000001:begin    //mulh
                                if (EX_read_rs1_data[31]^EX_read_rs2_data[31]) begin
                                    EX_result = EX_mul_result_temp[63:32];
                                end
                                else begin
                                    EX_result = EX_mul_result_temp[63:32];
                                end
                            end*/
                            default:begin
                                EX_result = 0;
                            end
                        endcase
                    end
                    3'b010:begin
                        case (EX_funct7)
                            7'b0000000:begin    //slt
                                if ({{~EX_read_rs1_data[31]},EX_read_rs1_data[30:0]}<{{~EX_read_rs2_data[31]},EX_read_rs2_data[30:0]}) begin
                                    EX_result = 1;
                                end
                                else begin
                                    EX_result = 0;
                                end
                            end/*
                            7'b0000001:begin    //mulhsu
                                if (EX_read_rs1_data[31] == 1'b1) begin
                                    EX_result = EX_mul_result_temp[63:32];
                                end
                                else begin
                                    EX_result = EX_mul_result_temp[63:32];
                                end
                            end*/
                            default:begin
                                EX_result = 0;
                            end
                        endcase
                    end
                    3'b011:begin
                        case (EX_funct7)
                            7'b0000000:begin    //sltu
                                if (EX_read_rs1_data<EX_read_rs2_data) begin
                                    EX_result = 1;
                                end
                                else begin
                                    EX_result = 0;
                                end
                            end/*
                            7'b0000001:begin    //mulhu
                                EX_result      = EX_mul_result_temp[63:32];
                            end*/
                            default:begin
                                EX_result = 0;
                            end
                        endcase
                    end
                    3'b100:begin
                        case (EX_funct7)
                            7'b0000000:begin    //xor
                                EX_result = EX_read_rs1_data^EX_read_rs2_data;
                            end/*
                            7'b0000001:begin    //div
                                if (EX_read_rs2_data == 0) begin
                                    EX_result = 32'hffff_ffff;
                                end
                                else begin
                                    if (EX_read_rs1_data[31]^EX_read_rs2_data[31]) begin
                                       EX_result = divResultInvert;
                                    end
                                    else begin
                                       EX_result = divResult;
                                    end
                                end
                            end*/
                            default:begin
                                EX_result = 0;
                            end
                        endcase
                    end
                    3'b101:begin
                        case (EX_funct7)
                            7'b0000000:begin    //srl
                                EX_result = EX_read_rs1_data>>EX_read_rs2_data[4:0];
                            end
                            7'b0100000:begin    //sra
                                EX_result = $signed(EX_read_rs1_data)>>>EX_read_rs2_data[4:0];//(EX_read_rs1_data>>EX_read_rs2_data) | ({32{EX_read_rs1_data[31]}} & (~(32'hffff_ffff>>EX_read_rs2_data)));
                            end/*
                            7'b0000001:begin    //divu
                                if (EX_read_rs2_data == 0) begin
                                    EX_result = 32'hffff_ffff;
                                end
                                else begin
                                    EX_result   = divResult;
                                end
                            end*/
                            default:begin
                                EX_result = 0;
                            end
                        endcase
                    end
                    3'b110:begin
                        case (EX_funct7)
                            7'b0000000:begin    //or
                                EX_result = EX_read_rs1_data|EX_read_rs2_data;
                            end
                            /*
                            7'b0000001:begin    //rem
                                if (EX_read_rs2_data == 0) begin
                                    EX_result = EX_read_rs1_data;
                                end
                                else begin
                                    if (EX_read_rs1_data[31]) begin
                                        EX_result = remReasultInvert;
                                    end
                                    else begin
                                        EX_result = remReasult;
                                    end
                                end
                            end*/
                            default:begin
                                EX_result = 0;
                            end
                        endcase
                    end
                    3'b111:begin
                        case (EX_funct7)
                            7'b0000000:begin    //and
                                EX_result = EX_read_rs1_data&EX_read_rs2_data;
                            end/*
                            7'b0000001:begin    //remu
                                if (EX_read_rs2_data == 0) begin
                                    EX_result = EX_read_rs1_data;
                                end
                                else begin
                                    EX_result   = remReasult;
                                end
                            end*/
                            default:begin
                                EX_result = 0;
                            end
                        endcase
                    end
                    default:begin
                        EX_result = 0;
                    end
                endcase
            end

            7'b1110011:begin //csr*
                EX_result = EX_read_CSR_data;
            end

            default:begin
                EX_result = 0;
            end
        endcase
    end
    

    reg EX_jump_req;
    always @(*) begin
        case(EX_opcode)
            7'b1101111:begin    //jal
                EX_jump_req = 1;
            end
            7'b1100111:begin
                case (EX_funct3)
                    3'b000:begin    //jalr
                        EX_jump_req = 1;
                    end
                    default:begin
                        EX_jump_req = 0;
                    end
                endcase
            end
            7'b1100011:begin
                case (EX_funct3)
                    3'b000:begin    //beq
                        EX_jump_req = EX_read_rs1_data == EX_read_rs2_data;
                    end
                    3'b001:begin    //bne
                        EX_jump_req = EX_read_rs1_data != EX_read_rs2_data;
                    end
                    3'b100:begin    //blt
                        EX_jump_req = {{~EX_read_rs1_data[31]},EX_read_rs1_data[30:0]} < {{~EX_read_rs2_data[31]},EX_read_rs2_data[30:0]};
                    end
                    3'b101:begin    //bge
                        EX_jump_req = {{~EX_read_rs1_data[31]},EX_read_rs1_data[30:0]} >= {{~EX_read_rs2_data[31]},EX_read_rs2_data[30:0]};
                    end
                    3'b110:begin    //bltu
                        EX_jump_req = EX_read_rs1_data<EX_read_rs2_data;
                    end
                    3'b111:begin    //bgeu
                        EX_jump_req = EX_read_rs1_data >= EX_read_rs2_data;
                    end
                    default:begin
                        EX_jump_req = 0;
                    end
                endcase
            end
            default:begin
                EX_jump_req = 0;
            end
        endcase
    end
    //jump address
    reg [31:0] EX_jump_addr;
    always @(*) begin
        case(EX_opcode)
            7'b1101111:begin    //jal
                EX_jump_addr   = EX_jal_jump_addr + EX_PC;
            end
            7'b1100111:begin    //jalr
                EX_jump_addr   = (EX_read_rs1_data + EX_imm_type_i)&(~32'b1);
            end
            7'b1100011:begin    //beq bne blt bge bltu bgeu
                EX_jump_addr   = EX_inst_B_jump_addr+EX_PC;
            end
            default:begin
                EX_jump_addr   = 0;
            end
        endcase
    end


    //MEM read and write req and addr
    reg EX_read_MEM_manger_req;
    always @(*) begin
        case (EX_opcode)
            7'b0000011:begin
                case (EX_funct3)
                    3'b000,3'b001,3'b010,3'b100,3'b101:begin    //lb lh lw lbu lhu
                        EX_read_MEM_manger_req = 1;
                    end
                    default:begin
                        EX_read_MEM_manger_req = 0;
                    end
                endcase
            end
            7'b0100011:begin
                case (EX_funct3) 
                    3'b000,3'b001,3'b010:begin    //sb sh sw
                        EX_read_MEM_manger_req = 1;
                    end
                    default:begin
                        EX_read_MEM_manger_req = 0;
                    end
                endcase
            end
            default:begin
                EX_read_MEM_manger_req = 0;
            end
        endcase
    end
    reg EX_write_MEM_manger_req;
    always @(*) begin
        case (EX_opcode)
            7'b0100011:begin
                case (EX_funct3)
                    3'b000,3'b001,3'b010:begin    //sb sh sw
                        EX_write_MEM_manger_req = 1;
                    end
                    default:begin
                        EX_write_MEM_manger_req   = 0;
                    end
                endcase
            end
            default:begin
                EX_write_MEM_manger_req   = 0;
            end
        endcase
    end
    reg [31:0] EX_write_read_MEM_manger_addr;
    always @(*) begin
        case(EX_opcode)
            7'b0000011:begin    //lb lh lw lbu lhu
                EX_write_read_MEM_manger_addr = EX_read_rs1_data + EX_imm_type_i;
            end
            7'b0100011:begin    //sb sh sw
                EX_write_read_MEM_manger_addr = EX_read_rs1_data + EX_imm_type_s;
            end
            default:begin
                EX_write_read_MEM_manger_addr = 0;
            end
        endcase
    end


    //CSR write data
    reg [31:0] EX_write_CSR_data;
    always @(*) begin
        case(EX_opcode)
            7'b1110011:begin
                case (EX_funct3)
                    3'b001:begin    //csrrw
                        EX_write_CSR_data = EX_read_rs1_data;
                    end
                    3'b010:begin    //csrrs
                        EX_write_CSR_data = EX_read_CSR_data|EX_read_rs1_data;
                    end
                    3'b011:begin    //csrrc
                        EX_write_CSR_data = EX_read_CSR_data & (~EX_read_rs1_data);
                    end
                    3'b101:begin    //csrrwi
                        EX_write_CSR_data = EX_rs1;
                    end
                    3'b110:begin    //csrrsi
                        EX_write_CSR_data = EX_read_CSR_data|EX_rs1;
                    end
                    3'b111:begin    //csrrci
                        EX_write_CSR_data = EX_read_CSR_data & (~EX_rs1);
                    end
                    default:begin
                        EX_write_CSR_data = 0;
                    end
                endcase
            end
            default:begin
                EX_write_CSR_data = 0;
            end
        endcase
    end


/******************************* EX MEM pipline ****************************/
    
    reg [31:0] MEM_IR,MEM_PC;
    reg [6:0] MEM_opcode,MEM_funct7;   
    reg [2:0] MEM_funct3;

    reg [4:0] MEM_rs1,MEM_rs2,MEM_rd;
    reg MEM_rd_write_req;
    reg [31:0] MEM_rs1_data,MEM_rs2_data;

    reg [31:0] MEM_result;

    reg MEM_read_MEM_manger_req,MEM_write_MEM_manger_req;
    reg [31:0] MEM_write_read_MEM_manger_addr;

    always @(posedge clk or negedge rstn) begin
        if(~rstn)begin
            MEM_PC<=0;
            MEM_IR <= 32'b1;

            MEM_opcode <= 0;
            MEM_funct7<=0;
            MEM_funct3 <= 0;

            MEM_rs1<=0;
            MEM_rs2<=0;
            MEM_rs1_data<=0;
            MEM_rs2_data<=0;

            MEM_result<=0;

            MEM_rd<=0;
            MEM_rd_write_req<=0;
            MEM_read_MEM_manger_req<=0;
            MEM_write_MEM_manger_req<=0;
            MEM_write_read_MEM_manger_addr<=0;
        end else begin
            MEM_PC <= EX_PC;
            MEM_IR <= EX_IR;

            MEM_opcode <= EX_opcode;
            MEM_funct7<=EX_funct7;
            MEM_funct3 <= EX_funct3;

            MEM_rs1<=EX_rs1;
            MEM_rs2<=EX_rs2;
            MEM_rs1_data<=EX_read_rs1_data;
            MEM_rs2_data<=EX_read_rs2_data;

            MEM_result<=EX_result;

            MEM_rd<=EX_rd;
            MEM_rd_write_req<=EX_rd_write_req;
            MEM_read_MEM_manger_req<=EX_read_MEM_manger_req;
            MEM_write_MEM_manger_req<=EX_write_MEM_manger_req;
            MEM_write_read_MEM_manger_addr<=EX_write_read_MEM_manger_addr;
        end
    end

/****************************** MEM **************************************/


/******************************** MEM WB pipline ***************************/

    reg [31:0] WB_IR,WB_PC;
    reg [6:0] WB_opcode,WB_funct7;   
    reg [2:0] WB_funct3;

    reg [4:0] WB_rs1,WB_rs2,WB_rd;
    reg WB_rd_write_req;
    reg [31:0] WB_rs1_data,WB_rs2_data;

    reg [31:0] WB_result;

    reg WB_read_MEM_manger_req,WB_write_MEM_manger_req;
    reg [31:0] WB_write_read_MEM_manger_addr;

    always @(posedge clk or negedge rstn) begin
        if(~rstn)begin
            WB_PC <= 0;
            WB_IR <= 32'b1;

            WB_opcode <= 0;
            WB_funct7 <= 0;
            WB_funct3 <= 0;

            WB_rs1<=0;
            WB_rs2<=0;
            WB_rs1_data<=0;
            WB_rs2_data<=0;

            WB_result<=0;

            WB_rd<=0;
            WB_rd_write_req<=0;
            WB_read_MEM_manger_req<=0;
            WB_write_MEM_manger_req<=0;
            WB_write_read_MEM_manger_addr<=0;
        end else begin
            WB_PC <= MEM_PC;
            WB_IR <= MEM_IR;

            WB_opcode <= MEM_opcode;
            WB_funct7<=MEM_funct7;
            WB_funct3 <= MEM_funct3;

            WB_rs1<=MEM_rs1;
            WB_rs2<=MEM_rs2;
            WB_rs1_data<=MEM_rs1_data;
            WB_rs2_data<=MEM_rs2_data;

            WB_result<=MEM_result;

            WB_rd<=MEM_rd;
            WB_rd_write_req<=MEM_rd_write_req;
            WB_read_MEM_manger_req<=MEM_read_MEM_manger_req;
            WB_write_MEM_manger_req<=MEM_write_MEM_manger_req;
            WB_write_read_MEM_manger_addr<=MEM_write_read_MEM_manger_addr;
        end
    end

/******************************* WB **************************************/
    reg [31:0] WB_read_MEM_maneger_data; //from MEM manger

    reg [31:0] WB_rd_write_data;
    always @(*) begin
        case (WB_opcode)
            7'b0000011:begin //load
                case (WB_funct3)
                    3'b000:begin    //lb
                        case (WB_write_read_MEM_manger_addr[1:0])
                            2'b00: begin
                                WB_rd_write_data = {{24{WB_read_MEM_maneger_data[7]}}, WB_read_MEM_maneger_data[7:0]};
                            end
                            2'b01: begin
                                WB_rd_write_data = {{24{WB_read_MEM_maneger_data[15]}}, WB_read_MEM_maneger_data[15:8]};
                            end
                            2'b10: begin
                                WB_rd_write_data = {{24{WB_read_MEM_maneger_data[23]}}, WB_read_MEM_maneger_data[23:16]};
                            end
                            default: begin
                                WB_rd_write_data = {{24{WB_read_MEM_maneger_data[31]}}, WB_read_MEM_maneger_data[31:24]};
                            end
                        endcase
                    end
                    3'b001:begin    //lh
                        case (WB_write_read_MEM_manger_addr[1:0])
                            2'b00: begin
                                WB_rd_write_data = {{16{WB_read_MEM_maneger_data[15]}}, WB_read_MEM_maneger_data[15:0]};
                            end
                            default: begin
                                WB_rd_write_data = {{16{WB_read_MEM_maneger_data[31]}}, WB_read_MEM_maneger_data[31:16]};
                            end
                        endcase
                    end
                    3'b010:begin    //lw
                        WB_rd_write_data = WB_read_MEM_maneger_data;
                    end
                    3'b100:begin    //lbu
                        case (WB_write_read_MEM_manger_addr[1:0])
                            2'b00: begin
                                WB_rd_write_data = {24'b0, WB_read_MEM_maneger_data[7:0]};
                            end
                            2'b01: begin
                                WB_rd_write_data = {24'b0, WB_read_MEM_maneger_data[15:8]};
                            end
                            2'b10: begin
                                WB_rd_write_data = {24'b0, WB_read_MEM_maneger_data[23:16]};
                            end
                            default: begin
                                WB_rd_write_data = {24'b0, WB_read_MEM_maneger_data[31:24]};
                            end
                        endcase
                    end
                    3'b101:begin    //lhu
                        case (WB_write_read_MEM_manger_addr[1:0])
                            2'b00: begin
                                WB_rd_write_data = {16'b0, WB_read_MEM_maneger_data[15:0]};
                            end
                            default: begin
                                WB_rd_write_data = {16'b0, WB_read_MEM_maneger_data[31:16]};
                            end
                        endcase
                    end
                    default:begin
                        WB_rd_write_data = 0;
                    end
                endcase
            end

            default:begin
                WB_rd_write_data = WB_result;
            end
        endcase
    end

    reg [31:0] WB_write_MEM_manger_data;
    always @(*) begin
        case (WB_opcode)
            7'b0100011:begin
                case (WB_funct3)
                    3'b000:begin    //sb
                        case (WB_write_read_MEM_manger_addr[1:0])
                            2'b00:begin
                                WB_write_MEM_manger_data = {WB_read_MEM_maneger_data[31:8],WB_rs2_data[7:0]};
                            end
                            2'b01: begin
                                WB_write_MEM_manger_data = {WB_read_MEM_maneger_data[31:16], WB_rs2_data[7:0], WB_read_MEM_maneger_data[7:0]};
                            end
                            2'b10: begin
                                WB_write_MEM_manger_data = {WB_read_MEM_maneger_data[31:24], WB_rs2_data[7:0], WB_read_MEM_maneger_data[15:0]};
                            end
                            default: begin
                                WB_write_MEM_manger_data = {WB_rs2_data[7:0], WB_read_MEM_maneger_data[23:0]};
                            end
                        endcase
                    end
                    3'b001:begin    //sh
                        case (WB_write_read_MEM_manger_addr[1:0])
                            2'b00:begin
                                WB_write_MEM_manger_data = {WB_read_MEM_maneger_data[31:16],WB_rs2_data[15:0]};
                            end
                            default: begin
                                WB_write_MEM_manger_data = {WB_rs2_data[15:0], WB_read_MEM_maneger_data[15:0]};
                            end
                        endcase
                    end
                    3'b010:begin    //sw
                        WB_write_MEM_manger_data = WB_rs2_data;
                    end
                    default:begin
                        WB_write_MEM_manger_data = 0;
                    end
                endcase
            end
            default:begin
                WB_write_MEM_manger_data = 0;
            end
        endcase
    end






/******************************  ctrl **********************************/

    reg ctrl_EX_jump_RST_pipline;
    always @(*) begin
        if(EX_jump_req)
            ctrl_EX_jump_RST_pipline = 1;
        else
            ctrl_EX_jump_RST_pipline = 0;
    end

    reg ctrl_rs_wait_rd_hold;
    always @(*) begin
        if (ID_rs1==EX_rd && EX_rd_write_req && EX_rd!=0) begin
            ctrl_rs_wait_rd_hold = 1;
        end
        else if (ID_rs2==EX_rd && EX_rd_write_req && EX_rd!=0) begin
            ctrl_rs_wait_rd_hold = 1;
        end
        else if (ID_rs1==MEM_rd && MEM_rd_write_req && MEM_rd!=0) begin
            ctrl_rs_wait_rd_hold = 1;
        end
        else if (ID_rs2==MEM_rd && MEM_rd_write_req && MEM_rd!=0) begin
            ctrl_rs_wait_rd_hold = 1;
        end
        else begin
            ctrl_rs_wait_rd_hold = 0;
        end
    end

    //when MEM read ITCM IR read will be wait
    wire ctrl_ITCM_read_IR_busy=MEM_manger_read_ITCM_req;

    //when some time pipline need a none IR
    reg ctrl_add_NOP_IR_to_ID;
    always @(posedge clk or negedge rstn) begin
        if(~rstn)
            ctrl_add_NOP_IR_to_ID<=0;
        else
            ctrl_add_NOP_IR_to_ID<=EX_jump_req || ctrl_ITCM_read_IR_busy;
    end

/******************************** MEM manger ***************************/
    //write request from WB write bus request
    wire MEM_manger_write_ITCM_req = (WB_write_read_MEM_manger_addr[31:28]==4'h0)?WB_write_MEM_manger_req:0;
    wire MEM_manger_write_DTCM_req = (WB_write_read_MEM_manger_addr[31:28]==4'h1)?WB_write_MEM_manger_req:0;
    wire core_write_bus_req_o = (WB_write_read_MEM_manger_addr[31:28]>=4'h2)?WB_write_MEM_manger_req:0;

    //write addr
    wire [31:0] MEM_manger_write_ITCM_addr={4'b0,WB_write_read_MEM_manger_addr[27:0]};
    wire [31:0] MEM_manger_write_DTCM_addr={4'b0,WB_write_read_MEM_manger_addr[27:0]};
    wire [31:0] core_write_bus_addr_o={4'b0,WB_write_read_MEM_manger_addr[27:0]};

    //write data
    wire [31:0] MEM_manger_write_ITCM_data=WB_write_MEM_manger_data;
    wire [31:0] MEM_manger_write_DTCM_data=WB_write_MEM_manger_data;
    wire [31:0] core_write_bus_data_o=WB_write_MEM_manger_data;

    //write busy
    wire MEM_write_busy = (WB_write_read_MEM_manger_addr[31:28]>=4'h2) ? core_write_bus_busy_i : 0;



    //read request from MEM read bus request
    wire MEM_manger_read_ITCM_req = (MEM_write_read_MEM_manger_addr[31:28]==4'h0)?MEM_read_MEM_manger_req:0;
    wire MEM_manger_read_DTCM_req = (MEM_write_read_MEM_manger_addr[31:28]==4'h1)?MEM_read_MEM_manger_req:0;
    wire core_read_bus_req_o = (MEM_write_read_MEM_manger_addr[31:28]>=4'h2)?MEM_read_MEM_manger_req:0;

    //read addr
    wire [31:0] MEM_manger_read_ITCM_addr={4'b0,MEM_write_read_MEM_manger_addr[27:0]};
    wire [31:0] MEM_manger_read_DTCM_addr={4'b0,MEM_write_read_MEM_manger_addr[27:0]};
    wire [31:0] core_read_bus_addr_o={4'b0,MEM_write_read_MEM_manger_addr[27:0]};

    //read data
    always @(*) begin
        case(WB_write_read_MEM_manger_addr[31:28])
            4'h0:begin
                WB_read_MEM_maneger_data = ITCM_read_data;
            end
            4'h1:begin
                WB_read_MEM_maneger_data = DTCM_read_data;
            end
            default:begin
                WB_read_MEM_maneger_data = core_read_bus_data_i;
            end
        endcase
    end

    //read busy
    wire MEM_read_busy = (MEM_write_read_MEM_manger_addr[31:28]>=4'h2) ? core_read_bus_busy_i : 0;

/******************************  regfiles ******************************/

    reg [31:0] regfiles [1:31];

    //regs read
    reg [31:0] EX_read_rs1_data_temp;
    always @(posedge clk or negedge rstn) begin
        if (~rstn) begin
            EX_read_rs1_data<=0;
        end
        else if((ID_rs1==WB_rd) && WB_rd_write_req && ID_rs1!=0) begin
            EX_read_rs1_data <= WB_rd_write_data;
        end
        else begin
            EX_read_rs1_data <= ID_rs1==0 ? 0 : regfiles[ID_rs1];
        end
    end
    reg [31:0] EX_read_rs2_data_temp;
    always @(posedge clk or negedge rstn) begin
        if (~rstn) begin
            EX_read_rs2_data<=0;
        end
        else if((ID_rs2==WB_rd) && WB_rd_write_req && ID_rs2!=0) begin
            EX_read_rs2_data <= WB_rd_write_data;
        end
        else begin
            EX_read_rs2_data <= ID_rs2==0 ? 0 : regfiles[ID_rs2];
        end
    end


    //regs write
    always @(posedge clk) begin
        if (WB_rd_write_req && WB_rd!= 0 && rstn) begin
            regfiles[WB_rd] <= WB_rd_write_data;
        end
        else begin
        end
    end

/******************************* CSRs *********************************/
    
    reg [31:0] mie,mscratch,mcause,mstatus,mtvec,mepc;
    reg [63:0] cycle;
    //parameter [31:0] mvendorid = 0,marchid = 0,mimpid = 0,mhartid = 0;//chip infomation
    
    
    always@(posedge clk,negedge rstn)begin
        if(~rstn)begin
            cycle <= 0;
        end
        else begin
            cycle <= cycle+1; //cycle add self
        end
    end
    
    //CSRs write
    always @(posedge clk,negedge rstn) begin
        if (~rstn) begin
            mstatus   <= 0;
            mie       <= 0;
            mtvec     <= 0;
            mscratch  <= 0;
            mepc      <= 0;
            mcause    <= 0;
        end
        else begin
            if (EX_CSR_write_req) begin
                case (EX_CSR_read_write_addr)
                    12'h300:begin
                        mstatus <= EX_write_CSR_data;
                    end
                    12'h304:begin
                        mie <= EX_write_CSR_data;
                    end
                    12'h305:begin
                        mtvec <= EX_write_CSR_data;
                    end
                    12'h340:begin
                        mscratch <= EX_write_CSR_data;
                    end
                    12'h341:begin
                        mepc <= EX_write_CSR_data;
                    end
                    12'h342:begin
                        mcause <= EX_write_CSR_data;
                    end
                    default:begin
                        
                    end
                endcase
            end
            else begin
                
            end
        end
    end
    
    //CSRs read
    always @(posedge clk) begin
        if (ID_CSR_read_write_addr==EX_CSR_read_write_addr && EX_CSR_write_req)begin
            EX_read_CSR_data<=EX_write_CSR_data;
        end
        else begin
            case (ID_CSR_read_write_addr)
                12'h300:begin
                    EX_read_CSR_data <= mstatus;
                end
                12'h304:begin
                    EX_read_CSR_data <= mie;
                end
                12'h305:begin
                    EX_read_CSR_data <= mtvec;
                end
                12'h340:begin
                    EX_read_CSR_data <= mscratch;
                end
                12'h341:begin
                    EX_read_CSR_data <= mepc;
                end
                12'h342:begin
                    EX_read_CSR_data <= mcause;
                end
                12'hc00:begin
                    EX_read_CSR_data <= cycle[31:0];
                end
                12'hc80:begin
                    EX_read_CSR_data <= cycle[63:32];
                end
                default:begin
                    EX_read_CSR_data <= 0;
                end
            endcase
        end
    end

/******************************   DTCM *********************************/

    //read than write
    wire [31:0] DTCM_read_data = ((MEM_manger_read_DTCM_addr_temp[31:2]==MEM_manger_write_DTCM_addr_temp[31:2]) && MEM_manger_write_DTCM_req_temp) ? MEM_manger_write_DTCM_data_temp : DTCM_read_data_temp ;
    reg [31:0] MEM_manger_write_DTCM_data_temp, MEM_manger_read_DTCM_addr_temp, MEM_manger_write_DTCM_addr_temp;
    reg MEM_manger_write_DTCM_req_temp;
    always @(posedge clk ) begin
        if (~rstn) begin
            MEM_manger_write_DTCM_data_temp<=0;
            MEM_manger_read_DTCM_addr_temp<=0;
            MEM_manger_write_DTCM_addr_temp<=0;
            MEM_manger_write_DTCM_req_temp<=0;
        end else begin
            MEM_manger_write_DTCM_data_temp <= MEM_manger_write_DTCM_data;
            MEM_manger_read_DTCM_addr_temp<=MEM_manger_read_DTCM_addr;
            MEM_manger_write_DTCM_addr_temp<=MEM_manger_write_DTCM_addr;
            MEM_manger_write_DTCM_req_temp<=MEM_manger_write_DTCM_req;
        end
    end


    reg [31:0] DTCM [0:4095];
    //read and data
    reg [31:0] DTCM_read_data_temp;
    always @(posedge clk or negedge rstn) begin
        if (~rstn) begin
            DTCM_read_data_temp <= 0;
        end 
        else begin
            DTCM_read_data_temp <= DTCM[MEM_manger_read_DTCM_addr[31:2]];
        end
    end

    //write data
    always @(posedge clk) begin
        if (MEM_manger_write_DTCM_req&&rstn) begin
            DTCM[MEM_manger_write_DTCM_addr[31:2]] <= MEM_manger_write_DTCM_data;
        end
        else begin
            
        end
    end

/*******************************   ITCM   *********************************/
    
    
    wire [31:0] ITCM_read_addr = MEM_manger_read_ITCM_req ? MEM_manger_read_ITCM_addr : IF_PC;

    //read than write
    wire [31:0] ITCM_read_data = ((ITCM_read_addr_temp[31:2]==MEM_manger_write_ITCM_addr_temp[31:2]) && MEM_manger_write_ITCM_req_temp) ? MEM_manger_write_ITCM_data_temp : ITCM_read_data_temp ; 
    reg [31:0] MEM_manger_write_ITCM_data_temp, ITCM_read_addr_temp, MEM_manger_write_ITCM_addr_temp;
    reg MEM_manger_write_ITCM_req_temp;
    always @(posedge clk ) begin
        if (~rstn) begin
            MEM_manger_write_ITCM_data_temp<=0;
            ITCM_read_addr_temp<=0;
            MEM_manger_write_ITCM_addr_temp<=0;
            MEM_manger_write_ITCM_req_temp<=0;
        end else begin
            MEM_manger_write_ITCM_data_temp <= MEM_manger_write_ITCM_data;
            ITCM_read_addr_temp<=ITCM_read_addr;
            MEM_manger_write_ITCM_addr_temp<=MEM_manger_write_ITCM_addr;
            MEM_manger_write_ITCM_req_temp<=MEM_manger_write_ITCM_req;
        end
    end


    reg [31:0] ITCM [0:8191];
    //read data
    reg [31:0] ITCM_read_data_temp;
    always@(posedge clk or negedge rstn) begin
        if(~rstn) begin
            ITCM_read_data_temp <= 0;
        end
        else begin
            ITCM_read_data_temp <= ITCM[ITCM_read_addr[31:2]];
        end
    end
    //write data
    always @(posedge clk) begin
        if (MEM_manger_write_ITCM_req&&rstn) begin
            ITCM[MEM_manger_write_ITCM_addr[31:2]] <= MEM_manger_write_ITCM_data;
        end
        else begin
    
        end
    end

endmodule
