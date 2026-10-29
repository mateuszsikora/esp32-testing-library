/* Emulated-time cost of the crypto a TLS client spends.
 *
 * Each phase prints `BENCH begin <name>` and `BENCH end <name>: ...`, so a host reading the console can
 * time the same phase in wall time (tools/benchtime.py), then `BENCH result <name> <hash>`. The hash
 * is computed after the phase's clock has stopped and after its end line is out, so neither the
 * emulated nor the wall time includes it. Every phase runs long enough (at least 100 ms emulated with
 * the accelerators) for the wall timing to mean something.
 *
 * What is checked, and how independently:
 * - every mbedTLS call: a failing call aborts the run instead of timing its error path;
 * - SHA-256 and AES-CBC against published vectors (FIPS 180-2, NIST SP 800-38A), multi-block;
 * - ECDH from both sides, ECDSA verify accepting its own signature and rejecting a changed digest,
 *   and x^65537 against square-and-multiply: consistency checks, through the same code;
 * - every phase's result but ECDSA verify's, whose result is the return code CHECK already tests,
 *   hashed in software (FNV-1a) and printed. Inputs come from a fixed seed,
 *   so a build with the accelerators and one without (sdkconfig.defaults.nohw) must print the same
 *   hashes: that comparison is the independent check of the accelerator models. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_cpu.h"
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

/* Deterministic inputs: xorshift64* from a fixed seed, for data and for mbedTLS's RNG alike. */
static uint64_t seed = 0x9e3779b97f4a7c15ULL;
static uint8_t next_byte(void) {
    seed ^= seed >> 12, seed ^= seed << 25, seed ^= seed >> 27;
    return (uint8_t)((seed * 0x2545f4914f6cdd1dULL) >> 56);
}
static void fill(unsigned char *out, size_t len) {
    for (size_t i = 0; i < len; i++)
        out[i] = next_byte();
}
static int rng(void *ctx, unsigned char *out, size_t len) {
    (void)ctx;
    fill(out, len);
    return 0;
}

/* FNV-1a, in software, so a result's hash never passes through an accelerator. */
static uint64_t fnv(uint64_t h, const unsigned char *p, size_t len) {
    for (size_t i = 0; i < len; i++)
        h = (h ^ p[i]) * 0x100000001b3ULL;
    return h;
}
static const uint64_t FNV0 = 0xcbf29ce484222325ULL;
static uint64_t fnv_mpi(uint64_t h, const mbedtls_mpi *x) {
    unsigned char b[256];
    CHECK(mbedtls_mpi_write_binary(x, b, sizeof b));
    return fnv(h, b, sizeof b);
}

static int64_t t0;
static uint32_t c0; /* CCOUNT is 32 bits: it wraps after 2^32 cycles, 26.8 s at 160 MHz */
static const char *phase;
static void begin(const char *name) {
    phase = name;
    printf("BENCH begin %s\n", name);
    c0 = esp_cpu_get_cycle_count();
    t0 = esp_timer_get_time();
}
/* Stops the phase's clock and prints its timing at once: the host stamps this line. */
static void end(int n) {
    int64_t us = esp_timer_get_time() - t0;
    uint32_t cycles = esp_cpu_get_cycle_count() - c0;
    printf("BENCH end %s: %d x %.1f us, %lu cycles total\n", phase, n, (double)us / n,
           (unsigned long)cycles);
}
/* After end(): the result's hash, computed outside the timed window. */
static void result(uint64_t hash) {
    printf("BENCH result %s %016llx\n", phase, (unsigned long long)hash);
}

