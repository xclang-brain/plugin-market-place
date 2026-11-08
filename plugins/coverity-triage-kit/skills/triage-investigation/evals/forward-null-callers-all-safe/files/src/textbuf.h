#ifndef TEXTBUF_H
#define TEXTBUF_H

#include <stddef.h>

struct textbuf {
	char data[256];
	size_t len;
	unsigned int errors;
};

int textbuf_header(struct textbuf *b);
int textbuf_footer(struct textbuf *b);
int textbuf_name(struct textbuf *b, const char *name);

#endif
