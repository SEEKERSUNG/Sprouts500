/* Sprouts500 lockstep: RTL image.  Code lives in the ITCM at 0x0 (4 MiB in
   sim/tb_lockstep.v).  Generated data pages and stacks are pinned inside the
   16 KiB DTCM at 0x10000000 so they cannot alias each other. */
OUTPUT_ARCH("riscv")
ENTRY(_start)
SECTIONS {
  . = 0x00000000;
  .text : { *(.text) *(.text.*) }
  . = 0x10000000;
  .region_0 : { *(.region_0) }
  . = 0x10003000;
  .user_stack : { *(.user_stack) }
  . = 0x10003400;
  .data : { *(.data) *(.data.*) *(.rodata) *(.rodata.*) *(.sdata) *(.sdata.*) }
  .bss (NOLOAD) : { *(.bss) *(.sbss) }
  /* trap record (mcause, mepc, mtval, count) and halt mailbox: top of DTCM */
  trap_record = 0x10003f00;
  halt_mailbox = 0x10003ff0;
}
