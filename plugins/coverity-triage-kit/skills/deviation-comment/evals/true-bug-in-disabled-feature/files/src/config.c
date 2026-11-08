/* Feature switches of the product. The table is read only (see feature_enabled()). */
#include <string.h>

struct feature {
    const char *name;
    int enabled;
};

static const struct feature features[] = {
    { "log_rotate", 1 },
    { "fast_boot", 1 },
    { "legacy_export", 0 },
    { "remote_debug", 0 },
};

int feature_enabled(const char *name)
{
    size_t i;
    for (i = 0; i < sizeof features / sizeof features[0]; i++) {
        if (strcmp(features[i].name, name) == 0) {
            return features[i].enabled;
        }
    }
    return 0;
}
