/* Linker script for the benchmarks (bench/).
 *
 * Image fits in ITCM (32 KiB, SoC configuration).  .bss and the stack live in
 * DTCM (16 KiB).  The top words of ITCM are the mailboxes read by tb_bench:
 *   0x7FF4 cycles, 0x7FF8 result, 0x7FFC halt magic.
 */
OUTPUT_ARCH("riscv")
ENTRY(_start)

MEMORY
{
    ITCM (rx) : ORIGIN = 0x00000000, LENGTH = 0x00008000
    DTCM (rw) : ORIGIN = 0x10000000, LENGTH = 0x00004000
}

SECTIONS
{
    .text : {
        *(.text.init)
        *(.text)
        *(.text.*)
    } > ITCM

    .rodata : {
        *(.rodata)
        *(.rodata.*)
        *(.srodata)
        *(.srodata.*)
    } > ITCM

    .data : {
        *(.data)
        *(.data.*)
        *(.sdata)
        *(.sdata.*)
        . = ALIGN(4);
        _image_end = .;
    } > ITCM

    .bss (NOLOAD) : {
        _sbss = .;
        *(.bss)
        *(.bss.*)
        *(.sbss)
        *(.sbss.*)
        *(COMMON)
        . = ALIGN(4);
        _ebss = .;
    } > DTCM

    _stack_top = ORIGIN(DTCM) + LENGTH(DTCM);

    ASSERT(_image_end <= 0x00007FF0, "benchmark image overlaps the ITCM mailboxes")
    ASSERT(_ebss <= _stack_top - 0x00000800, ".bss leaves less than 2 KiB for the stack")
}
