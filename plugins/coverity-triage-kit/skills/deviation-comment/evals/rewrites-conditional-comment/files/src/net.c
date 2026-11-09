/* Sending of packets. */
#include <stddef.h>

int hw_send(const unsigned char *data, size_t len);

/* Copies the first byte as the packet type. Called only from send_all(). */
static int send_packet(const unsigned char *buf, size_t len)
{
    unsigned char type = buf[0];
    if (type == 0xFFu) {
        return -1;
    }
    return hw_send(buf, len);
}

int send_all(const unsigned char *buf, size_t len)
{
    if (buf == NULL || len == 0) {
        return 0;
    }
    return send_packet(buf, len);
}
