/*
 * HC-SR04 超声波测距 (Linux sysfs GPIO)
 * 编译: gcc -O2 -o hc_sr04 hc_sr04.c -lm
 *
 * 接线:
 *   VCC  -> 3.3V
 *   GND  -> GND
 *   Trig -> Pin13/GPIO72
 *   Echo -> Pin11/GPIO71
 *
 * 原理: Trig发10us脉冲 → 模块发8个40kHz超声波 → Echo高电平=飞行时间
 *       距离(cm) = Echo高电平时间(us) / 58
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <signal.h>
#include <poll.h>
#include <math.h>

#define TRIG_PIN    72
#define ECHO_PIN    71
#define TIMEOUT_US  50000  /* Echo超时 50ms (约8.5m, 实际HC-SR04最大4m) */

static int running = 1;

static void sig_handler(int sig) { running = 0; }

/* 导出GPIO并设方向 */
static int gpio_export(int pin, const char *dir)
{
    char path[64];
    int fd;

    /* 检查是否已导出 */
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/direction", pin);
    fd = open(path, O_WRONLY);
    if (fd < 0) {
        /* 未导出, 先导出 */
        fd = open("/sys/class/gpio/export", O_WRONLY);
        if (fd < 0) { perror("export"); return -1; }
        dprintf(fd, "%d", pin);
        close(fd);
        usleep(100000); /* 等sysfs创建设备节点 */
    } else {
        close(fd);
    }

    /* 设置方向 */
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/direction", pin);
    fd = open(path, O_WRONLY);
    if (fd < 0) { perror("direction"); return -1; }
    dprintf(fd, "%s", dir);
    close(fd);
    return 0;
}

/* 写GPIO值 */
static int gpio_write(int pin, int val)
{
    char path[64];
    int fd;
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/value", pin);
    fd = open(path, O_WRONLY);
    if (fd < 0) return -1;
    dprintf(fd, "%d", val);
    close(fd);
    return 0;
}

/* 打开value文件用于poll */
static int gpio_open_value(int pin)
{
    char path[64];
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/value", pin);
    return open(path, O_RDONLY);
}

/* 设置边沿触发 */
static int gpio_set_edge(int pin, const char *edge)
{
    char path[64];
    int fd;
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/edge", pin);
    fd = open(path, O_WRONLY);
    if (fd < 0) return -1;
    dprintf(fd, "%s", edge);
    close(fd);
    return 0;
}

/* 微秒级时间戳 (CLOCK_MONOTONIC) */
static inline double micros(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000000.0 + ts.tv_nsec / 1000.0;
}

int main(void)
{
    signal(SIGINT, sig_handler);
    signal(SIGTERM, sig_handler);

    printf("=== HC-SR04 Ultrasonic Ranging ===\n");
    printf("Trig=GPIO%d  Echo=GPIO%d  Timeout=%dus\n", TRIG_PIN, ECHO_PIN, TIMEOUT_US);

    /* 初始化GPIO */
    if (gpio_export(TRIG_PIN, "out") != 0) { printf("FAIL: GPIO%d export\n", TRIG_PIN); return 1; }
    if (gpio_export(ECHO_PIN, "in") != 0)  { printf("FAIL: GPIO%d export\n", ECHO_PIN); return 1; }
    gpio_write(TRIG_PIN, 0);
    printf("[OK] GPIO init done\n");

    /* 设置Echo边沿检测 */
    gpio_set_edge(ECHO_PIN, "both");

    /* 打开Echo value用于poll监听 */
    int echo_fd = gpio_open_value(ECHO_PIN);
    if (echo_fd < 0) { perror("echo open"); return 1; }

    /* 先读一次清空状态 */
    char buf[4];
    read(echo_fd, buf, sizeof(buf));

    double sum_dist = 0;
    int valid_cnt = 0;
    int iter = 0;

    printf("\n  Distance    Smooth   Valid\n");
    printf("  --------    ------   -----\n");

    while (running) {
        iter++;

        /* 1. 发Trig脉冲(12us > 10us要求) */
        gpio_write(TRIG_PIN, 1);
        usleep(12);
        gpio_write(TRIG_PIN, 0);

        /* 2. 用poll等Echo上升沿 */
        struct pollfd pfd = { .fd = echo_fd, .events = POLLPRI | POLLERR };
        int ret = poll(&pfd, 1, TIMEOUT_US / 1000 + 1);
        if (ret <= 0) {
            printf("  [TIMEOUT] no echo response\n");
            goto next_cycle;
        }

        /* 读取并清除事件, 记录上升沿时间 */
        lseek(echo_fd, 0, SEEK_SET);
        read(echo_fd, buf, sizeof(buf));
        double t_rise = micros();

        /* 3. 等Echo下降沿 */
        ret = poll(&pfd, 1, TIMEOUT_US / 1000 + 1);
        if (ret <= 0) {
            printf("  [TIMEOUT] echo stuck high?\n");
            goto next_cycle;
        }

        lseek(echo_fd, 0, SEEK_SET);
        read(echo_fd, buf, sizeof(buf));
        double t_fall = micros();

        /* 4. 计算距离 */
        double pulse_us = t_fall - t_rise;
        if (pulse_us < 10 || pulse_us > TIMEOUT_US) {
            printf("  [INVALID] pulse=%.0fus (out of range)\n", pulse_us);
            goto next_cycle;
        }

        double distance = pulse_us / 58.0;  /* cm */
        valid_cnt++;
        sum_dist += distance;

        double smooth = sum_dist / valid_cnt;

        printf("  %7.1fcm  %7.1fcm  %5d\r",
               distance, smooth, valid_cnt);
        fflush(stdout);

        /* 每50次换行 */
        if (iter % 50 == 0) {
            printf("\n");
            sum_dist = 0;
            valid_cnt = 0;
        }

next_cycle:
        usleep(60000);  /* 60ms间隔 (HC-SR04建议>60ms) */
    }

    printf("\n\n[STOP] Cleanup...\n");
    close(echo_fd);
    gpio_write(TRIG_PIN, 0);

    /* 卸载GPIO */
    int fd = open("/sys/class/gpio/unexport", O_WRONLY);
    if (fd >= 0) {
        dprintf(fd, "%d", TRIG_PIN);
        dprintf(fd, "%d", ECHO_PIN);
        close(fd);
    }

    printf("Done.\n");
    return 0;
}
