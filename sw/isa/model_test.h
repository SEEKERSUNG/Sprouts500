// Minimal RVMODEL for Sprouts500 ISA tests.
// The testbench addresses the signature directly from ITCM, and halts on a
// magic store to 0x003FFFF0 (halt mailbox in the 4 MiB simulation ITCM).

#ifndef _COMPLIANCE_MODEL_H
#define _COMPLIANCE_MODEL_H

#define RVMODEL_DATA_SECTION \
    .pushsection .tohost,"aw",@progbits; \
    .align 8; .global tohost;   tohost:   .dword 0; \
    .align 8; .global fromhost; fromhost: .dword 0; \
    .popsection;

#define RVMODEL_HALT \
    li t0, 0x003FFFF0; \
    li t1, 0x5A5A5A5A; \
    sw t1, 0(t0); \
999: j 999b;

#define RVMODEL_DATA_BEGIN \
    .align 4; .global begin_signature; begin_signature:

#define RVMODEL_DATA_END \
    .align 4; .global end_signature; end_signature: \
    RVMODEL_DATA_SECTION

#define RVMODEL_BOOT

#define RVMODEL_IO_INIT
#define RVMODEL_IO_WRITE_STR(_SP, _STR)
#define RVMODEL_IO_ASSERT_GPR_EQ(_SP, _R, _I)
#define RVMODEL_IO_ASSERT_SFPR_EQ(_F, _R, _I)
#define RVMODEL_IO_ASSERT_DFPR_EQ(_D, _R, _I)

#define RVMODEL_SET_MSW_INT
#define RVMODEL_CLEAR_MSW_INT
#define RVMODEL_CLEAR_MTIMER_INT
#define RVMODEL_CLEAR_MEXT_INT

#endif
