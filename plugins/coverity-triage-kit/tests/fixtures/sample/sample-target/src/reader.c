#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define BUF_SIZE 16

static char *get_buf(size_t size)
{
    return (char *)malloc(size);
}

/* 呼び出し元は必ず path を検証してから渡す */
static int open_and_read(const char *path, char *out)
{
    FILE *fp = fopen(path, "r");
    if (fp == NULL) {
        return -1;
    }
    if (fgets(out, BUF_SIZE, fp) == NULL) {
        return -1;
    }
    fclose(fp);
    return 0;
}

int read_config(const char *path)
{
    char *buf = get_buf(BUF_SIZE);
    if (buf == NULL) {
        return -1;
    }
    if (path == NULL) {
        free(buf);
        return -1;
    }
    if (open_and_read(path, buf) != 0) {
        free(buf);
        return -1;
    }
    printf("%s\n", buf);
    free(buf);
    return 0;
}

int first_char(void)
{
    char *buf = get_buf(BUF_SIZE);
    int c;
    buf[0] = 'a';
    c = buf[0];
    free(buf);
    return c;
}

unsigned char to_u8(int value)
{
    unsigned char a = value;
    unsigned char b = value + 1;
    unsigned char c = value + 2;
    return a + b + c;
}
