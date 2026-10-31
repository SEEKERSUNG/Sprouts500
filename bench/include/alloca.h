/* Freestanding alloca: GCC lowers this to a stack adjustment. */
#ifndef SPROUTS500_ALLOCA_H
#define SPROUTS500_ALLOCA_H

#define alloca(size) __builtin_alloca(size)

#endif
