/* Resident bootloader: first 2 KiB of ITCM, data/stack in DTCM. */
OUTPUT_ARCH("riscv")
ENTRY(_start)

MEMORY
{
    BOOT (rx)  : ORIGIN = 0x00000000, LENGTH = 0x00000800
    DTCM (rw)  : ORIGIN = 0x10000000, LENGTH = 0x00004000
}

SECTIONS
{
    .text : {
        *(.text.init)
        *(.text)
        *(.text.*)
    } > BOOT

    .rodata : {
        *(.rodata)
        *(.rodata.*)
        *(.srodata)
        *(.srodata.*)
    } > BOOT

    .data : {
        *(.data)
        *(.data.*)
        *(.sdata)
        *(.sdata.*)
        . = ALIGN(4);
        _image_end = .;
    } > BOOT

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
    ASSERT(_image_end <= ORIGIN(BOOT) + LENGTH(BOOT), "bootloader exceeds 2 KiB")
}
