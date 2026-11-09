/* fatal.c - stops the controller on an unrecoverable error */
#include "fatal.h"
#include "wdt.h"
#include "log.h"

void fatal(const char *why)
{
	log_write(LOG_CRIT, why);
	wdt_force_reset();
	for (;;) {
		/* wait for the watchdog reset */
	}
}
