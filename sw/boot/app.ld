/* Application layout for the UART Bootloader.  The host prepends a 16-byte
 * header at 0x800; this raw binary is linked and loaded at 0x810. */
OUTPUT_ARCH("riscv")
ENTRY(_start)

MEMORY
{
    ITCM (rx) : ORIGIN = 0x00000810, LENGTH = 0x000077E0
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

    ASSERT(_image_end <= 0x00007FF0, "boot app overlaps the ITCM mailboxes")
    ASSERT(_ebss <= _stack_top - 0x00000800, ".bss leaves less than 2 KiB for the stack")
}
