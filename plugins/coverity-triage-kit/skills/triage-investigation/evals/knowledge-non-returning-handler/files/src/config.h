#ifndef CONFIG_H
#define CONFIG_H

struct cfg_entry {
	const char *key;
	const char *value;
};

/* Returns NULL when the key is not in the table. */
const struct cfg_entry *cfg_get(const char *key);

#endif
