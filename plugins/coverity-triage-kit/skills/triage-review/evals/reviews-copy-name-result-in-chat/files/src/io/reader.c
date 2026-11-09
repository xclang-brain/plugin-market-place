/* Reading of name records. */
#include <stddef.h>
#include <string.h>

#define POOL_COUNT 4
#define NAME_MAX_LEN 64

static char pool[POOL_COUNT][NAME_MAX_LEN];
static int used;

/* Returns a free buffer, or NULL when all buffers are in use. */
char *get_buf(void)
{
    if (used >= POOL_COUNT) {
        return NULL;
    }
    return pool[used++];
}

static size_t copy_name(char *dst, const char *src)
{
    size_t n = strnlen(src, NAME_MAX_LEN - 1);
    memcpy(dst, src, n);
    dst[n] = '\0';
    return n;
}

/* Reads one name record into a pool buffer. */
int read_all(const char *name)
{
    char *buf = get_buf();
    if (buf == NULL) {
        return -1;
    }
    (void)copy_name(buf, name);
    return 0;
}
