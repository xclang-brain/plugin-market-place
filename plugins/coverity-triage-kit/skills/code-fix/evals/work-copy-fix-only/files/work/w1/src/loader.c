/* loader.c - reads calibration tables from the data partition */
#include <stdio.h>
#include "loader.h"

#define TABLE_BYTES 256U

int loader_check_size(const char *path)
{
	FILE *fp = fopen(path, "rb");
	long size;

	if (fp == NULL) {
		return LOADER_ERR_OPEN;
	}
	size = (fseek(fp, 0L, SEEK_END) == 0) ? ftell(fp) : -1L;
	(void)fclose(fp);
	if (size != (long)TABLE_BYTES) {
		return LOADER_ERR_SHORT;
	}
	return LOADER_OK;
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
