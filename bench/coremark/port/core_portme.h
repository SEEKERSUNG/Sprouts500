/*
Copyright 2018 Embedded Microprocessor Benchmark Consortium (EEMBC)
Licensed under the Apache License, Version 2.0.
https://github.com/eembc/coremark/blob/main/barebones/core_portme.h

Sprouts500 port: no stdio/time.h, no hardware floating point, timing from the
cycle CSR (rdcycle), 16 KiB DTCM for the static data area.
*/
#ifndef CORE_PORTME_H
#define CORE_PORTME_H

#include <stddef.h>

/* Sprouts500 has no FPU; ee_printf prints integers only. */
#define HAS_FLOAT 0
#define HAS_TIME_H 0
#define USE_CLOCK 0
#define HAS_STDIO 0
#define HAS_PRINTF 0

#ifndef COMPILER_VERSION
#define COMPILER_VERSION "GCC " __VERSION__
#endif
#ifndef COMPILER_FLAGS
#define COMPILER_FLAGS FLAGS_STR
#endif
#ifndef MEM_LOCATION
#define MEM_LOCATION "DTCM"
#endif

typedef signed short   ee_s16;
typedef unsigned short ee_u16;
typedef signed int     ee_s32;
typedef double         ee_f32;
typedef unsigned char  ee_u8;
typedef unsigned int   ee_u32;
typedef ee_u32         ee_ptr_int;
typedef ee_u32         ee_size_t;
#define NULL ((void *)0)

#define align_mem(x) (void *)(4 + (((ee_ptr_int)(x)-1) & ~3))

/* Timing: rdcycle returns CPU cycles; CLOCKS_PER_SEC is fixed by tb_bench. */
#define CORETIMETYPE ee_u32
typedef ee_u32 CORE_TICKS;

#define SEED_METHOD SEED_VOLATILE
#define MEM_METHOD MEM_STATIC
#define MULTITHREAD 1
#define USE_PTHREAD 0
#define USE_FORK 0
#define USE_SOCKET 0
#define MAIN_HAS_NOARGC 1
#define MAIN_HAS_NORETURN 0

typedef struct CORE_PORTABLE_S
{
    ee_u8 portable_id;
} core_portable;

void portable_init(core_portable *p, int *argc, char *argv[]);
void portable_fini(core_portable *p);

/* bench/coremark/port/main.c drives the benchmark; see bench/BENCHMARKS.md. */
int ee_printf(const char *fmt, ...);

extern ee_u32 default_num_contexts;

#endif /* CORE_PORTME_H */
