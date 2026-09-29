#include <immintrin.h>

#define N 128

static double a[N][N];
static double b[N][N];
static double c[N][N];

int main(void) {
    for (int i = 0; i < N; i++) {
        for (int j = 0; j < N; j++) {
            a[i][j] = (double)((i * 17 + j * 13) % 97 + 1) / 97.0;
            b[i][j] = (double)((i * 29 + j * 7) % 89 + 1) / 89.0;
            c[i][j] = 0.0;
        }
    }

    for (;;) {
        for (int i = 0; i < N; i++) {
            for (int k = 0; k < N; k++) {
                const __m256d av = _mm256_set1_pd(a[i][k]);
                for (int j = 0; j < N; j += 4) {
                    const __m256d bv = _mm256_loadu_pd(&b[k][j]);
                    __m256d cv = _mm256_loadu_pd(&c[i][j]);
                    cv = _mm256_fmadd_pd(av, bv, cv);
                    _mm256_storeu_pd(&c[i][j], cv);
                }
            }
        }
        __asm__ volatile("" : : "m"(c) : "memory");
    }
}
