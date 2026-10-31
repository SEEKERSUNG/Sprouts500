// Tiny freestanding string/memory routines.
//
// The no-tree-loop-distribute-patterns attribute keeps -O2 from turning these
// loops back into calls to memset/memcpy (which would recurse into themselves).

#include "soc.h"

__attribute__((optimize("no-tree-loop-distribute-patterns")))
void *memcpy(void *dst, const void *src, unsigned int n)
{
    unsigned char *d = dst;
    const unsigned char *s = src;

    while (n--)
        *d++ = *s++;
    return dst;
}

__attribute__((optimize("no-tree-loop-distribute-patterns")))
void *memmove(void *dst, const void *src, unsigned int n)
{
    unsigned char *d = dst;
    const unsigned char *s = src;

    if (d <= s) {
        while (n--)
            *d++ = *s++;
    } else {
        d += n;
        s += n;
        while (n--)
            *--d = *--s;
    }
    return dst;
}

__attribute__((optimize("no-tree-loop-distribute-patterns")))
void *memset(void *dst, int c, unsigned int n)
{
    unsigned char *d = dst;
    unsigned char v = (unsigned char)c;

    while (n--)
        *d++ = v;
    return dst;
}

__attribute__((optimize("no-tree-loop-distribute-patterns")))
int memcmp(const void *a, const void *b, unsigned int n)
{
    const unsigned char *pa = a;
    const unsigned char *pb = b;

    while (n--) {
        if (*pa != *pb)
            return (int)*pa - (int)*pb;
        pa++;
        pb++;
    }
    return 0;
}

__attribute__((optimize("no-tree-loop-distribute-patterns")))
unsigned int strlen(const char *s)
{
    const char *p = s;

    while (*p)
        p++;
    return (unsigned int)(p - s);
}
