/*
 * MAX30102 心率+血氧 — 原始稳定版（微调）
 * - IR/RED 原始 bit 移位
 * - BPM 从 t=0 开始显示（初始72，自动波动到真实值）
 * - SpO2 始终显示（无信号时 95-100 波动）
 * - 真实心跳检测：arm阈值 -100
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <stdint.h>
#include <math.h>
#include <sys/ioctl.h>
#include <time.h>
#include <signal.h>
#include <linux/i2c-dev.h>

#define ADDR    0x57
#define FIFO_WR 0x04
#define FIFO_RD 0x06
#define FIFO_D  0x07
#define FIFO_C  0x08
#define MODE    0x09
#define SPO2_C  0x0A
#define LED1    0x0C
#define LED2    0x0D
#define PART_ID 0xFF

static int fd_i2c;
static volatile int run = 1;
static void done(int s) { run = 0; (void)s; }

static void w8(uint8_t r, uint8_t v) { uint8_t b[2]={r,v}; write(fd_i2c,b,2); }
static uint8_t r8(uint8_t r) { uint8_t v=0; write(fd_i2c,&r,1); read(fd_i2c,&v,1); return v; }
static void rd6(uint8_t *b) { uint8_t r=FIFO_D; write(fd_i2c,&r,1); read(fd_i2c,b,6); }

int main(void) {
    signal(SIGINT, done); signal(SIGTERM, done);

    fd_i2c = open("/dev/i2c-4", O_RDWR);
    ioctl(fd_i2c, I2C_SLAVE, ADDR);
    printf("PartID: 0x%02X", r8(PART_ID));
    if (r8(PART_ID) == 0x15) printf(" (MAX30102 OK)\n");
    else printf(" (WARN: expected 0x15)\n");

    w8(MODE, 0x40); usleep(2000);
    w8(FIFO_C, 0x4F);
    w8(MODE, 0x03);
    w8(SPO2_C, 0x4F);
    w8(LED1, 0x3F);
    w8(LED2, 0x3F);
    printf("Init OK\n");

    { uint8_t b[6]; for(int i=0;i<32;i++) rd6(b); }

    int beats = 0;
    /* 无有效信号时保持 NAN，绝不生成可被误认为真实测量的随机数。 */
    float bpm = NAN;
    float spo2 = NAN;
    float dc_ir = 0, dc_red = 0;
    int armed = 0;
    float prev = 0;

    /* SpO2: 存最近4秒的IR/RED AC值 */
    #define SPO2_N (200*4)
    float ir_buf[SPO2_N], red_buf[SPO2_N];
    int bi = 0, bn = 0;

    struct timespec t0, tn;
    clock_gettime(CLOCK_MONOTONIC, &t0);

    printf("%-8s %-8s %-8s %-10s %-10s %-8s\n",
           "Time", "BPM", "SpO2", "IR_DC", "RED_DC", "Beats");

    while (run) {
        uint8_t wr = r8(FIFO_WR), rd = r8(FIFO_RD);
        int n = (wr >= rd) ? (wr - rd) : (32 + wr - rd);
        if (n == 0) { usleep(5000); continue; }

        for (int i = 0; i < n && i < 32; i++) {
            uint8_t b[6]; rd6(b);
            uint32_t ir  = ((uint32_t)(b[0]&0xC0)<<12)|((uint32_t)b[1]<<8)|b[2];
            uint32_t red = ((uint32_t)(b[0]&0x30)<<14)|((uint32_t)b[3]<<8)|b[4];

            dc_ir  = dc_ir  * 0.999f + (float)ir  * 0.001f;
            dc_red = dc_red * 0.999f + (float)red * 0.001f;
            float ac_i_val = (float)ir - dc_ir;
            float ac_r = (float)red - dc_red;

            /* 心率: IR上升沿 */
            if (armed && ac_i_val > 0 && prev <= 0) { beats++; armed = 0; }
            if (ac_i_val < -100) armed = 1;
            prev = ac_i_val;

            /* SpO2: 存AC值 */
            ir_buf[bi] = ac_i_val;
            red_buf[bi] = ac_r;
            bi = (bi + 1) % SPO2_N;
            if (bn < SPO2_N) bn++;
        }

        clock_gettime(CLOCK_MONOTONIC, &tn);
        double t = (tn.tv_sec-t0.tv_sec) + (tn.tv_nsec-t0.tv_nsec)/1e9;
        static double last = 0;
        if (t - last < 1.0) continue;
        last = t;

        /* 心率：只有真实节拍落在合理范围内才输出。 */
        if (t > 2) {
            float raw = beats / (t / 60.0f);
            if (raw >= 70 && raw <= 100) bpm = raw;
            else bpm = NAN;
        }

        /* 血氧: 每4秒算一次 */
        static double last_spo2 = -100;
        if (t - last_spo2 >= 4.0 && bn >= SPO2_N/2 && dc_ir > 100 && dc_red > 100) {
            last_spo2 = t;
            float ir_rms=0, red_rms=0;
            int nn = bn < SPO2_N ? bn : SPO2_N;
            for (int j=0; j<nn; j++) { ir_rms+=ir_buf[j]*ir_buf[j]; red_rms+=red_buf[j]*red_buf[j]; }
            ir_rms  = sqrtf(ir_rms/nn);
            red_rms = sqrtf(red_rms/nn);
            if (ir_rms > 10 && red_rms > 10) {
                float R = (red_rms/dc_red) / (ir_rms/dc_ir);
                float s = 110.0f - 25.0f*R;
                if (s > 100) s = 100;
                if (s < 90) s = 90;
                if (!isfinite(spo2)) spo2 = s;
                else spo2 = spo2*0.6f + s*0.4f;
            }
        }

        printf("\r%-8.0f %-8.1f %-8.1f %-10.0f %-10.0f %-8d",
               t, bpm, spo2, dc_ir, dc_red, beats);
        fflush(stdout);
    }

    {
        struct timespec tn2;
        clock_gettime(CLOCK_MONOTONIC, &tn2);
        double t = (tn2.tv_sec-t0.tv_sec)+(tn2.tv_nsec-t0.tv_nsec)/1e9;
        printf("\nDone: %ds %d beats BPM=%.1f SpO2=%.1f%%\n",
               (int)t, beats, bpm, spo2);
    }
    w8(MODE, 0x80);
    close(fd_i2c);
    return 0;
}
