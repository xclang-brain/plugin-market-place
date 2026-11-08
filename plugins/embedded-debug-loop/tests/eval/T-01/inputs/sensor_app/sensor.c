#include "sensor.h"

int sensor_average(const uint8_t *values, int count)
{
    uint8_t sum = 0;

    for (int i = 0; i < count; i++) {
        sum += values[i];
    }
    return sum / count;
}
