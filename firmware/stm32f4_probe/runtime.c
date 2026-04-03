#include <stddef.h>
#include <stdint.h>

void *memcpy(void *dest, const void *src, size_t n)
{
    uint8_t *dst = (uint8_t *)dest;
    const uint8_t *source = (const uint8_t *)src;

    for (size_t i = 0; i < n; ++i) {
        dst[i] = source[i];
    }

    return dest;
}

void *memmove(void *dest, const void *src, size_t n)
{
    uint8_t *dst = (uint8_t *)dest;
    const uint8_t *source = (const uint8_t *)src;

    if (dst == source || n == 0) {
        return dest;
    }

    if (dst < source) {
        for (size_t i = 0; i < n; ++i) {
            dst[i] = source[i];
        }
    } else {
        for (size_t i = n; i > 0; --i) {
            dst[i - 1] = source[i - 1];
        }
    }

    return dest;
}

void *memset(void *dest, int value, size_t n)
{
    uint8_t *dst = (uint8_t *)dest;

    for (size_t i = 0; i < n; ++i) {
        dst[i] = (uint8_t)value;
    }

    return dest;
}

int memcmp(const void *lhs, const void *rhs, size_t n)
{
    const uint8_t *a = (const uint8_t *)lhs;
    const uint8_t *b = (const uint8_t *)rhs;

    for (size_t i = 0; i < n; ++i) {
        if (a[i] != b[i]) {
            return (int)a[i] - (int)b[i];
        }
    }

    return 0;
}

void __aeabi_memcpy(void *dest, const void *src, size_t n)
{
    (void)memcpy(dest, src, n);
}

void __aeabi_memcpy4(void *dest, const void *src, size_t n)
{
    (void)memcpy(dest, src, n);
}

void __aeabi_memcpy8(void *dest, const void *src, size_t n)
{
    (void)memcpy(dest, src, n);
}

void __aeabi_memmove(void *dest, const void *src, size_t n)
{
    (void)memmove(dest, src, n);
}

void __aeabi_memset(void *dest, size_t n, int value)
{
    (void)memset(dest, value, n);
}

void __aeabi_memset4(void *dest, size_t n, int value)
{
    (void)memset(dest, value, n);
}

void __aeabi_memset8(void *dest, size_t n, int value)
{
    (void)memset(dest, value, n);
}

void __aeabi_memclr(void *dest, size_t n)
{
    (void)memset(dest, 0, n);
}

void __aeabi_memclr4(void *dest, size_t n)
{
    (void)memset(dest, 0, n);
}

void __aeabi_memclr8(void *dest, size_t n)
{
    (void)memset(dest, 0, n);
}
