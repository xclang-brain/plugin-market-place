/* calib.c - applies the calibration table at start-up */
#include "loader.h"

static unsigned char table[256];

int calib_init(void)
{
	/* The file can be shorter than 256 bytes after an interrupted update. */
	return load_table("/data/calib.bin", table);
}
