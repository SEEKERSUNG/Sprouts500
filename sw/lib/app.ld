/* Linker script for user programs (sw/lib).
 *
 * ITCM (32 KiB): .text, .rodata, .data -- loaded by $readmemh at address 0.
 * DTCM (16 KiB): .bss and the stack -- zeroed by crt0, never preloaded.
 *
 * The top 16 bytes of ITCM are the simulation mailboxes:
 *   0x7FF8 result code, 0x7FFC halt magic (0x5A5A5A5A).
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

    ASSERT(_image_end <= 0x00007FF0, "program image overlaps the ITCM mailboxes")
    ASSERT(_ebss <= _stack_top - 0x00000800, ".bss leaves less than 2 KiB for the stack")
}
