/* Emulated-time cost of the crypto a TLS session to an MQTT broker spends. */
#include <stdio.h>
#include <string.h>
#include "esp_timer.h"
#include "esp_random.h"
#include "mbedtls/ecp.h"
#include "mbedtls/ecdh.h"
#include "mbedtls/ecdsa.h"
#include "mbedtls/bignum.h"
#include "mbedtls/sha256.h"
#include "mbedtls/aes.h"

static int rng(void *ctx, unsigned char *out, size_t len) { (void)ctx; esp_fill_random(out, len); return 0; }
static int64_t t0;
static void start(void) { t0 = esp_timer_get_time(); }
static void stop(const char *what, int n) {
    int64_t us = esp_timer_get_time() - t0;
    printf("BENCH %-28s %4d x %10.1f ms\n", what, n, us / 1000.0 / n);
}

void app_main(void) {
    printf("BENCH begin %lld\n", (long long)esp_timer_get_time());
    static unsigned char buf[16384];
    esp_fill_random(buf, sizeof buf);

    unsigned char digest[32];
    start();
    for (int i = 0; i < 64; i++) mbedtls_sha256(buf, sizeof buf, digest, 0);
    stop("sha256 16KiB", 64);

    mbedtls_aes_context aes; mbedtls_aes_init(&aes);
    unsigned char key[16] = {0}, iv[16] = {0};
    mbedtls_aes_setkey_enc(&aes, key, 128);
    start();
    for (int i = 0; i < 16; i++) mbedtls_aes_crypt_cbc(&aes, MBEDTLS_AES_ENCRYPT, sizeof buf, iv, buf, buf);
    stop("aes128-cbc 16KiB", 16);

    mbedtls_ecp_group grp; mbedtls_ecp_group_init(&grp);
    mbedtls_ecp_group_load(&grp, MBEDTLS_ECP_DP_SECP256R1);
    mbedtls_mpi d, z; mbedtls_ecp_point q, qp;
    mbedtls_mpi_init(&d); mbedtls_mpi_init(&z); mbedtls_ecp_point_init(&q); mbedtls_ecp_point_init(&qp);
    start();
    for (int i = 0; i < 4; i++) mbedtls_ecp_gen_keypair(&grp, &d, &q, rng, NULL);
    stop("p256 keygen", 4);
    mbedtls_mpi d2; mbedtls_mpi_init(&d2);
    mbedtls_ecp_gen_keypair(&grp, &d2, &qp, rng, NULL);
    start();
    for (int i = 0; i < 4; i++) mbedtls_ecdh_compute_shared(&grp, &z, &qp, &d, rng, NULL);
    stop("p256 ecdh shared", 4);
    mbedtls_mpi r, s; mbedtls_mpi_init(&r); mbedtls_mpi_init(&s);
    start();
    for (int i = 0; i < 4; i++) mbedtls_ecdsa_sign(&grp, &r, &s, &d, digest, 32, rng, NULL);
    stop("p256 ecdsa sign", 4);
    start();
    int bad = 0;
    for (int i = 0; i < 4; i++) bad |= mbedtls_ecdsa_verify(&grp, digest, 32, &q, &r, &s);
    stop(bad ? "p256 ecdsa verify FAILED" : "p256 ecdsa verify", 4);

    /* RSA-2048: a public operation (e = 65537) and a full-length private-size exponent. */
    mbedtls_mpi n, e, x, y, rr; mbedtls_mpi_init(&n); mbedtls_mpi_init(&e); mbedtls_mpi_init(&x); mbedtls_mpi_init(&y); mbedtls_mpi_init(&rr);
    mbedtls_mpi_fill_random(&n, 256, rng, NULL); mbedtls_mpi_set_bit(&n, 0, 1); mbedtls_mpi_set_bit(&n, 2047, 1);
    mbedtls_mpi_fill_random(&x, 255, rng, NULL);
    mbedtls_mpi_lset(&e, 65537);
    start();
    for (int i = 0; i < 8; i++) mbedtls_mpi_exp_mod(&y, &x, &e, &n, &rr);
    stop("rsa2048 public (e=65537)", 8);
    mbedtls_mpi_fill_random(&e, 256, rng, NULL);
    start();
    for (int i = 0; i < 2; i++) mbedtls_mpi_exp_mod(&y, &x, &e, &n, &rr);
    stop("rsa2048 private-size exp", 2);

    printf("BENCH end %lld\n", (long long)esp_timer_get_time());
}
