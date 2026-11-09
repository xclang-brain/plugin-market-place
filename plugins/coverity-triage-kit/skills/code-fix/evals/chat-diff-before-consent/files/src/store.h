#ifndef STORE_H
#define STORE_H

#include <stddef.h>

#define STORE_OK          0
#define STORE_ERR_NOMEM (-1)
#define STORE_ERR_FULL  (-2)

struct store;

int store_open(struct store **out, size_t size);
int store_put(struct store *s, const void *rec, size_t len);
void store_close(struct store *s);

#endif
