/* Reading of the temperature sensor. */
#include <stdint.h>

#define TABLE_SIZE 16

/* Provided by the board support package (not in this repository). */
extern uint32_t read_reg(uint32_t addr);

static const int16_t table[TABLE_SIZE] = {
    -40, -30, -20, -10, 0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110
};

int16_t read_temperature(void)
{
    uint32_t index = read_reg(0x40010000u) >> 4;
    return table[index];
}
