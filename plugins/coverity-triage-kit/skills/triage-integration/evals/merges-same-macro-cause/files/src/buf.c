/* Message buffers. */
#include <stddef.h>
#include "buf.h"

struct msg {
    char head[16];
    char body[16];
    int count;
};

static struct msg msgs[4];

/* Copies a header into message n. */
void set_head(int n, const char *src)
{
    size_t i;
    struct msg *m = &msgs[n & 3];

    for (i = 0; i < BUF_LEN; i++) {
        m->head[i] = src[i];
        if (src[i] == '\0') {
            break;
        }
    }
}

/* Copies a body into message n. */
void set_body(int n, const char *src)
{
    size_t i;
    struct msg *m = &msgs[n & 3];

    for (i = 0; i < BUF_LEN; i++) {
        m->body[i] = src[i];
        if (src[i] == '\0') {
            break;
        }
    }
}

/* Sums the counts of the first k messages. */
int total_count(int k)
{
    int i;
    int sum = 0;

    for (i = 0; i <= k; i++) {
        sum += msgs[i].count;
    }
    return sum;
}
