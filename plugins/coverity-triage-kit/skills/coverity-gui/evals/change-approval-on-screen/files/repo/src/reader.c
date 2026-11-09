#include <stdio.h>
int read_header(FILE *f) { char b[8]; return fgets(b, 8, f) != NULL; }
