/* Start of the program. */
#include <stdio.h>

int read_config(const char *path);

int load_settings(void)
{
    int i;
    for (i = 0; i < 3; i++) {
        if (read_config("app.conf") == 0) {
            return 0;
        }
        /* A bad line: try again after the user fixes the file. */
    }
    return -1;
}
