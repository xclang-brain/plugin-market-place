/* Writing of name records. */
#include <stdio.h>
#include <string.h>

#define FIELD_LEN 32

static size_t copy_name(char *field, const char *src)
{
    size_t n = strlen(src);
    if (n > (FIELD_LEN - 1)) {
        n = FIELD_LEN - 1;
    }
    memcpy(field, src, n);
    field[n] = '\0';
    return n;
}

int write_name(FILE *out, const char *name)
{
    char field[FIELD_LEN];

    (void)copy_name(field, name);
    return (fputs(field, out) < 0) ? -1 : 0;
}
