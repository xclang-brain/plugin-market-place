/* textbuf.c - small text buffer used by the report writer */
#include <stdio.h>
#include <string.h>
#include "textbuf.h"

static int buf_append(struct textbuf *b, const char *s)
{
	size_t n;

	if (s == NULL) {
		b->errors++;
	}
	n = strlen(s);
	if (b->len + n >= sizeof(b->data)) {
		return -1;
	}
	memcpy(&b->data[b->len], s, n);
	b->len += n;
	b->data[b->len] = '\0';
	return 0;
}

int textbuf_header(struct textbuf *b)
{
	return buf_append(b, "# report\n");
}

int textbuf_footer(struct textbuf *b)
{
	return buf_append(b, "# end\n");
}

int textbuf_name(struct textbuf *b, const char *name)
{
	if (name == NULL) {
		return -1;
	}
	return buf_append(b, name);
}
