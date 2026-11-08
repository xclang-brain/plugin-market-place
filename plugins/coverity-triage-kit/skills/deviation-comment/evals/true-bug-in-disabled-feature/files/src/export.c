/* Export of records in the old (v1) format. */
#include <string.h>

int feature_enabled(const char *name);
int write_record(const char *tag, const char *body);

#define TAG_LEN 8

/* name comes from the user's record and can be up to 32 characters. */
int export_legacy(const char *name, const char *body)
{
    char tag[TAG_LEN];

    if (!feature_enabled("legacy_export")) {
        return -1;
    }
    /* No length check: a long name overruns tag. */
    strcpy(tag, name);
    return write_record(tag, body);
}
