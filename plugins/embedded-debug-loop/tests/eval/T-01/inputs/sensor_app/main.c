#include <stdio.h>

#include "sensor.h"

static const uint8_t readings[SENSOR_COUNT] = {24, 25, 26, 25, 24, 26, 25, 25, 24, 26, 25, 25};

int main(void)
{
    printf("sensor_app: %d readings\n", SENSOR_COUNT);
    printf("AVG=%d\n", sensor_average(readings, SENSOR_COUNT));
    return 0;
}
