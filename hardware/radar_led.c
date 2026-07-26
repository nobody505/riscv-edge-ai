/*
 * HC-SR04 超声波雷达 + WS2812B 灯带联动
 * 距离逻辑（黄色聆听反馈结束后恢复原状态）:
 *   休眠态无效ASR请求      → 黄色双闪（最高灯带优先级，短暂覆盖后恢复）
 *   行路模式开启 + < 2米  → 蓝色快闪
 *   其他情况 + 绿色开启   → 绿色常亮
 *   其他情况 + 绿色关闭   → 灭灯
 *   摔倒警报活动          → 完全让出灯带，不写任何颜色或off
 *
 * 编译: gcc -O2 -o radar_led radar_led.c -lm
 * 运行: sudo ./radar_led [LED_COUNT]
 *
 * 接线:
 *   HC-SR04 Trig → GPIO72  (Pin13)
 *   HC-SR04 Echo → GPIO71  (Pin11)
 *   WS2812B DATA → GPIO37  (Pin40)
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <time.h>
#include <signal.h>
#include <poll.h>
#include <math.h>
#include <sys/mman.h>
#include <sched.h>
#include <stdint.h>

/* ====== 常量定义 ====== */
#define TRIG_PIN        72
#define ECHO_PIN        71
#define TIMEOUT_US      116000  /* Echo超时 ~20m (远超HC-SR04 4m上限) */

#define GPIO_BASE       0xd4019000
#define GPIO_SET        (0x1c / 4)
#define GPIO_CLEAR      (0x28 / 4)
#define BIT_MASK         (1u << 5)    /* GPIO37 = bit5 */

#define LED_COUNT        60
#define LED_BRIGHTNESS   64     /* 25% of full channel value (255) */

#define DIST_ALERT_CM    200.0
#define BLINK_INTERVAL_US 150000.0
#define GREEN_MODE_FILE  "/run/elder-assistant/radar_green_enabled"
#define TRAVEL_MODE_FILE "/run/elder-assistant/radar_travel_enabled"
#define FALL_ACTIVE_FILE "/run/elder-assistant/fall_alert_active"
#define YELLOW_REQUEST_FILE "/run/elder-assistant/radar_listening_yellow_request"
#define YELLOW_ACTIVE_FILE  "/run/elder-assistant/radar_listening_yellow_active"
#define YELLOW_SETTLE_US    220000
#define YELLOW_ON_US        200000
#define YELLOW_OFF_US       120000
#define BUILD_ID            "20260716-listening-yellow-r2"

enum led_mode {
    MODE_BLUE_BLINK = 0,
    MODE_GREEN_SOLID = 1,
    MODE_OFF = 2,
};

enum led_owner {
    OWNER_RADAR = 0,
    OWNER_FALL = 1,
    OWNER_LISTENING_YELLOW = 2,
};

static volatile int running = 1;
static volatile uint32_t *gpio;
static unsigned char leds[LED_COUNT * 3];
static int active_led_count = LED_COUNT;

/* ====== 时间工具 ====== */
static inline unsigned long rdtime(void) {
    unsigned long t;
    asm volatile("rdtime %0" : "=r"(t));
    return t;
}

static inline double micros(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000000.0 + ts.tv_nsec / 1000.0;
}

static void sig_handler(int sig) { (void)sig; running = 0; }

/* ====== GPIO sysfs 操作 ====== */
static int gpio_export(int pin, const char *dir) {
    char path[64];
    int fd;

    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/direction", pin);
    fd = open(path, O_WRONLY);
    if (fd < 0) {
        fd = open("/sys/class/gpio/export", O_WRONLY);
        if (fd < 0) { perror("gpio export"); return -1; }
        dprintf(fd, "%d", pin);
        close(fd);
        usleep(100000);
        fd = open(path, O_WRONLY);
        if (fd < 0) { perror("gpio direction"); return -1; }
    }
    dprintf(fd, "%s", dir);
    close(fd);
    return 0;
}

