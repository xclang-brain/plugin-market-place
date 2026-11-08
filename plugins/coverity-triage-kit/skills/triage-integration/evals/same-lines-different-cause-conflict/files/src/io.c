/* Reading of records. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct record {
    char name[16];
    int size;
};

int read_record(FILE *fp, struct record *out)
{
    char line[64];
    char *data;

    if (fgets(line, sizeof line, fp) == NULL) {
        return -1;
    }
    data = malloc(strlen(line) + 1);
    strcpy(data, line);
    out->size = atoi(data);
    strcpy(out->name, data);
    free(data);
    return 0;
}
