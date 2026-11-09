/* Reading of the settings file. */
#include <stdio.h>
#include <string.h>

#define LINE_MAX_LEN 128

int parse_line(const char *line);

int read_config(const char *path)
{
    char line[LINE_MAX_LEN];
    FILE *fp = fopen(path, "r");
    if (fp == NULL) {
        return -1;
    }
    while (fgets(line, sizeof line, fp) != NULL) {
        if (parse_line(line) != 0) {
            return -2;
        }
    }
    fclose(fp);
    return 0;
}
