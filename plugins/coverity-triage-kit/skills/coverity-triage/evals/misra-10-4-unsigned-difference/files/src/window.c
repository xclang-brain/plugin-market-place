/* window.c - receive window of the serial link */
#include <stdint.h>

/*
 * Returns the number of bytes left in the receive window.
 * Returns 0 when end is not greater than start.
 */
uint16_t window_remaining(uint16_t start, uint16_t end)
{
	uint16_t remaining = 0U;

	if ((end - start) > 0) {
		remaining = (uint16_t)(end - start);
	}
	return remaining;
}
