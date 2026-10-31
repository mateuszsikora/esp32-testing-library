/* The little of NMEA 0183 nmea_reader needs: a position fix from a GGA or RMC sentence. No IDF
 * dependency, so the parser can be read on its own. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* NMEA 0183 caps a sentence at 82 characters, '$' to the line feed. */
#define NMEA_SENTENCE_MAX 82

typedef struct {
    char time[16];  /* the UTC time field as sent, hhmmss.ss; one epoch's GGA and RMC share it */
    int32_t lat_e6; /* microdegrees, north positive */
    int32_t lon_e6; /* microdegrees, east positive */
} nmea_fix_t;

/* Parses one sentence, '$' to the checksum, without the line ending. Returns true and fills
 * *fix for a GGA with a fix quality other than 0, or an RMC with status A, of any talker ($GP,
 * $GN, ...) whose checksum is right and whose time and position fields are well formed. Any
 * other sentence returns false and leaves *fix as it was. */
bool nmea_parse_fix(const char *sentence, nmea_fix_t *fix);