static int gpio_write(int pin, int val) {
    char path[64];
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/value", pin);
    int fd = open(path, O_WRONLY);
    if (fd < 0) return -1;
    dprintf(fd, "%d", val);
    close(fd);
    return 0;
}

static int gpio_open_value(int pin) {
    char path[64];
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/value", pin);
    return open(path, O_RDONLY);
}

static int gpio_set_edge(int pin, const char *edge) {
    char path[64];
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/edge", pin);
    int fd = open(path, O_WRONLY);
    if (fd < 0) return -1;
    dprintf(fd, "%s", edge);
    close(fd);
    return 0;
}

/* ====== WS2812B 灯带 ====== */
static unsigned char scale_brightness(int value) {
    if (value <= 0) return 0;
    if (value >= 255) value = 255;
    return (unsigned char)((value * LED_BRIGHTNESS + 127) / 255);
}

static void fill_all(int r, int g, int b) {
    memset(leds, 0, sizeof(leds));
    for (int i = 0; i < active_led_count; i++) {
        leds[i * 3 + 0] = scale_brightness(g);
        leds[i * 3 + 1] = scale_brightness(r);
        leds[i * 3 + 2] = scale_brightness(b);
    }
}

static void send_byte(unsigned char v) {
    for (int b = 7; b >= 0; b--) {
        if ((v >> b) & 1) {
            gpio[GPIO_SET] = BIT_MASK;
            unsigned long t = rdtime() + 20;
            while (rdtime() < t) {}
            gpio[GPIO_CLEAR] = BIT_MASK;
            t = rdtime() + 10;
            while (rdtime() < t) {}
        } else {
            gpio[GPIO_SET] = BIT_MASK;
            unsigned long t = rdtime() + 9;
            while (rdtime() < t) {}
            gpio[GPIO_CLEAR] = BIT_MASK;
            t = rdtime() + 21;
            while (rdtime() < t) {}
        }
    }
}

static void ws2812_flush(void) {
    gpio[GPIO_CLEAR] = BIT_MASK;
    usleep(300);
    for (int i = 0; i < LED_COUNT * 3; i++)
        send_byte(leds[i]);
    gpio[GPIO_CLEAR] = BIT_MASK;
    usleep(300);
}

static enum led_owner select_led_owner(int yellow_requested, int fall_active) {
    if (yellow_requested) return OWNER_LISTENING_YELLOW;
    if (fall_active) return OWNER_FALL;
    return OWNER_RADAR;
}

static int run_listening_yellow_flash(void) {
    if (access(YELLOW_REQUEST_FILE, F_OK) != 0) return 0;

    /* 原子接管请求。活动期间新到的request会保留到下一轮，不会被本轮误删。 */
    if (rename(YELLOW_REQUEST_FILE, YELLOW_ACTIVE_FILE) != 0) {
        if (errno != ENOENT) perror("yellow request claim");
        return 0;
    }

    printf("\n[RADAR] Listening feedback: YELLOW double-flash start\n");
    fflush(stdout);

    /* 给摔倒红闪线程足够时间观察活动标记并停止写灯带。 */
    usleep(YELLOW_SETTLE_US);
    for (int flash = 0; flash < 2 && running; ++flash) {
        fill_all(255, 255, 0);
        ws2812_flush();
        usleep(YELLOW_ON_US);
        fill_all(0, 0, 0);
        ws2812_flush();
        if (flash == 0) usleep(YELLOW_OFF_US);
    }
    fill_all(0, 0, 0);
    ws2812_flush();
    unlink(YELLOW_ACTIVE_FILE);
    printf("[RADAR] Listening feedback: YELLOW double-flash complete; restoring owner\n");
    fflush(stdout);
    return 1;
}

