/* Linker script for riscv-arch-test 2.x I/M tests on Sprouts500.
 * Code lives at ITCM 0x0000_0000, data/signature at the next 4 KiB boundary.
 * The simulation ITCM is 4 MiB; the word at 0x003F_FFF0 is the halt mailbox.
 */
OUTPUT_ARCH("riscv")
ENTRY(rvtest_entry_point)

SECTIONS
{
    . = 0x00000000;
    .text.init : { *(.text.init) }
    .text      : { *(.text) }

    . = ALIGN(0x1000);
    .data      : { *(.data) *(.data.string) *(.sdata) *(.sdata.string) }
    .bss       : { *(.bss) *(.sbss) }
    .tohost    : { *(.tohost) }

    _end = .;
    ASSERT(_end <= 0x003FFFF0, "test image exceeds 4 MiB simulation ITCM")
}
