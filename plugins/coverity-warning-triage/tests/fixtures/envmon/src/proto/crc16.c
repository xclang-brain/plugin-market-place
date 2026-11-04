/**
 * @file    crc16.c
 * @brief   CRC16-CCITT（上りリンク、フラッシュ）と CRC8（Sensirion）
 *
 * CRC16 は表を使う。表（512 バイト）は最初に呼ばれたときに RAM に作る。
 * 確認用の値: "123456789" → 0x29B1
 */
#include "proto.h"

#define CRC16_POLY      (0x1021U)
#define CRC16_INIT      (0xFFFFU)
#define CRC8_POLY       (0x31U)
#define CRC8_INIT       (0xFFU)

static uint16_t s_crc16_table[256];
static bool     s_crc16_ready;

static void crc16_build_table(void)
{
    uint32_t i;
    uint32_t bit;

    for (i = 0U; i < 256U; i++) {
        uint16_t crc = (uint16_t)(i << 8);
        for (bit = 0U; bit < 8U; bit++) {
            if ((crc & 0x8000U) != 0U) {
                crc = (uint16_t)((uint16_t)(crc << 1) ^ CRC16_POLY);
            } else {
                crc = (uint16_t)(crc << 1);
            }
        }
        s_crc16_table[i] = crc;
    }
    s_crc16_ready = true;
}

uint16_t crc16_ccitt_update(uint16_t crc, const uint8_t *data, size_t len)
{
    size_t i;

    if (!s_crc16_ready) {
        crc16_build_table();
    }
    if (data == NULL) {
        return crc;
    }
    for (i = 0U; i < len; i++) {
        uint8_t idx = (uint8_t)((crc >> 8) ^ data[i]);
        crc = (uint16_t)((uint16_t)(crc << 8) ^ s_crc16_table[idx]);
    }
    return crc;
}

uint16_t crc16_ccitt(const uint8_t *data, size_t len)
{
    return crc16_ccitt_update((uint16_t)CRC16_INIT, data, len);
}

/* Sensirion SHT3x のデータシート 4.12 の CRC8 */
uint8_t crc8_sensirion(const uint8_t *data, size_t len)
{
    uint8_t crc = CRC8_INIT;
    size_t  i;
    uint8_t bit;

    for (i = 0U; i < len; i++) {
        crc ^= data[i];
        for (bit = 0U; bit < 8U; bit++) {
            if ((crc & 0x80U) != 0U) {
                crc = (uint8_t)((uint8_t)(crc << 1) ^ CRC8_POLY);
            } else {
                crc = (uint8_t)(crc << 1);
            }
        }
    }
    return crc;
}
