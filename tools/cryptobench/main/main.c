/* Emulated-time cost of the crypto a TLS client spends, with every call checked.
 *
 * Each phase prints `BENCH begin <name>` and `BENCH end <name>`, so a host reading the console can
 * time the same phase in wall time. Every mbedTLS call is checked: a failing call aborts the run
 * instead of timing its error path. Every primitive also produces a result that is checked against
 * an independent computation, so a fast but wrong accelerator model cannot pass as a fast one. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_cpu.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "mbedtls/aes.h"
#include "mbedtls/bignum.h"
#include "mbedtls/ecdh.h"
#include "mbedtls/ecdsa.h"
#include "mbedtls/ecp.h"
#include "mbedtls/sha256.h"

#define CHECK(x)                                                                                   \
    do {                                                                                           \
        int rc_ = (x);                                                                             \
        if (rc_) {                                                                                 \
            printf("BENCH FAIL %s -> -0x%04x\n", #x, (unsigned)-rc_);                              \
            abort();                                                                               \
        }                                                                                          \
    } while (0)
#define EXPECT(cond, what)                                                                         \
    do {                                                                                           \
        if (!(cond)) {                                                                             \
            printf("BENCH FAIL %s\n", what);                                                       \
            abort();                                                                               \
        }                                                                                          \
    } while (0)

static int rng(void *ctx, unsigned char *out, size_t len) {
    (void)ctx;
    esp_fill_random(out, len);
    return 0;
}

static int64_t t0;
static uint32_t c0;
static const char *phase;
static void begin(const char *name) {
    phase = name;
    printf("BENCH begin %s\n", name);
    c0 = esp_cpu_get_cycle_count();
    t0 = esp_timer_get_time();
}
static void end(int n) {
    int64_t us = esp_timer_get_time() - t0;
    uint32_t cycles = esp_cpu_get_cycle_count() - c0;
    printf("BENCH end %s: %d x %.1f us, %lu cycles total\n", phase, n, (double)us / n,
           (unsigned long)cycles);
}

static unsigned char buf[65536], copy[65536];

void app_main(void) {
    printf("BENCH start, CPU at %d MHz\n", CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ);

    /* Calibration: a fixed number of iterations of a loop the compiler cannot remove, timed in
     * emulated microseconds and in CPU cycles. Ties emulated time to the instructions executed. */
    volatile uint32_t sink = 0;
    begin("calibrate-10M-iterations");
    for (uint32_t i = 0; i < 10000000; i++)
        sink += i;
    end(1);

    /* SHA-256: a known-answer test first, then three sizes, to see whether time grows with size. */
    static const unsigned char abc_digest[32] = {
        0xba, 0x78, 0x16, 0xbf, 0x8f, 0x01, 0xcf, 0xea, 0x41, 0x41, 0x40, 0xde, 0x5d, 0xae, 0x22, 0x23,
        0xb0, 0x03, 0x61, 0xa3, 0x96, 0x17, 0x7a, 0x9c, 0xb4, 0x10, 0xff, 0x61, 0xf2, 0x00, 0x15, 0xad};
    unsigned char digest[32];
    CHECK(mbedtls_sha256((const unsigned char *)"abc", 3, digest, 0));
    EXPECT(memcmp(digest, abc_digest, 32) == 0, "sha256(\"abc\") known answer");
    esp_fill_random(buf, sizeof buf);
    static const size_t sizes[] = {1024, 16384, 65536};
    for (int s = 0; s < 3; s++) {
        char name[32];
        snprintf(name, sizeof name, "sha256-%uKiB", (unsigned)(sizes[s] / 1024));
        begin(name);
        for (int i = 0; i < 16; i++)
            CHECK(mbedtls_sha256(buf, sizes[s], digest, 0));
        end(16);
    }

    /* AES-128-CBC at the same three sizes: each pass encrypts the same plaintext from a zero IV, then
     * the last ciphertext is decrypted and compared with the plaintext. */
    mbedtls_aes_context aes;
    unsigned char key[16], iv[16];
    esp_fill_random(key, sizeof key);
    esp_fill_random(copy, sizeof copy); /* plaintext; `buf` receives the ciphertext */
    for (int s = 0; s < 3; s++) {
        char name[32];
        snprintf(name, sizeof name, "aes128cbc-%uKiB", (unsigned)(sizes[s] / 1024));
        mbedtls_aes_init(&aes);
        CHECK(mbedtls_aes_setkey_enc(&aes, key, 128));
        begin(name);
        for (int i = 0; i < 4; i++) {
            memset(iv, 0, sizeof iv);
            CHECK(mbedtls_aes_crypt_cbc(&aes, MBEDTLS_AES_ENCRYPT, sizes[s], iv, copy, buf));
        }
        end(4);
        EXPECT(memcmp(buf, copy, sizes[s]) != 0, "aes ciphertext differs from plaintext");
        CHECK(mbedtls_aes_setkey_dec(&aes, key, 128));
        memset(iv, 0, sizeof iv);
        CHECK(mbedtls_aes_crypt_cbc(&aes, MBEDTLS_AES_DECRYPT, sizes[s], iv, buf, buf));
        mbedtls_aes_free(&aes);
        EXPECT(memcmp(buf, copy, sizes[s]) == 0, "aes decrypt(encrypt(x)) == x");
    }

    /* P-256: key generation, ECDH computed from both sides and compared, ECDSA sign and verify. */
    mbedtls_ecp_group grp;
    mbedtls_ecp_group_init(&grp);
    CHECK(mbedtls_ecp_group_load(&grp, MBEDTLS_ECP_DP_SECP256R1));
    mbedtls_mpi da, db, za, zb, r, s;
    mbedtls_ecp_point qa, qb;
    mbedtls_mpi_init(&da), mbedtls_mpi_init(&db), mbedtls_mpi_init(&za), mbedtls_mpi_init(&zb);
    mbedtls_mpi_init(&r), mbedtls_mpi_init(&s);
    mbedtls_ecp_point_init(&qa), mbedtls_ecp_point_init(&qb);
    begin("p256-keygen");
    for (int i = 0; i < 4; i++)
        CHECK(mbedtls_ecp_gen_keypair(&grp, &da, &qa, rng, NULL));
    end(4);
    CHECK(mbedtls_ecp_gen_keypair(&grp, &db, &qb, rng, NULL));
    begin("p256-ecdh");
    for (int i = 0; i < 4; i++)
        CHECK(mbedtls_ecdh_compute_shared(&grp, &za, &qb, &da, rng, NULL));
    end(4);
    CHECK(mbedtls_ecdh_compute_shared(&grp, &zb, &qa, &db, rng, NULL));
    EXPECT(mbedtls_mpi_cmp_mpi(&za, &zb) == 0, "ecdh shared secret equal from both sides");
    CHECK(mbedtls_sha256(buf, 1024, digest, 0));
    begin("p256-ecdsa-sign");
    for (int i = 0; i < 4; i++)
        CHECK(mbedtls_ecdsa_sign(&grp, &r, &s, &da, digest, 32, rng, NULL));
    end(4);
    begin("p256-ecdsa-verify");
    for (int i = 0; i < 4; i++)
        CHECK(mbedtls_ecdsa_verify(&grp, digest, 32, &qa, &r, &s));
    end(4);
    digest[0] ^= 1;
    EXPECT(mbedtls_ecdsa_verify(&grp, digest, 32, &qa, &r, &s) != 0, "ecdsa rejects a changed digest");

    /* RSA-2048-sized modular exponentiation. The modulus is a random odd 2048-bit number, not a
     * product of two primes: enough to time the arithmetic, not a key. The public operation uses
     * e = 65537. The second phase uses a full 2048-bit exponent without CRT, which is several times
     * dearer than a real RSA-2048 signature (two 1024-bit exponentiations with CRT). The public
     * result is checked against squaring and multiplying by hand. */
    mbedtls_mpi n, e, x, y, check, rr;
    mbedtls_mpi_init(&n), mbedtls_mpi_init(&e), mbedtls_mpi_init(&x), mbedtls_mpi_init(&y);
    mbedtls_mpi_init(&check), mbedtls_mpi_init(&rr);
    CHECK(mbedtls_mpi_fill_random(&n, 256, rng, NULL));
    CHECK(mbedtls_mpi_set_bit(&n, 0, 1));
    CHECK(mbedtls_mpi_set_bit(&n, 2047, 1));
    CHECK(mbedtls_mpi_fill_random(&x, 255, rng, NULL));
    CHECK(mbedtls_mpi_lset(&e, 65537));
    begin("rsa2048-public-e65537");
    for (int i = 0; i < 8; i++)
        CHECK(mbedtls_mpi_exp_mod(&y, &x, &e, &n, &rr));
    end(8);
    CHECK(mbedtls_mpi_copy(&check, &x));
    for (int i = 0; i < 16; i++) { /* x^(2^16) */
        CHECK(mbedtls_mpi_mul_mpi(&check, &check, &check));
        CHECK(mbedtls_mpi_mod_mpi(&check, &check, &n));
    }
    CHECK(mbedtls_mpi_mul_mpi(&check, &check, &x)); /* x^(2^16 + 1) = x^65537 */
    CHECK(mbedtls_mpi_mod_mpi(&check, &check, &n));
    EXPECT(mbedtls_mpi_cmp_mpi(&y, &check) == 0, "x^65537 mod n matches square-and-multiply");
    CHECK(mbedtls_mpi_fill_random(&e, 256, rng, NULL));
    begin("rsa2048-full-exponent-no-crt");
    for (int i = 0; i < 2; i++)
        CHECK(mbedtls_mpi_exp_mod(&y, &x, &e, &n, &rr));
    end(2);

    printf("BENCH done, all checks passed\n");
}
