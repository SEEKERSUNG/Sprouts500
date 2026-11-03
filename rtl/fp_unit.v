// Sprouts500 - RV32F floating-point unit (staged implementation).
//
// The arithmetic is split across pipeline stages so no single combinational
// cone carries an entire IEEE operation:
//   A: decode, unpack and normalise the operands
//   B: special cases and the arithmetic core (align+add, multiply, convert)
//   C: normalise / round-to-int for FMA and divide/square-root
//   R: round and pack (rp_f32)
// The core freezes the EX instruction while busy and takes the result when
// done is asserted.  Everything is Verilog-2001.

module fp_unit (
    input  wire        clk,
    input  wire        rstn,
    input  wire [31:0] insn,
    input  wire [2:0]  rm,
    input  wire [31:0] a, b, c,
    input  wire        start,
    output wire        busy,
    output wire        done,
    output wire [31:0] result,
    output wire [4:0]  fflags
);

    localparam [31:0] CANON_NAN = 32'h7fc0_0000;
    localparam [4:0]  F_NX = 5'b00001;
    localparam [4:0]  F_UF = 5'b00010;
    localparam [4:0]  F_OF = 5'b00100;
    localparam [4:0]  F_DZ = 5'b01000;
    localparam [4:0]  F_NV = 5'b10000;

    localparam [4:0] FOP_ADD  = 5'd0,  FOP_SUB  = 5'd1,  FOP_MUL  = 5'd2,
                     FOP_DIV  = 5'd3,  FOP_SQRT = 5'd4,  FOP_SGNJ = 5'd5,
                     FOP_SGNJN= 5'd6,  FOP_SGNJX= 5'd7,  FOP_MIN  = 5'd8,
                     FOP_MAX  = 5'd9,  FOP_EQ   = 5'd10, FOP_LT   = 5'd11,
                     FOP_LE   = 5'd12, FOP_W_S  = 5'd13, FOP_WU_S = 5'd14,
                     FOP_S_W  = 5'd15, FOP_S_WU = 5'd16, FOP_MVX  = 5'd17,
                     FOP_MVWX = 5'd18, FOP_CLASS= 5'd19, FOP_MADD = 5'd20,
                     FOP_MSUB = 5'd21, FOP_NMSUB= 5'd22, FOP_NMADD= 5'd23;

    localparam [2:0] S_IDLE = 3'd0, S_B = 3'd1, S_FMA = 3'd2,
                     S_ITER = 3'd3, S_ROUND = 3'd4, S_DONE = 3'd5,
                     S_ITERF = 3'd6;

    //--------------------------------------------------------------------------
    // instruction decode
    //--------------------------------------------------------------------------
    reg [4:0] fop;
    always @(*) begin
        fop = 5'd31;
        case (insn[6:0])
            7'b1010011: begin
              case (insn[31:25])
                7'b0000000: fop = FOP_ADD;
                7'b0000100: fop = FOP_SUB;
                7'b0001000: fop = FOP_MUL;
                7'b0001100: fop = FOP_DIV;
                7'b0101100: fop = FOP_SQRT;
                7'b0010000: fop = (insn[14:12] == 3'b000) ? FOP_SGNJ  :
                                  (insn[14:12] == 3'b001) ? FOP_SGNJN :
                                  (insn[14:12] == 3'b010) ? FOP_SGNJX : 5'd31;
                7'b0010100: fop = (insn[14:12] == 3'b000) ? FOP_MIN :
                                  (insn[14:12] == 3'b001) ? FOP_MAX : 5'd31;
                7'b1010000: fop = (insn[14:12] == 3'b000) ? FOP_LE :
                                  (insn[14:12] == 3'b001) ? FOP_LT :
                                  (insn[14:12] == 3'b010) ? FOP_EQ : 5'd31;
                7'b1100000: fop = insn[20] ? FOP_WU_S : FOP_W_S;
                7'b1101000: fop = insn[20] ? FOP_S_WU : FOP_S_W;
                7'b1110000: fop = (insn[14:12] == 3'b000) ? FOP_MVX :
                                  (insn[14:12] == 3'b001) ? FOP_CLASS : 5'd31;
                7'b1111000: fop = FOP_MVWX;
                default:    fop = 5'd31;
              endcase
            end
            7'b1000011: fop = FOP_MADD;
            7'b1000111: fop = FOP_MSUB;
            7'b1001011: fop = FOP_NMSUB;
            7'b1001111: fop = FOP_NMADD;
            default:    fop = 5'd31;
        endcase
    end

    wire [2:0] rm_imm = insn[14:12];
    wire [2:0] rm_eff = (rm_imm == 3'b111) ? rm : rm_imm;

    //--------------------------------------------------------------------------
    // helpers
    //--------------------------------------------------------------------------
    function is_nan32; input [31:0] x;
        begin is_nan32 = (x[30:23] == 8'hff) && (x[22:0] != 23'd0); end
    endfunction
    function is_snan32; input [31:0] x;
        begin is_snan32 = (x[30:23] == 8'hff) && (x[22:0] != 23'd0) && !x[22]; end
    endfunction
    function is_inf32; input [31:0] x;
        begin is_inf32 = (x[30:23] == 8'hff) && (x[22:0] == 23'd0); end
    endfunction
    function is_zero32; input [31:0] x;
        begin is_zero32 = (x[30:23] == 8'd0) && (x[22:0] == 23'd0); end
    endfunction

    function [5:0] clz32;
        input [31:0] x;
        integer i; reg found;
        begin
            clz32 = 6'd32; found = 1'b0;
            for (i = 31; i >= 0; i = i - 1)
                if (!found && x[i]) begin clz32 = 31 - i; found = 1'b1; end
        end
    endfunction
    function [5:0] clz24; input [23:0] x;
        begin clz24 = clz32({8'b0, x}) - 6'd8; end
    endfunction
    function [6:0] clz64;
        input [63:0] x;
        integer i; reg found;
        begin
            clz64 = 7'd64; found = 1'b0;
            for (i = 63; i >= 0; i = i - 1)
                if (!found && x[i]) begin clz64 = 63 - i; found = 1'b1; end
        end
    endfunction
    function [31:0] srj32;
        input [31:0] x; input integer sh;
        begin
            if (sh <= 0) srj32 = x;
            else if (sh >= 32) srj32 = {31'b0, (x != 32'd0)};
            else srj32 = (x >> sh) |
                ((|(x & ((32'd1 << sh) - 32'd1))) ? 32'd1 : 32'd0);
        end
    endfunction
    function [63:0] srj64;
        input [63:0] x; input integer sh;
        begin
            if (sh <= 0) srj64 = x;
            else if (sh >= 64) srj64 = {63'b0, (x != 64'd0)};
            else srj64 = (x >> sh) |
                ((|(x & ((64'd1 << sh) - 64'd1))) ? 64'd1 : 64'd0);
        end
    endfunction

    function [36:0] rp_f32;
        input [2:0] rmode; input sign;
        input signed [15:0] e; input [31:0] sig; input sticky_in;
        reg [15:0] ee;
        reg [31:0] shifted, lostmask;
        reg [23:0] mm;
        reg g, r, s, inexact, inc;
        reg [24:0] msum;
        reg [31:0] rz;
        reg [4:0] fl;
        integer k;
        begin
            fl = 5'b0; rz = {sign, 31'b0}; ee = e;
            if (!sig && !sticky_in) begin
                rp_f32 = {fl, rz};
            end else if (e > 0) begin
                mm = sig[31:8]; g = sig[7]; r = sig[6];
                s = (|sig[5:0]) | sticky_in;
                inexact = g | r | s;
                inc = (rmode == 3'b000) ? (g & (r | s | mm[0])) :
                      (rmode == 3'b100) ? g :
                      (rmode == 3'b001) ? 1'b0 :
                      (rmode == 3'b010) ? (sign & (g | r | s)) :
                      (rmode == 3'b011) ? (~sign & (g | r | s)) : 1'b0;
                msum = {1'b0, mm} + inc;
                if (msum[24]) begin mm = 24'h80_0000; ee = ee + 16'd1; end
                else mm = msum[23:0];
                if (ee >= 16'd255) begin
                    fl = F_OF | F_NX;
                    if (rmode == 3'b001)
                        rz = {sign, 8'hfe, 23'h7f_ffff};
                    else if (rmode == 3'b010)
                        rz = sign ? {1'b1, 8'hff, 23'd0} :
                                    {1'b0, 8'hfe, 23'h7f_ffff};
                    else if (rmode == 3'b011)
                        rz = sign ? {1'b1, 8'hfe, 23'h7f_ffff} :
                                    {1'b0, 8'hff, 23'd0};
                    else
                        rz = {sign, 8'hff, 23'd0};
                end else if (mm >= 24'h80_0000) begin
                    rz = {sign, ee[7:0], mm[22:0]};
                    fl = inexact ? F_NX : 5'b0;
                end else begin
                    rz = {sign, 8'h00, mm[22:0]};
                    fl = inexact ? (F_NX | F_UF) : 5'b0;
                end
            end else begin
                k = 1 - e;
                if (k > 31) begin shifted = 32'd0; lostmask = 32'hffff_ffff; end
                else begin shifted = sig >> k; lostmask = (32'd1 << k) - 32'd1; end
                mm = shifted[31:8]; g = shifted[7]; r = shifted[6];
                s = (|shifted[5:0]) | (|(sig & lostmask)) | sticky_in;
                inexact = g | r | s;
                inc = (rmode == 3'b000) ? (g & (r | s | mm[0])) :
                      (rmode == 3'b100) ? g :
                      (rmode == 3'b001) ? 1'b0 :
                      (rmode == 3'b010) ? (sign & (g | r | s)) :
                      (rmode == 3'b011) ? (~sign & (g | r | s)) : 1'b0;
                msum = {1'b0, mm} + inc;
                mm = msum[23:0];
                if (mm >= 24'h80_0000) rz = {sign, 8'h01, 23'd0};
                else rz = {sign, 8'h00, mm[22:0]};
                fl = inexact ? F_NX : 5'b0;
                if (inexact && (mm < 24'h80_0000)) fl = fl | F_UF;
            end
            rp_f32 = {fl, rz};
        end
    endfunction

    //--------------------------------------------------------------------------
    // stage A: unpack and normalise the operands
    //--------------------------------------------------------------------------
    reg [4:0]  op_q;
    reg [2:0]  rm_q;
    reg [31:0] ra, rb, rc;
    reg [23:0] xm, ym, zm;
    reg signed [15:0] xe, ye, ze;
    reg xs, ys, zs;
    reg [23:0] axm, aym, azm;
    reg signed [15:0] axe, aye, aze;
    reg axs, ays, azs;
    reg [5:0]  alz;
    always @(*) begin
        axm = (a[30:23] == 8'd0) ? {1'b0, a[22:0]} : {1'b1, a[22:0]};
        axe = (a[30:23] == 8'd0) ? 16'sd1 : $signed({8'd0, a[30:23]});
        alz = clz24(axm); axm = axm << alz; axe = axe - alz;
        axs = a[31];
        aym = (b[30:23] == 8'd0) ? {1'b0, b[22:0]} : {1'b1, b[22:0]};
        aye = (b[30:23] == 8'd0) ? 16'sd1 : $signed({8'd0, b[30:23]});
        alz = clz24(aym); aym = aym << alz; aye = aye - alz;
        ays = b[31];
        azm = (c[30:23] == 8'd0) ? {1'b0, c[22:0]} : {1'b1, c[22:0]};
        aze = (c[30:23] == 8'd0) ? 16'sd1 : $signed({8'd0, c[30:23]});
        alz = clz24(azm); azm = azm << alz; aze = aze - alz;
        azs = c[31];
    end

    //--------------------------------------------------------------------------
    // stage B: arithmetic core
    //--------------------------------------------------------------------------
    reg        pre_valid;
    reg        pre_sign;
    reg signed [15:0] pre_e;
    reg [31:0] pre_sig;
    reg        pre_sticky;

    reg        dir_valid;
    reg [31:0] dir_result;
    reg [4:0]  dir_flags;

    reg        fma_go;
    reg [63:0] fma_p64, fma_c64;
    reg signed [15:0] fma_base;
    reg        fma_sp, fma_sc;

    reg        iter_go;
    reg        iter_sqrt;

    reg [31:0] yy, sx_v, sy_v;
    reg sxa, sya, sbig;
    reg [31:0] mag32, sbg, ssm;
    reg [32:0] sum33;
    reg signed [15:0] er;
    reg [5:0]  lz6;
    reg [47:0] prod;
    reg [5:0]  lp;
    reg signed [15:0] leadp, leadc, dd;
    reg [63:0] p64, c64, mag64;
    reg [64:0] sum65;
    reg [9:0]  clsb;
    reg [31:0] tz;
    reg        eqv, ltv;
    reg [31:0] rz;
    reg [4:0]  flz;
    reg [63:0] cvt64;
    reg [11:0] rb12;
    reg [63:0] cinc;
    reg [31:0] zint;
    reg        cinv;
    integer    sh_i;

    always @(*) begin
        pre_valid = 1'b0; pre_sign = 1'b0; pre_e = 16'sd0;
        pre_sig = 32'd0; pre_sticky = 1'b0;
        dir_valid = 1'b0; dir_result = 32'd0; dir_flags = 5'b0;
        fma_go = 1'b0; fma_p64 = 64'd0; fma_c64 = 64'd0;
        fma_base = 16'sd0; fma_sp = 1'b0; fma_sc = 1'b0;
        iter_go = 1'b0; iter_sqrt = 1'b0;
        // Scratch values are consumed only on the paths that assign them, but
        // a default keeps this combinational block latch-free for synthesis.
        yy = 32'd0; sx_v = 32'd0; sy_v = 32'd0;
        sxa = 1'b0; sya = 1'b0; sbig = 1'b0;
        mag32 = 32'd0; sbg = 32'd0; ssm = 32'd0; sum33 = 33'd0;
        er = 16'sd0; lz6 = 6'd0; prod = 48'd0; lp = 6'd0;
        leadp = 16'sd0; leadc = 16'sd0; dd = 16'sd0;
        p64 = 64'd0; c64 = 64'd0; mag64 = 64'd0; sum65 = 65'd0;
        clsb = 10'd0; tz = 32'd0; eqv = 1'b0; ltv = 1'b0;
        rz = 32'd0; flz = 5'd0; cvt64 = 64'd0; rb12 = 12'd0;
        cinc = 64'd0; zint = 32'd0; cinv = 1'b0; sh_i = 0;

        case (op_q)
            FOP_ADD, FOP_SUB: begin
                yy = (op_q == FOP_SUB) ? {~rb[31], rb[30:0]} : rb;
                sx_v = {xs, ra[30:0]};
                sy_v = {ys ^ (op_q == FOP_SUB), rb[30:0]};
                if (is_nan32(ra) || is_nan32(yy)) begin
                    dir_valid = 1'b1;
                    dir_result = CANON_NAN;
                    dir_flags = (is_snan32(ra) || is_snan32(yy)) ? F_NV : 5'b0;
                end else if (is_inf32(ra) || is_inf32(yy)) begin
                    dir_valid = 1'b1;
                    if (is_inf32(ra) && is_inf32(yy) && (sx_v[31] != sy_v[31])) begin
                        dir_result = CANON_NAN; dir_flags = F_NV;
                    end else if (is_inf32(ra)) begin
                        dir_result = ra;
                    end else begin
                        dir_result = sy_v;
                    end
                end else if (is_zero32(ra) && is_zero32(yy)) begin
                    dir_valid = 1'b1;
                    dir_result = {(sx_v[31] == sy_v[31]) ? sx_v[31] :
                                  (rm_q == 3'b010), 31'b0};
                end else if (is_zero32(ra)) begin
                    dir_valid = 1'b1; dir_result = sy_v;
                end else if (is_zero32(yy)) begin
                    dir_valid = 1'b1; dir_result = ra;
                end else begin
                    // Both operands are normalised at this point.
                    sxa = xs;
                    sya = ys ^ (op_q == FOP_SUB);
                    if ((xe < ye) || ((xe == ye) && (xm < ym))) begin
                        sbg = {ym, 8'b0}; ssm = {xm, 8'b0};
                        er = ye; dd = ye - xe; sbig = sya;
                    end else begin
                        sbg = {xm, 8'b0}; ssm = {ym, 8'b0};
                        er = xe; dd = xe - ye; sbig = sxa;
                    end
                    if (dd >= 16'sd32) ssm = 32'd1;
                    else if (dd > 0) ssm = srj32(ssm, dd);
                    if (sxa == sya) begin
                        sum33 = {1'b0, sbg} + {1'b0, ssm};
                        if (sum33[32]) begin
                            mag32 = sum33[32:1] | {31'b0, sum33[0]};
                            er = er + 16'd1;
                        end else mag32 = sum33[31:0];
                        pre_sign = sxa;
                    end else begin
                        mag32 = sbg - ssm;
                        pre_sign = sbig;
                        if (mag32 == 32'd0) begin
                            dir_valid = 1'b1;
                            dir_result = {rm_q == 3'b010, 31'b0};
                        end
                    end
                    if (!dir_valid) begin
                        if (mag32 != 32'd0) begin
                            lz6 = clz32(mag32);
                            mag32 = mag32 << lz6;
                            er = er - lz6;
                        end
                        pre_valid = 1'b1; pre_e = er; pre_sig = mag32;
                    end
                end
            end
            FOP_MUL: begin
                if (is_nan32(ra) || is_nan32(rb)) begin
                    dir_valid = 1'b1; dir_result = CANON_NAN;
                    dir_flags = (is_snan32(ra) || is_snan32(rb)) ? F_NV : 5'b0;
                end else if ((is_inf32(ra) && is_zero32(rb)) ||
                             (is_zero32(ra) && is_inf32(rb))) begin
                    dir_valid = 1'b1; dir_result = CANON_NAN; dir_flags = F_NV;
                end else if (is_inf32(ra) || is_inf32(rb)) begin
                    dir_valid = 1'b1; dir_result = {xs ^ ys, 8'hff, 23'd0};
                end else if (is_zero32(ra) || is_zero32(rb)) begin
                    dir_valid = 1'b1; dir_result = {xs ^ ys, 31'b0};
                end else begin
                    prod = xm * ym;
                    lp = prod[47] ? 6'd47 : 6'd46;
                    sh_i = lp - 31;
                    pre_valid = 1'b1;
                    pre_sign = xs ^ ys;
                    pre_e = xe + ye + lp - 16'sd173;
                    pre_sig = prod >> sh_i;
                    pre_sticky = |(prod & ((48'd1 << sh_i) - 48'd1));
                end
            end
            FOP_MADD, FOP_MSUB, FOP_NMSUB, FOP_NMADD: begin
                fma_sp = xs ^ ys;
                fma_sc = zs;
                if (op_q == FOP_MSUB || op_q == FOP_NMADD) fma_sc = ~fma_sc;
                if (op_q == FOP_NMSUB || op_q == FOP_NMADD) fma_sp = ~fma_sp;
                if (is_nan32(ra) || is_nan32(rb) || is_nan32(rc)) begin
                    dir_valid = 1'b1; dir_result = CANON_NAN;
                    dir_flags = (is_snan32(ra) || is_snan32(rb) || is_snan32(rc) ||
                                 (is_zero32(ra) && is_inf32(rb)) ||
                                 (is_inf32(ra) && is_zero32(rb))) ? F_NV : 5'b0;
                end else if ((is_inf32(ra) && is_zero32(rb)) ||
                             (is_zero32(ra) && is_inf32(rb))) begin
                    dir_valid = 1'b1; dir_result = CANON_NAN; dir_flags = F_NV;
                end else if (is_inf32(ra) || is_inf32(rb)) begin
                    dir_valid = 1'b1;
                    if (is_inf32(rc) && (fma_sc != fma_sp)) begin
                        dir_result = CANON_NAN; dir_flags = F_NV;
                    end else dir_result = {fma_sp, 8'hff, 23'd0};
                end else if (is_inf32(rc)) begin
                    dir_valid = 1'b1; dir_result = {fma_sc, rc[30:0]};
                end else if (is_zero32(ra) || is_zero32(rb)) begin
                    dir_valid = 1'b1;
                    if (is_zero32(rc))
                        dir_result = {(fma_sp == fma_sc) ? fma_sp :
                                      (rm_q == 3'b010), 31'b0};
                    else
                        dir_result = {fma_sc, rc[30:0]};
                end else if (is_zero32(rc)) begin
                    prod = xm * ym;
                    lp = prod[47] ? 6'd47 : 6'd46;
                    sh_i = lp - 31;
                    pre_valid = 1'b1; pre_sign = fma_sp;
                    pre_e = xe + ye + lp - 16'sd173;
                    pre_sig = prod >> sh_i;
                    pre_sticky = |(prod & ((48'd1 << sh_i) - 48'd1));
                end else begin
                    prod = xm * ym;
                    lp = prod[47] ? 6'd47 : 6'd46;
                    leadp = xe + ye - 16'sd300 + lp;
                    leadc = ze - 16'sd127;
                    dd = leadp - leadc;
                    if (dd >= 0) begin
                        p64 = {16'b0, prod} << (63 - lp);
                        if (dd <= 16'sd40)
                            c64 = {40'b0, zm} << (40 - dd[5:0]);
                        else
                            c64 = srj64({40'b0, zm}, dd - 40);
                        fma_base = leadp;
                    end else begin
                        c64 = {40'b0, zm} << 40;
                        p64 = srj64({16'b0, prod} << (63 - lp), -dd);
                        fma_base = leadc;
                    end
                    fma_go = 1'b1;
                    fma_p64 = p64; fma_c64 = c64;
                end
            end
            FOP_DIV: begin
                if (is_nan32(ra) || is_nan32(rb)) begin
                    dir_valid = 1'b1; dir_result = CANON_NAN;
                    dir_flags = (is_snan32(ra) || is_snan32(rb)) ? F_NV : 5'b0;
                end else if ((is_zero32(ra) && is_zero32(rb)) ||
                             (is_inf32(ra) && is_inf32(rb))) begin
                    dir_valid = 1'b1; dir_result = CANON_NAN; dir_flags = F_NV;
                end else if (is_inf32(ra)) begin
                    dir_valid = 1'b1; dir_result = {xs ^ ys, 8'hff, 23'd0};
                end else if (is_inf32(rb)) begin
                    dir_valid = 1'b1; dir_result = {xs ^ ys, 31'b0};
                end else if (is_zero32(rb)) begin
                    dir_valid = 1'b1; dir_result = {xs ^ ys, 8'hff, 23'd0};
                    dir_flags = F_DZ;
                end else if (is_zero32(ra)) begin
                    dir_valid = 1'b1; dir_result = {xs ^ ys, 31'b0};
                end else begin
                    iter_go = 1'b1; iter_sqrt = 1'b0;
                end
            end
            FOP_SQRT: begin
                if (is_nan32(ra)) begin
                    dir_valid = 1'b1; dir_result = CANON_NAN;
                    dir_flags = is_snan32(ra) ? F_NV : 5'b0;
                end else if (is_inf32(ra)) begin
                    dir_valid = 1'b1;
                    if (xs) begin dir_result = CANON_NAN; dir_flags = F_NV; end
                    else dir_result = ra;
                end else if (is_zero32(ra)) begin
                    dir_valid = 1'b1; dir_result = ra;
                end else if (xs) begin
                    dir_valid = 1'b1; dir_result = CANON_NAN; dir_flags = F_NV;
                end else begin
                    iter_go = 1'b1; iter_sqrt = 1'b1;
                end
            end
            FOP_SGNJ, FOP_SGNJN, FOP_SGNJX: begin
                dir_valid = 1'b1;
                rz = (op_q == FOP_SGNJ)  ? {rb[31], ra[30:0]} :
                     (op_q == FOP_SGNJN) ? {~rb[31], ra[30:0]} :
                                           {ra[31] ^ rb[31], ra[30:0]};
                dir_result = rz;
            end
            FOP_MIN, FOP_MAX: begin
                dir_valid = 1'b1;
                if (is_nan32(ra) && is_nan32(rb)) begin
                    dir_result = CANON_NAN;
                    dir_flags = (is_snan32(ra) || is_snan32(rb)) ? F_NV : 5'b0;
                end else if (is_nan32(ra)) begin
                    dir_result = rb;
                    dir_flags = is_snan32(ra) ? F_NV : 5'b0;
                end else if (is_nan32(rb)) begin
                    dir_result = ra;
                    dir_flags = is_snan32(rb) ? F_NV : 5'b0;
                end else if (is_zero32(ra) && is_zero32(rb)) begin
                    dir_result = {(op_q == FOP_MIN) ? (ra[31] | rb[31]) :
                                  (ra[31] & rb[31]), 31'b0};
                end else begin
                    if (is_zero32(ra) && is_zero32(rb)) ltv = 1'b0;
                    else if (ra[31] != rb[31]) ltv = ra[31];
                    else if (ra[31] == 1'b0) ltv = (ra < rb);
                    else ltv = (ra > rb);
                    dir_result = (op_q == FOP_MIN) ? (ltv ? ra : rb) :
                                                     (ltv ? rb : ra);
                end
            end
            FOP_EQ, FOP_LT, FOP_LE: begin
                dir_valid = 1'b1;
                if (is_nan32(ra) || is_nan32(rb)) begin
                    dir_result = 32'd0;
                    if ((op_q == FOP_EQ) ?
                        (is_snan32(ra) || is_snan32(rb)) : 1'b1)
                        dir_flags = F_NV;
                end else begin
                    eqv = (ra == rb) || (is_zero32(ra) && is_zero32(rb));
                    if (eqv) ltv = 1'b0;
                    else if (ra[31] != rb[31]) ltv = ra[31];
                    else if (ra[31] == 1'b0) ltv = (ra < rb);
                    else ltv = (ra > rb);
                    rz = (op_q == FOP_EQ) ? {31'b0, eqv} :
                         (op_q == FOP_LT) ? {31'b0, ltv} :
                                            {31'b0, ltv | eqv};
                    dir_result = rz;
                end
            end
            FOP_CLASS: begin
                dir_valid = 1'b1;
                clsb = 10'd0;
                if (is_nan32(ra)) begin
                    if (ra[22]) clsb[9] = 1'b1; else clsb[8] = 1'b1;
                end else if (is_inf32(ra)) clsb[ra[31] ? 0 : 7] = 1'b1;
                else if (ra[30:23] != 8'd0) clsb[ra[31] ? 1 : 6] = 1'b1;
                else if (ra[22:0] != 23'd0) clsb[ra[31] ? 2 : 5] = 1'b1;
                else clsb[ra[31] ? 3 : 4] = 1'b1;
                dir_result = {22'b0, clsb};
            end
            FOP_MVX, FOP_MVWX: begin
                dir_valid = 1'b1; dir_result = ra;
            end
            FOP_W_S, FOP_WU_S: begin
                dir_valid = 1'b1;
                if (is_nan32(ra)) begin
                    dir_flags = F_NV;
                    dir_result = (op_q == FOP_WU_S) ? 32'hffff_ffff : 32'h7fff_ffff;
                end else if (is_inf32(ra)) begin
                    dir_flags = F_NV;
                    if (op_q == FOP_WU_S)
                        dir_result = xs ? 32'h0000_0000 : 32'hffff_ffff;
                    else
                        dir_result = xs ? 32'h8000_0000 : 32'h7fff_ffff;
                end else begin
                    cvt64 = {8'b0, ((ra[30:23] == 8'd0) ? {1'b0, ra[22:0]} :
                                    {1'b1, ra[22:0]}), 32'b0};
                    sh_i = 170 - ra[30:23];
                    if (sh_i > 0) cvt64 = srj64(cvt64, sh_i);
                    rb12 = cvt64[11:0];
                    if ((rm_q == 3'b000) || (rm_q == 3'b100)) cinc = 64'h800;
                    else if (xs ? (rm_q == 3'b010) : (rm_q == 3'b011))
                        cinc = 64'hfff;
                    else cinc = 64'd0;
                    cvt64 = cvt64 + cinc;
                    cinv = |(cvt64 & 64'hffff_f000_0000_0000);
                    zint = cvt64[43:12];
                    if ((rb12 == 12'h800) && (rm_q == 3'b000)) zint = zint & ~32'd1;
                    if (op_q == FOP_WU_S) begin
                        if (xs && (zint != 32'd0)) cinv = 1'b1;
                        if (cinv) begin dir_flags = F_NV;
                            dir_result = xs ? 32'd0 : 32'hffff_ffff; end
                        else begin
                            dir_flags = (rb12 != 12'd0) ? F_NX : 5'b0;
                            dir_result = zint;
                        end
                    end else begin
                        if (!xs && (zint > 32'h7fff_ffff)) cinv = 1'b1;
                        if (xs && (zint > 32'h8000_0000)) cinv = 1'b1;
                        if (cinv) begin dir_flags = F_NV;
                            dir_result = xs ? 32'h8000_0000 : 32'h7fff_ffff; end
                        else begin
                            dir_flags = (rb12 != 12'd0) ? F_NX : 5'b0;
                            dir_result = xs ? (~zint + 32'd1) : zint;
                        end
                    end
                end
            end
            FOP_S_W, FOP_S_WU: begin
                if ((op_q == FOP_S_WU) || !ra[31]) tz = ra;
                else tz = ~ra + 32'd1;
                if (tz == 32'd0) begin
                    dir_valid = 1'b1; dir_result = 32'b0;
                end else begin
                    lz6 = clz32(tz);
                    pre_valid = 1'b1;
                    pre_sign = (op_q == FOP_S_WU) ? 1'b0 : ra[31];
                    pre_e = 16'sd158 - lz6;
                    pre_sig = tz << lz6;
                end
            end
            default: ;
        endcase
    end

    //--------------------------------------------------------------------------
    // stage C: FMA add/normalise, iterative divide/square-root
    //--------------------------------------------------------------------------
    reg [2:0]  state;
    reg        pre_valid_q, pre_sign_q;
    reg signed [15:0] pre_e_q;
    reg [31:0] pre_sig_q;
    reg        pre_sticky_q;

    reg [63:0] fq_p64, fq_c64;
    reg signed [15:0] fq_base;
    reg        fq_sp, fq_sc;

    reg        pre2_valid, pre2_zero, pre2_sign;
    reg signed [15:0] pre2_e;
    reg [31:0] pre2_sig;
    reg        pre2_sticky;
    reg [63:0] m64;
    reg [64:0] s65;
    reg signed [15:0] b2;
    reg        s2;
    reg [6:0]  lzc;
    reg [36:0] rp;

    always @(*) begin
        pre2_valid = 1'b0; pre2_zero = 1'b0; pre2_sign = 1'b0;
        pre2_e = 16'sd0; pre2_sig = 32'd0; pre2_sticky = 1'b0;
        m64 = 64'd0; b2 = fq_base; s2 = 1'b0;
        s65 = 65'd0; lzc = 7'd0;
        if (state == S_FMA) begin
            if (fq_sp == fq_sc) begin
                s65 = {1'b0, fq_p64} + {1'b0, fq_c64};
                if (s65[64]) begin
                    m64 = s65[64:1] | {63'b0, s65[0]};
                    b2 = fq_base + 16'd1;
                end else m64 = s65[63:0];
                s2 = fq_sp;
            end else if (fq_p64 >= fq_c64) begin
                m64 = fq_p64 - fq_c64; s2 = fq_sp;
            end else begin
                m64 = fq_c64 - fq_p64; s2 = fq_sc;
            end
            if (m64 == 64'd0) begin
                pre2_valid = 1'b1; pre2_zero = 1'b1;
            end else begin
                lzc = clz64(m64);
                m64 = m64 << lzc;
                b2 = b2 - lzc;
                pre2_valid = 1'b1;
                pre2_sign = s2;
                pre2_e = b2 + 16'sd127;
                pre2_sig = m64[63:32];
                pre2_sticky = |m64[31:0];
            end
        end
    end

    // iterative engin
    reg [23:0] d_ma, d_mb;
    reg signed [15:0] d_ea, d_eb;
    reg [63:0] d_dividend;
    reg [24:0] d_rem;
    reg [32:0] d_q;
    reg [35:0] s_rem;
    reg [31:0] s_q;
    reg [63:0] s_rad;
    reg signed [15:0] s_k;
    reg [5:0]  seq_cnt;
    reg        iter_sqrt_q;

    wire [24:0] d_rem_sh = {d_rem[23:0], d_dividend[55]};
    wire        d_ge     = (d_rem_sh >= {1'b0, d_mb});
    wire [24:0] d_rem_nx = d_ge ? (d_rem_sh - {1'b0, d_mb}) : d_rem_sh;
    wire [32:0] d_q_nx   = {d_q[31:0], d_ge};

    wire [5:0]  s_pair_idx = (6'd31 - seq_cnt) << 1;
    wire [1:0]  s_pair = s_rad[s_pair_idx +: 2];
    wire [35:0] s_rem_sh = {s_rem[33:0], s_pair};
    wire [35:0] s_trial = {2'b0, s_q, 2'b00} | 36'd1;
    wire        s_ge = (s_rem_sh >= s_trial);
    wire [35:0] s_rem_nx = s_ge ? (s_rem_sh - s_trial) : s_rem_sh;
    wire [31:0] s_q_nx = {s_q[30:0], s_ge};

    reg [31:0] d_sig;
    reg        d_sticky;
    reg signed [15:0] d_exp;
    reg [31:0] s_sig;
    reg        s_sticky;
    reg signed [15:0] s_exp;
    always @(*) begin
        d_sig    = d_q[32] ? d_q[32:1] : d_q[31:0];
        d_sticky = (d_rem != 25'd0) | (d_q[32] & d_q[0]);
        d_exp    = d_ea - d_eb + 16'd126 + {15'b0, d_q[32]};
        s_sig    = s_q;
        s_sticky = (s_rem != 36'd0);
        s_exp    = s_k[0] ? (16'sd139 + (($signed(s_k) - 16'sd1) >>> 1))
                          : (16'sd138 + ($signed(s_k) >>> 1));
    end

    reg [31:0] result_q;
    reg [4:0]  flags_q;

    assign busy   = (state == S_B) || (state == S_FMA) || (state == S_ITER) ||
                    (state == S_ITERF) || (state == S_ROUND);
    assign done   = (state == S_DONE);
    assign result = result_q;
    assign fflags = flags_q;

    always @(posedge clk) begin
        if (!rstn) begin
            state <= S_IDLE;
            op_q <= 5'd31; rm_q <= 3'd0;
            ra <= 32'd0; rb <= 32'd0; rc <= 32'd0;
            xm <= 24'd0; ym <= 24'd0; zm <= 24'd0;
            xe <= 16'sd0; ye <= 16'sd0; ze <= 16'sd0;
            xs <= 1'b0; ys <= 1'b0; zs <= 1'b0;
            pre_valid_q <= 1'b0; pre_sign_q <= 1'b0;
            pre_e_q <= 16'sd0; pre_sig_q <= 32'd0; pre_sticky_q <= 1'b0;
            result_q <= 32'd0; flags_q <= 5'b0;
            fq_p64 <= 64'd0; fq_c64 <= 64'd0; fq_base <= 16'sd0;
            fq_sp <= 1'b0; fq_sc <= 1'b0;
            seq_cnt <= 6'd0;
            d_ma <= 24'd0; d_mb <= 24'd0; d_ea <= 16'sd0; d_eb <= 16'sd0;
            d_dividend <= 64'd0; d_rem <= 25'd0; d_q <= 33'd0;
            s_rem <= 36'd0; s_q <= 32'd0; s_rad <= 64'd0; s_k <= 16'sd0;
            iter_sqrt_q <= 1'b0;
        end else begin
            case (state)
                S_IDLE, S_DONE: begin
                    if (start) begin
                        op_q <= fop;
                        rm_q <= rm_eff;
                        ra <= a; rb <= b; rc <= c;
                        xm <= axm; ym <= aym; zm <= azm;
                        xe <= axe; ye <= aye; ze <= aze;
                        xs <= a[31]; ys <= b[31]; zs <= c[31];
                        state <= S_B;
                    end
                end
                S_B: begin
                    if (dir_valid) begin
                        result_q <= dir_result;
                        flags_q  <= dir_flags;
                        state    <= S_DONE;
                    end else if (fma_go) begin
                        fq_p64 <= fma_p64; fq_c64 <= fma_c64;
                        fq_base <= fma_base; fq_sp <= fma_sp; fq_sc <= fma_sc;
                        state <= S_FMA;
                    end else if (iter_go) begin
                        if (iter_sqrt) begin
                            iter_sqrt_q <= 1'b1;
                            s_k <= xe - 16'sd150;
                            s_rad <= ((xe - 16'sd150) & 16'sd1) ?
                                     ({40'b0, xm} << 39) :
                                     ({40'b0, xm} << 40);
                            s_rem <= 36'b0; s_q <= 32'b0;
                        end else begin
                            iter_sqrt_q <= 1'b0;
                            d_ma <= xm; d_mb <= ym;
                            d_ea <= xe; d_eb <= ye;
                            d_dividend <= {8'b0, xm, 32'b0};
                            d_rem <= 25'b0; d_q <= 33'b0;
                        end
                        seq_cnt <= 6'd0;
                        state <= S_ITER;
                    end else begin
                        pre_valid_q <= pre_valid;
                        pre_sign_q <= pre_sign;
                        pre_e_q <= pre_e;
                        pre_sig_q <= pre_sig;
                        pre_sticky_q <= pre_sticky;
                        state <= S_ROUND;
                    end
                end
                S_FMA: begin
                    if (pre2_zero) begin
                        result_q <= {rm_q == 3'b010, 31'b0};
                        flags_q  <= 5'b0;
                        state    <= S_DONE;
                    end else begin
                        pre_valid_q <= pre2_valid;
                        pre_sign_q <= pre2_sign;
                        pre_e_q <= pre2_e;
                        pre_sig_q <= pre2_sig;
                        pre_sticky_q <= pre2_sticky;
                        state <= S_ROUND;
                    end
                end
                S_ITER: begin
                    if (iter_sqrt_q == 1'b0) begin
                        d_rem      <= d_rem_nx;
                        d_q        <= d_q_nx;
                        d_dividend <= {d_dividend[62:0], 1'b0};
                        if (seq_cnt == 6'd55) state <= S_ITERF;
                        else seq_cnt <= seq_cnt + 6'd1;
                    end else begin
                        s_rem <= s_rem_nx;
                        s_q   <= s_q_nx;
                        if (seq_cnt == 6'd31) state <= S_ITERF;
                        else seq_cnt <= seq_cnt + 6'd1;
                    end
                end
                S_ITERF: begin
                    pre_valid_q <= 1'b1;
                    pre_sticky_q <= iter_sqrt_q ? s_sticky : d_sticky;
                    if (iter_sqrt_q) begin
                        pre_sign_q <= 1'b0;
                        pre_e_q <= s_exp;
                        pre_sig_q <= s_sig;
                    end else begin
                        pre_sign_q <= ra[31] ^ rb[31];
                        pre_e_q <= d_exp;
                        pre_sig_q <= d_sig;
                    end
                    state <= S_ROUND;
                end
                S_ROUND: begin
                    rp = rp_f32(rm_q, pre_sign_q, pre_e_q,
                                pre_sig_q, pre_sticky_q);
                    result_q <= pre_valid_q ? rp[31:0] : 32'd0;
                    flags_q  <= pre_valid_q ? rp[36:32] : 5'b0;
                    state    <= S_DONE;
                end
                default: state <= S_IDLE;
            endcase
        end
    end

endmodule
