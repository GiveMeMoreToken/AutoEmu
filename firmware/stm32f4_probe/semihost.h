#ifndef AUTOEMU_STM32F4_PROBE_SEMIHOST_H
#define AUTOEMU_STM32F4_PROBE_SEMIHOST_H

#include <stdint.h>

void semihost_write0(const char *message);
void semihost_write_line(const char *message);
void semihost_exit(int status) __attribute__((noreturn));

#endif
