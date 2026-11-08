/* settings.c - reads values from the configuration table */
#include <stdlib.h>
#include "config.h"
#include "fatal.h"
#include "settings.h"

static unsigned int read_uint(const char *key, unsigned int fallback)
{
	const struct cfg_entry *e = cfg_get(key);

	if (e == NULL) {
		return fallback;
	}
	return (unsigned int)strtoul(e->value, NULL, 10);
}

unsigned int settings_port(void)
{
	const struct cfg_entry *p = cfg_get("net.port");

	if (p == NULL) {
		fatal("net.port がない");
	}
	return (unsigned int)strtoul(p->value, NULL, 10);
}

unsigned int settings_timeout(void)
{
	return read_uint("net.timeout", 30U);
}
