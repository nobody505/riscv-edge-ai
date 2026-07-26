/*
 * WS2812B GPIO 位翻版 - Pin 40 (GPIO37) 最终版
 * 编译: gcc -O2 -o /tmp/ws2812_gpio ws2812_gpio.c
 *
 * GPIO @ 0xd4019000 (无 O_SYNC — 关键!):
 *   +0x1c = SET 寄存器 (写 bit 置 HIGH)
 *   +0x28 = CLEAR 寄存器 (写 bit 置 LOW)
 *   GPIO37 = bit 5
 *
 * 实测 SET+CLEAR 一对 = 7 ticks (无 fence)
 * 目标每 bit 1250ns = 30 ticks@24MHz
 * 剩余可用: 30 - 7 = 23 ticks 用于等待
 *
 * 1-bit: 800ns(19t) HIGH + 450ns(11t) LOW
 *   扣除 SET/CLEAR 写开销, 实际等待: HIGH≈16t, LOW≈7t
 * 0-bit: 400ns(10t) HIGH + 850ns(20t) LOW
 *   实际等待: HIGH≈6t, LOW≈17t
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <stdint.h>
#include <sys/mman.h>
#include <sched.h>

#define LED_COUNT    60
#define LED_BRIGHTNESS 64    /* 25% of full channel value (255) */
#define GPIO_BASE    0xd4019000
#define GPIO_SET     (0x1c / 4)   /* 写1=置HIGH */
#define GPIO_CLEAR   (0x28 / 4)   /* 写1=置LOW */
#define BIT_MASK     (1u << 5)    /* GPIO37 = bit5 */

static volatile uint32_t *gpio;
static unsigned char leds[LED_COUNT * 3];

static inline unsigned long rdtime(void) {
    unsigned long t;
    asm volatile("rdtime %0" : "=r"(t));
    return t;
}

static unsigned char scale_brightness(int value) {
    if (value <= 0) return 0;
    if (value >= 255) value = 255;
    return (unsigned char)((value * LED_BRIGHTNESS + 127) / 255);
}

static void fill(int r, int g, int b) {
    for (int i = 0; i < LED_COUNT; i++) {
        leds[i*3+0] = scale_brightness(g);
        leds[i*3+1] = scale_brightness(r);
        leds[i*3+2] = scale_brightness(b);
    }
}

static void send_byte(unsigned char v) {
    for (int b = 7; b >= 0; b--) {
        if ((v >> b) & 1) {
            /* 1-bit: 800ns HIGH + 450ns LOW */
            gpio[GPIO_SET] = BIT_MASK;
            unsigned long t = rdtime() + 20;
            while (rdtime() < t) {}
            gpio[GPIO_CLEAR] = BIT_MASK;
            t = rdtime() + 10;
            while (rdtime() < t) {}
        } else {
            /* 0-bit: 400ns HIGH + 850ns LOW */
            gpio[GPIO_SET] = BIT_MASK;
            unsigned long t = rdtime() + 9;
            while (rdtime() < t) {}
            gpio[GPIO_CLEAR] = BIT_MASK;
            t = rdtime() + 21;
            while (rdtime() < t) {}
        }
    }
}

static void flush(void) {
    gpio[GPIO_CLEAR] = BIT_MASK;
    usleep(300);

    unsigned long t0 = rdtime();
    for (int i = 0; i < LED_COUNT * 3; i++)
        send_byte(leds[i]);
    unsigned long t1 = rdtime();

    gpio[GPIO_CLEAR] = BIT_MASK;
    usleep(300);

    printf("  ticks: %lu total, %.1f/bit (target 30)\n",
           t1 - t0, (double)(t1 - t0) / (LED_COUNT * 24));
}

int main(int argc, char *argv[]) {
    int r = 0, g = 0, b = 0;

    if (argc == 2 && strcmp(argv[1], "--brightness-self-test") == 0) {
        if (scale_brightness(0) != 0 ||
                scale_brightness(128) != 32 ||
                scale_brightness(255) != LED_BRIGHTNESS) {
            fputs("BRIGHTNESS_SELF_TEST_FAILED\n", stderr);
            return 1;
        }
        puts("WS2812_BRIGHTNESS_SELF_TEST_OK");
        return 0;
    }

    struct sched_param sp = { .sched_priority = 99 };
    sched_setscheduler(0, SCHED_FIFO, &sp);
    cpu_set_t cpuset; CPU_ZERO(&cpuset); CPU_SET(0, &cpuset);
    sched_setaffinity(0, sizeof(cpuset), &cpuset);

    if (argc < 2) {
        printf("Usage: %s [green|red|blue|white|off]|<R> <G> <B>\n", argv[0]);
        return 1;
    }
    if (strcmp(argv[1], "green") == 0)  {r=0; g=255; b=0;}
    else if (strcmp(argv[1], "red")==0) {r=255; g=0; b=0;}
    else if (strcmp(argv[1], "blue")==0){r=0; g=0; b=255;}
    else if (strcmp(argv[1], "white")==0){r=255;g=255;b=255;}
    else if (strcmp(argv[1], "off")==0) {r=0; g=0; b=0;}
    else if (argc >= 4) {r=atoi(argv[1]); g=atoi(argv[2]); b=atoi(argv[3]);}

    /* MMIO — 无 O_SYNC！这是关键！ */
    int fd = open("/dev/mem", O_RDWR);
    if (fd < 0) { perror("/dev/mem"); return 1; }
    gpio = mmap(NULL, 0x1000, PROT_READ|PROT_WRITE, MAP_SHARED, fd, GPIO_BASE);
    close(fd);
    if (gpio == MAP_FAILED) { perror("mmap"); return 1; }

    /* pinctrl: GPIO37→GPIO 模式 (mux=0) */
    {
        int fd2 = open("/dev/mem", O_RDWR);
        volatile uint32_t *pinctrl = mmap(NULL, 0x1000, PROT_READ|PROT_WRITE, MAP_SHARED, fd2, 0xd401e000);
        close(fd2);
        pinctrl[0xd4/4] = 0xc440;
    }

    /* sysfs 导出 GPIO37 并设方向输出 */
    {
        FILE *f = fopen("/sys/class/gpio/gpio37/direction", "w");
        if (!f) {
            /* 未导出，先导出 */
            f = fopen("/sys/class/gpio/export", "w");
            if (!f) { perror("gpio export"); return 1; }
            fprintf(f, "37"); fclose(f);
            usleep(100000);
            f = fopen("/sys/class/gpio/gpio37/direction", "w");
            if (!f) { perror("gpio37 direction"); return 1; }
        }
        fprintf(f, "out"); fclose(f);
    }

    /* 确保初始为 LOW */
    gpio[GPIO_CLEAR] = BIT_MASK;

    printf("[OK] GPIO37 bit-bang (no O_SYNC, no fence)\n");

    fill(r, g, b);
    printf("Sending R=%d G=%d B=%d...\n", r, g, b);
    flush();
    printf("Done.\n");
    return 0;
}
