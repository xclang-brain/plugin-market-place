#ifndef LOADER_H
#define LOADER_H

#define LOADER_OK        0
#define LOADER_ERR_OPEN  (-1)
#define LOADER_ERR_SHORT (-2)

unsigned int loader_table_bytes(void);
int load_table(const char *path, unsigned char *out);
int load_header(const char *path, unsigned char *out, unsigned int size);

#endif