/* ====== 初始化 WS2812B GPIO MMIO ====== */
static int ws2812_init(void) {
    /* 引脚复用切 GPIO */
    int fd2 = open("/dev/mem", O_RDWR);
    if (fd2 < 0) { perror("/dev/mem pinctrl"); return -1; }
    volatile uint32_t *pinctrl = mmap(NULL, 0x1000, PROT_READ|PROT_WRITE, MAP_SHARED, fd2, 0xd401e000);
    close(fd2);
    if (pinctrl == MAP_FAILED) { perror("mmap pinctrl"); return -1; }
    pinctrl[0xd4 / 4] = 0xc440;
    munmap((void*)pinctrl, 0x1000);

    /* GPIO 控制寄存器 */
    int fd = open("/dev/mem", O_RDWR);
    if (fd < 0) { perror("/dev/mem gpio"); return -1; }
    gpio = mmap(NULL, 0x1000, PROT_READ|PROT_WRITE, MAP_SHARED, fd, GPIO_BASE);
    close(fd);
    if (gpio == MAP_FAILED) { perror("mmap gpio"); return -1; }

    /* sysfs 导出 GPIO37 */
    FILE *f = fopen("/sys/class/gpio/gpio37/direction", "w");
    if (!f) {
        f = fopen("/sys/class/gpio/export", "w");
        if (!f) { perror("export gpio37"); return -1; }
        fprintf(f, "37"); fclose(f);
        usleep(100000);
        f = fopen("/sys/class/gpio/gpio37/direction", "w");
        if (!f) { perror("gpio37 direction"); return -1; }
    }
    fprintf(f, "out"); fclose(f);

    /* 确保初始 LOW */
    gpio[GPIO_CLEAR] = BIT_MASK;
    return 0;
}

/* ====== HC-SR04 测距 ====== */
static double measure_distance(int echo_fd) {
    char buf[4];

    /* 发 Trig 脉冲 (12us) */
    gpio_write(TRIG_PIN, 1);
    usleep(12);
    gpio_write(TRIG_PIN, 0);

    /* 等 Echo 上升沿 */
    struct pollfd pfd = { .fd = echo_fd, .events = POLLPRI | POLLERR };
    int ret = poll(&pfd, 1, TIMEOUT_US / 1000 + 1);
    if (ret <= 0) return -1;

    lseek(echo_fd, 0, SEEK_SET);
    read(echo_fd, buf, sizeof(buf));
    double t_rise = micros();

    /* 等 Echo 下降沿 */
    ret = poll(&pfd, 1, TIMEOUT_US / 1000 + 1);
    if (ret <= 0) return -1;

    lseek(echo_fd, 0, SEEK_SET);
    read(echo_fd, buf, sizeof(buf));
    double t_fall = micros();

    double pulse_us = t_fall - t_rise;
    if (pulse_us < 10 || pulse_us > TIMEOUT_US) return -1;

    return pulse_us / 58.0;  /* cm */
}

static enum led_mode select_led_mode(double distance_cm, int green_enabled,
                                     int travel_enabled) {
    if (travel_enabled && distance_cm < DIST_ALERT_CM) return MODE_BLUE_BLINK;
    return green_enabled ? MODE_GREEN_SOLID : MODE_OFF;
}

