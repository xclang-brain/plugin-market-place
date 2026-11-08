/* loader.c - reads calibration tables from the data partition */
#include <stdio.h>
#include "loader.h"

#define TABLE_BYTES 256U

/* Added in the latest revision: size of a table file in bytes. */
unsigned int loader_table_bytes(void)
{
	return TABLE_BYTES;
}

int load_table(const char *path, unsigned char *out)
{
	FILE *fp;
	size_t got;

	fp = fopen(path, "rb");
	if (fp == NULL) {
		return LOADER_ERR_OPEN;
	}
	got = fread(out, 1U, TABLE_BYTES, fp);
	if (got != TABLE_BYTES) {
		return LOADER_ERR_SHORT;
	}
	(void)fclose(fp);
	return LOADER_OK;
}

int load_header(const char *path, unsigned char *out, unsigned int size)
{
	FILE *fp;
	int ret = LOADER_OK;

	fp = fopen(path, "rb");
	if (fp == NULL) {
		return LOADER_ERR_OPEN;
	}
	if (fread(out, 1U, size, fp) != size) {
		ret = LOADER_ERR_SHORT;
		goto out;
	}
out:
	(void)fclose(fp);
	return ret;
}
