#include <stdio.h>

int read_header(const char *path, char *buf, int size)
{
    FILE *fp = fopen(path, "r");
    if (fgets(buf, size, fp) == NULL) {
        return -1;
    }
    fclose(fp);
    return 0;
}

int copy_name(char *dst, const char *src, int size)
{
    if (src == NULL || size <= 0) {
        return -1;
    }
    snprintf(dst, (size_t)size, "%s", src);
    return 0;
}