static int run_logic_self_test(void) {
    if (scale_brightness(0) != 0 ||
            scale_brightness(128) != 32 ||
            scale_brightness(255) != LED_BRIGHTNESS) {
        fputs("BRIGHTNESS_SELF_TEST_FAILED\n", stderr);
        return 1;
    }
    struct logic_case {
        double distance_cm;
        int green_enabled;
        int travel_enabled;
        enum led_mode expected;
    } cases[] = {
        {150.0, 0, 0, MODE_OFF},
        {150.0, 1, 0, MODE_GREEN_SOLID},
        {150.0, 0, 1, MODE_BLUE_BLINK},
        {199.9, 1, 1, MODE_BLUE_BLINK},
        {200.0, 1, 1, MODE_GREEN_SOLID},
        {200.0, 0, 1, MODE_OFF},
    };
    size_t count = sizeof(cases) / sizeof(cases[0]);
    for (size_t i = 0; i < count; ++i) {
        enum led_mode actual = select_led_mode(
            cases[i].distance_cm, cases[i].green_enabled,
            cases[i].travel_enabled);
        if (actual != cases[i].expected) {
            fprintf(stderr, "LOGIC_SELF_TEST_FAILED case=%zu actual=%d expected=%d\n",
                    i, (int)actual, (int)cases[i].expected);
            return 1;
        }
    }
    struct owner_case {
        int yellow_requested;
        int fall_active;
        enum led_owner expected;
    } owner_cases[] = {
        {0, 0, OWNER_RADAR},
        {0, 1, OWNER_FALL},
        {1, 0, OWNER_LISTENING_YELLOW},
        {1, 1, OWNER_LISTENING_YELLOW},
    };
    size_t owner_count = sizeof(owner_cases) / sizeof(owner_cases[0]);
    for (size_t i = 0; i < owner_count; ++i) {
        enum led_owner actual = select_led_owner(
            owner_cases[i].yellow_requested, owner_cases[i].fall_active);
        if (actual != owner_cases[i].expected) {
            fprintf(stderr, "OWNER_SELF_TEST_FAILED case=%zu actual=%d expected=%d\n",
                    i, (int)actual, (int)owner_cases[i].expected);
            return 1;
        }
    }
    puts("RADAR_LOGIC_AND_BRIGHTNESS_SELF_TEST_OK");
    return 0;
}