static void from_hex(const char *hex, unsigned char *out, size_t len) {
    for (size_t i = 0; i < len; i++)
        sscanf(hex + 2 * i, "%2hhx", &out[i]);
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
    result(sink);

    /* SHA-256 against FIPS 180-2: one block, two blocks, and a million bytes streamed. */
    unsigned char digest[32], want[32];
    CHECK(mbedtls_sha256((const unsigned char *)"abc", 3, digest, 0));
    from_hex("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", want, 32);
    EXPECT(memcmp(digest, want, 32) == 0, "sha256 FIPS 180-2 one-block vector");
    const char *two = "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";
    CHECK(mbedtls_sha256((const unsigned char *)two, strlen(two), digest, 0));
    from_hex("248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1", want, 32);
    EXPECT(memcmp(digest, want, 32) == 0, "sha256 FIPS 180-2 two-block vector");
    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    CHECK(mbedtls_sha256_starts(&sha, 0));
    memset(buf, 'a', 1000);
    for (int i = 0; i < 1000; i++)
        CHECK(mbedtls_sha256_update(&sha, buf, 1000));
    CHECK(mbedtls_sha256_finish(&sha, digest));
    mbedtls_sha256_free(&sha);
    from_hex("cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0", want, 32);
    EXPECT(memcmp(digest, want, 32) == 0, "sha256 FIPS 180-2 million-a vector");

    static const size_t sizes[] = {1024, 16384, 65536};
    static const int sha_iters[] = {1000, 100, 25}, aes_iters[] = {1500, 100, 25};
    fill(buf, sizeof buf);
    for (int s = 0; s < 3; s++) {
        char name[32];
        snprintf(name, sizeof name, "sha256-%uKiB", (unsigned)(sizes[s] / 1024));
        begin(name);
        for (int i = 0; i < sha_iters[s]; i++)
            CHECK(mbedtls_sha256(buf, sizes[s], digest, 0));
        end(sha_iters[s]);
        result(fnv(FNV0, digest, 32));
    }

    /* AES-128-CBC against NIST SP 800-38A F.2.1 (four blocks), then timed at three sizes: each pass
     * encrypts the same plaintext from a zero IV; the last ciphertext is decrypted and compared. */
    mbedtls_aes_context aes;
    unsigned char key[16], iv[16], vec[64], out[64];
    from_hex("2b7e151628aed2a6abf7158809cf4f3c", key, 16);
    from_hex("000102030405060708090a0b0c0d0e0f", iv, 16);
    from_hex("6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51"
             "30c81c46a35ce411e5fbc1191a0a52eff69f2445df4f9b17ad2b417be66c3710",
             vec, 64);
    mbedtls_aes_init(&aes);
    CHECK(mbedtls_aes_setkey_enc(&aes, key, 128));
    CHECK(mbedtls_aes_crypt_cbc(&aes, MBEDTLS_AES_ENCRYPT, 64, iv, vec, out));
    mbedtls_aes_free(&aes);
    from_hex("7649abac8119b246cee98e9b12e9197d5086cb9b507219ee95db113a917678b2"
             "73bed6b8e3c1743b7116e69e222295163ff1caa1681fac09120eca307586e1a7",
             vec, 64);
    EXPECT(memcmp(out, vec, 64) == 0, "aes-128-cbc NIST SP 800-38A F.2.1 vector");

    fill(key, sizeof key);
    fill(copy, sizeof copy); /* plaintext; `buf` receives the ciphertext */
    for (int s = 0; s < 3; s++) {
        char name[32];
        snprintf(name, sizeof name, "aes128cbc-%uKiB", (unsigned)(sizes[s] / 1024));
        mbedtls_aes_init(&aes);
        CHECK(mbedtls_aes_setkey_enc(&aes, key, 128));
        begin(name);
        for (int i = 0; i < aes_iters[s]; i++) {
            memset(iv, 0, sizeof iv);
            CHECK(mbedtls_aes_crypt_cbc(&aes, MBEDTLS_AES_ENCRYPT, sizes[s], iv, copy, buf));
        }
        end(aes_iters[s]);
        result(fnv(FNV0, buf, sizes[s]));
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
    result(fnv_mpi(fnv_mpi(FNV0, &qa.MBEDTLS_PRIVATE(X)), &qa.MBEDTLS_PRIVATE(Y)));
    CHECK(mbedtls_ecp_gen_keypair(&grp, &db, &qb, rng, NULL));
    begin("p256-ecdh");
    for (int i = 0; i < 4; i++)
        CHECK(mbedtls_ecdh_compute_shared(&grp, &za, &qb, &da, rng, NULL));
    end(4);
    result(fnv_mpi(FNV0, &za));
    CHECK(mbedtls_ecdh_compute_shared(&grp, &zb, &qa, &db, rng, NULL));
    EXPECT(mbedtls_mpi_cmp_mpi(&za, &zb) == 0, "ecdh shared secret equal from both sides");
    CHECK(mbedtls_sha256(buf, 1024, digest, 0));
    begin("p256-ecdsa-sign");
    for (int i = 0; i < 4; i++)
        CHECK(mbedtls_ecdsa_sign(&grp, &r, &s, &da, digest, 32, rng, NULL));
    end(4);
    result(fnv_mpi(fnv_mpi(FNV0, &r), &s));
    begin("p256-ecdsa-verify");
    for (int i = 0; i < 4; i++)
        CHECK(mbedtls_ecdsa_verify(&grp, digest, 32, &qa, &r, &s));
    end(4); /* the result is each call's return code, which CHECK has tested */
    digest[0] ^= 1;
    EXPECT(mbedtls_ecdsa_verify(&grp, digest, 32, &qa, &r, &s) != 0, "ecdsa rejects a changed digest");

    /* RSA-2048-sized modular exponentiation. The modulus is a random odd 2048-bit number, not a
     * product of two primes: enough to time the arithmetic, not a key. The public operation uses
     * e = 65537 and is checked against squaring and multiplying by hand. The second phase uses a
     * full 2048-bit exponent without CRT, which is several times dearer than a real RSA-2048
     * signature (two 1024-bit exponentiations with CRT). */
    mbedtls_mpi n, e, x, y, check, rr;
    mbedtls_mpi_init(&n), mbedtls_mpi_init(&e), mbedtls_mpi_init(&x), mbedtls_mpi_init(&y);
    mbedtls_mpi_init(&check), mbedtls_mpi_init(&rr);
    CHECK(mbedtls_mpi_fill_random(&n, 256, rng, NULL));
    CHECK(mbedtls_mpi_set_bit(&n, 0, 1));
    CHECK(mbedtls_mpi_set_bit(&n, 2047, 1));
    CHECK(mbedtls_mpi_fill_random(&x, 255, rng, NULL));
    CHECK(mbedtls_mpi_lset(&e, 65537));
    /* With the same modulus, mbedTLS computes R^2 mod n once and reuses it from `rr`. A TLS client
     * verifies each certificate under a different modulus, so the first phase discards `rr` before
     * every call, which costs what a new modulus costs: that is one verification. The second keeps
     * it. On the ESP32, IDF computes R^2 mod n in software (esp_bignum.c, calculate_rinv). */
    begin("rsa2048-public-e65537-fresh-rr");
    for (int i = 0; i < 20; i++) {
        mbedtls_mpi_free(&rr);
        mbedtls_mpi_init(&rr);
        CHECK(mbedtls_mpi_exp_mod(&y, &x, &e, &n, &rr));
    }
    end(20);
    result(fnv_mpi(FNV0, &y));
    begin("rsa2048-public-e65537-kept-rr");
    for (int i = 0; i < 400; i++)
        CHECK(mbedtls_mpi_exp_mod(&y, &x, &e, &n, &rr));
    end(400);
    result(fnv_mpi(FNV0, &y));
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
    for (int i = 0; i < 3; i++)
        CHECK(mbedtls_mpi_exp_mod(&y, &x, &e, &n, &rr));
    end(3);
    result(fnv_mpi(FNV0, &y));

    printf("BENCH done, all checks passed\n");
}
