#include <stdio.h>
#include <stdlib.h>

char *read_header(FILE *fp) {
    char *buf = malloc(64);
    fgets(buf, 64, fp);
    return buf;
}
