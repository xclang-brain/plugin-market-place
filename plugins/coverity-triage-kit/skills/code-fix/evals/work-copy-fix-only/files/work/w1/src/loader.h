#ifndef LOADER_H
#define LOADER_H

#define LOADER_OK        0
#define LOADER_ERR_OPEN  (-1)
#define LOADER_ERR_SHORT (-2)

int loader_check_size(const char *path);
int load_table(const char *path, unsigned char *out);
int load_header(const char *path, unsigned char *out, unsigned int size);

#endif