/* ====== 主程序 ====== */
int main(int argc, char *argv[]) {
    if (argc == 2 && strcmp(argv[1], "--logic-self-test") == 0)
        return run_logic_self_test();

    int n_leds = LED_COUNT;
    if (argc > 1) n_leds = atoi(argv[1]);
    if (n_leds < 1) n_leds = 1;
    if (n_leds > 60) n_leds = 60;
    active_led_count = n_leds;
    printf("LED count: %d\n", n_leds);

    signal(SIGINT, sig_handler);
    signal(SIGTERM, sig_handler);

    /* 初始化雷达 GPIO */
    printf("[INIT] Radar GPIO...\n");
    if (gpio_export(TRIG_PIN, "out") != 0) { fprintf(stderr, "FAIL: Trig GPIO72 export\n"); return 1; }
    if (gpio_export(ECHO_PIN, "in") != 0)  { fprintf(stderr, "FAIL: Echo GPIO71 export\n"); return 1; }
    gpio_write(TRIG_PIN, 0);
    gpio_set_edge(ECHO_PIN, "both");
    int echo_fd = gpio_open_value(ECHO_PIN);
    if (echo_fd < 0) { perror("echo open"); return 1; }

    /* 清空初始状态 */
    { char buf[4]; read(echo_fd, buf, sizeof(buf)); }

    /* 初始化灯带 */
    printf("[INIT] LED strip...\n");
    if (ws2812_init() != 0) { fprintf(stderr, "FAIL: WS2812 init\n"); return 1; }
    unlink(YELLOW_ACTIVE_FILE);  /* 清理由异常退出留下的所有权标记。 */

    /* 不做彩虹/红灯自检。摔倒警报已活动时连off也不写，避免覆盖红色警报。 */
    if (access(FALL_ACTIVE_FILE, F_OK) != 0) {
        fill_all(0, 0, 0);
        ws2812_flush();
    }

    printf("\n=== Radar + LED Ready ===\n");
    printf("  build: %s\n", BUILD_ID);
    printf("  invalid ASR request     → YELLOW double-flash, then restore\n");
    printf("  travel + < 200cm        → BLUE blink\n");
    printf("  otherwise + green       → GREEN solid\n");
    printf("  otherwise               → OFF\n");
    printf("  fall alert active       → YIELD except short YELLOW feedback\n");
    printf("  Ctrl+C to quit\n\n");
    printf("%-10s %-10s %-18s %-8s %-8s %s\n",
           "Distance", "Smooth", "Mode", "Travel", "Green", "LED");

    /* 状态机 */
    double smooth = -1;
    double alpha = 0.3;
    int blink_on = 0;
    double last_blink = 0;
    int last_mode = -1;
    int fall_yielding = 0;

    while (running) {
        /* 黄色聆听反馈短暂高于蓝、绿和摔倒红闪；完成后重算原状态。 */
        if (run_listening_yellow_flash()) {
            last_mode = -1;
            last_blink = 0;
            blink_on = 0;
            continue;
        }

        /* 除短暂黄色聆听反馈外，摔倒警报优先；活动期间雷达不写灯带，包括off。 */
        if (access(FALL_ACTIVE_FILE, F_OK) == 0) {
            if (!fall_yielding) {
                printf("\n[RADAR] Fall alert active: yielding LED ownership\n");
                fflush(stdout);
            }
            fall_yielding = 1;
            last_mode = -1;
            usleep(30000);
            continue;
        }
        if (fall_yielding) {
            printf("[RADAR] Fall alert cleared: resuming radar LED logic\n");
            fflush(stdout);
            fall_yielding = 0;
            smooth = -1;
            last_mode = -1;
            last_blink = 0;
            blink_on = 0;
        }

        double dist = measure_distance(echo_fd);

        if (dist < 0) {
            /* 超时/无效 → 保持当前状态 */
            usleep(30000);
        } else {
            /* 指数平滑 */
            smooth = (smooth < 0) ? dist : smooth * (1 - alpha) + dist * alpha;

            int green_enabled = (access(GREEN_MODE_FILE, F_OK) == 0);
            int travel_enabled = (access(TRAVEL_MODE_FILE, F_OK) == 0);
            enum led_mode mode = select_led_mode(
                smooth, green_enabled, travel_enabled);
            const char *mode_str =
                mode == MODE_BLUE_BLINK ? "<2m TRAVEL BLUE" :
                mode == MODE_GREEN_SOLID ? "GREEN" : "OFF";

            /* 切换模式时立即刷灯带 */
            int mode_value = (int)mode;
            int mode_changed = (mode_value != last_mode);
            if (mode_changed) last_mode = mode_value;

            /* 只有行路模式开启且距离小于2米时蓝色闪烁。 */
            if (mode == MODE_BLUE_BLINK) {
                if (mode_changed) {
                    blink_on = 0;
                    last_blink = 0;
                }
                double now = micros();
                if (now - last_blink > BLINK_INTERVAL_US) {
                    last_blink = now;
                    blink_on = !blink_on;
                    if (blink_on) {
                        fill_all(0, 0, 255);
                    } else {
                        fill_all(0, 0, 0);
                    }
                    ws2812_flush();
                }
            } else if (mode_changed) {
                if (mode == MODE_GREEN_SOLID)
                    fill_all(0, 255, 0);
                else
                    fill_all(0, 0, 0);
                ws2812_flush();
            }

            printf("\r%-8.1fcm %-8.1fcm %-18s %-8s %-8s %s          ",
                   dist, smooth, mode_str,
                   travel_enabled ? "ENABLED" : "DISABLED",
                   green_enabled ? "ENABLED" : "DISABLED",
                   (mode == MODE_BLUE_BLINK ? (blink_on ? "BLUE-ON" : "BLUE-OFF") :
                    mode == MODE_GREEN_SOLID ? "GREEN" : "OFF"));
            fflush(stdout);

            usleep(30000);  /* 30ms 测距间隔 */
        }
    }

    /* 清理 */
    printf("\n\n[STOP] Cleanup...\n");
    unlink(YELLOW_ACTIVE_FILE);
    if (access(FALL_ACTIVE_FILE, F_OK) != 0) {
        fill_all(0, 0, 0);
        ws2812_flush();
    } else {
        printf("[STOP] Fall alert active: leaving LED ownership untouched\n");
    }
    close(echo_fd);
    gpio_write(TRIG_PIN, 0);

    int fd = open("/sys/class/gpio/unexport", O_WRONLY);
    if (fd >= 0) {
        dprintf(fd, "%d", TRIG_PIN);
        dprintf(fd, "%d", ECHO_PIN);
        close(fd);
    }
    printf("Done.\n");
    return 0;
}
