#include "nmea.h"

#include <string.h>

static int digit(char c) {
    return c >= '0' && c <= '9' ? c - '0' : -1;
}

static int hex_digit(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

/* '$', then the body, then '*' and two hex digits that are the XOR of every body character. */
static bool checksum_ok(const char *s) {
    if (s[0] != '$') return false;
    unsigned sum = 0;
    const char *p = s + 1;
    for (; *p != '\0' && *p != '*'; p++) sum ^= (unsigned char)*p;
    if (*p != '*') return false;
    int high = hex_digit(p[1]);
    int low = high < 0 ? -1 : hex_digit(p[2]);
    return low >= 0 && p[3] == '\0' && sum == (unsigned)(high << 4 | low);
}

/* Copies field n into out, a buffer of size bytes; field 0 is the address (GPGGA). Fields end
 * at ',' or '*'. False when the sentence has no field n or the field does not fit. */
static bool field(const char *s, int n, char *out, size_t size) {
    const char *p = s + 1;
    for (int i = 0; i < n; i++) {
        p = strpbrk(p, ",*");
        if (p == NULL || *p == '*') return false;
        p++;
    }
    size_t length = strcspn(p, ",*");
    if (length >= size) return false;
    memcpy(out, p, length);
    out[length] = '\0';
    return true;
}

/* A latitude (ddmm.mmmm, degrees 2 digits) or longitude (dddmm.mmmm, 3 digits) and its
 * hemisphere letter, in microdegrees rounded to the nearest, half up. Minutes take up to six
 * decimals. False for anything else, an empty field included. */
static bool coordinate(const char *value, const char *hemisphere, int degree_digits,
                       char positive, char negative, int32_t *out) {
    const char *dot = strchr(value, '.');
    int whole = dot != NULL ? (int)(dot - value) : (int)strlen(value);
    if (whole != degree_digits + 2) return false;
    int32_t degrees = 0;
    int32_t minutes_e6 = 0; /* millionths of a minute */
    for (int i = 0; i < whole; i++) {
        int d = digit(value[i]);
        if (d < 0) return false;
        if (i < degree_digits) degrees = degrees * 10 + d;
        else minutes_e6 = minutes_e6 * 10 + d;
    }
    int decimals = 0;
    for (const char *p = dot != NULL ? dot + 1 : ""; *p != '\0'; p++, decimals++) {
        int d = digit(*p);
        if (d < 0 || decimals == 6) return false;
        minutes_e6 = minutes_e6 * 10 + d;
    }
    for (; decimals < 6; decimals++) minutes_e6 *= 10;
    int32_t limit = degree_digits == 2 ? 90 : 180;
    if (minutes_e6 >= 60000000 || degrees > limit || (degrees == limit && minutes_e6 > 0)) {
        return false;
    }
    int32_t e6 = degrees * 1000000 + (minutes_e6 + 30) / 60;
    if (hemisphere[0] == positive && hemisphere[1] == '\0') *out = e6;
    else if (hemisphere[0] == negative && hemisphere[1] == '\0') *out = -e6;
    else return false;
    return true;
}

bool nmea_parse_fix(const char *sentence, nmea_fix_t *fix) {
    char address[6], status[2], lat[16], ns[2], lon[16], ew[2];
    nmea_fix_t parsed;
    if (strlen(sentence) > NMEA_SENTENCE_MAX - 2 || !checksum_ok(sentence)) return false;
    if (!field(sentence, 0, address, sizeof address) || strlen(address) != 5) return false;
    /* The fields after the time: GGA has the position and then the fix quality, RMC the status
     * and then the position. */
    int position;
    if (strcmp(address + 2, "GGA") == 0) {
        if (!field(sentence, 6, status, sizeof status) || status[0] == '\0' || status[0] == '0') {
            return false;
        }
        position = 2;
    } else if (strcmp(address + 2, "RMC") == 0) {
        if (!field(sentence, 2, status, sizeof status) || strcmp(status, "A") != 0) return false;
        position = 3;
    } else {
        return false;
    }
    if (!field(sentence, 1, parsed.time, sizeof parsed.time) || strlen(parsed.time) < 6) {
        return false;
    }
    for (int i = 0; i < 6; i++) {
        if (digit(parsed.time[i]) < 0) return false;
    }
    if (!field(sentence, position, lat, sizeof lat) ||
        !field(sentence, position + 1, ns, sizeof ns) ||
        !field(sentence, position + 2, lon, sizeof lon) ||
        !field(sentence, position + 3, ew, sizeof ew) ||
        !coordinate(lat, ns, 2, 'N', 'S', &parsed.lat_e6) ||
        !coordinate(lon, ew, 3, 'E', 'W', &parsed.lon_e6)) {
        return false;
    }
    *fix = parsed;
    return true;
}
