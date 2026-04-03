#include "semihost.h"

#include <stdint.h>

enum {
    SEMIHOST_SYS_WRITE0 = 0x04,
    SEMIHOST_SYS_EXIT = 0x18,
    SEMIHOST_SYS_EXIT_EXTENDED = 0x20,
    ADP_STOPPED_APPLICATION_EXIT = 0x20026,
    ADP_STOPPED_RUNTIME_ERROR = 0x20023,
};

static int semihost_call(int reason, void *argument)
{
    register int r0 __asm__("r0") = reason;
    register void *r1 __asm__("r1") = argument;

    __asm__ volatile(
        "bkpt 0xab"
        : "+r"(r0)
        : "r"(r1)
        : "memory");

    return r0;
}

void semihost_write0(const char *message)
{
    semihost_call(SEMIHOST_SYS_WRITE0, (void *)message);
}

void semihost_write_line(const char *message)
{
    semihost_write0(message);
}

void semihost_exit(int status)
{
    uint32_t args[2] = {
        ADP_STOPPED_APPLICATION_EXIT,
        (uint32_t)status,
    };

    semihost_call(SEMIHOST_SYS_EXIT_EXTENDED, args);
    semihost_call(
        SEMIHOST_SYS_EXIT,
        (void *)(uintptr_t)(status == 0 ? ADP_STOPPED_APPLICATION_EXIT
                                        : ADP_STOPPED_RUNTIME_ERROR));

    for (;;) {
    }
}
