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
