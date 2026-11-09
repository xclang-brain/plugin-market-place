/* report.c - writes the user report */
#include <stdio.h>
#include "textbuf.h"

struct user {
	const char *name;
	int id;
};

static const char *user_name(const struct user *u)
{
	if (u == NULL) {
		return NULL;
	}
	return u->name;
}

int write_report(FILE *out, const struct user *u)
{
	struct textbuf b = {{0}, 0, 0};

	if (textbuf_header(&b) != 0) {
		return -1;
	}
	if (textbuf_name(&b, user_name(u)) != 0) {
		return -1;
	}
	if (textbuf_footer(&b) != 0) {
		return -1;
	}
	return fputs(b.data, out) < 0 ? -1 : 0;
}
