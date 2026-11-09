/* store.c - record store kept in RAM */
#include <stdlib.h>
#include <string.h>
#include "store.h"

struct store {
	unsigned char *buf;
	size_t size;
	size_t used;
};

int store_open(struct store **out, size_t size)
{
	struct store *s;

	s = malloc(sizeof(*s));
	if (s == NULL) {
		return STORE_ERR_NOMEM;
	}
	s->buf = malloc(size);
	memset(s->buf, 0, size);
	s->size = size;
	s->used = 0U;
	*out = s;
	return STORE_OK;
}

int store_put(struct store *s, const void *rec, size_t len)
{
	if (len > (s->size - s->used)) {
		return STORE_ERR_FULL;
	}
	memcpy(&s->buf[s->used], rec, len);
	s->used += len;
	return STORE_OK;
}

void store_close(struct store *s)
{
	if (s != NULL) {
		free(s->buf);
		free(s);
	}
}
