#ifndef SENSOR_H
#define SENSOR_H

#include <stdint.h>

#define SENSOR_COUNT 12

/* センサーの値の平均を返す */
int sensor_average(const uint8_t *values, int count);

#endif
