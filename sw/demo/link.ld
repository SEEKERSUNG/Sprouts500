OUTPUT_ARCH("riscv")
ENTRY(_start)

/* Demo image lives entirely in ITCM (32 KiB).  The top word 0x7FFC is the
 * simulation halt mailbox and the stack starts at 0x7FF0. */
SECTIONS
{
    . = 0x00000000;
    .text.init : { *(.text.init) }
    .text      : { *(.text) }
    .rodata    : { *(.rodata) *(.rodata.*) }
    .data      : { *(.data) *(.data.*) *(.sdata) *(.sdata.*) }
    .bss       : { *(.bss) *(.bss.*) *(.sbss) *(.sbss.*) *(COMMON) }

    _end = .;
    ASSERT(_end <= 0x00007F00, "demo image exceeds ITCM")
}
