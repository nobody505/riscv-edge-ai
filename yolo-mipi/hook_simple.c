#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <errno.h>
#include <grp.h>
#include "spm_cam_vi.h"
#include "cam_module_interface.h"

/* Two fixed-size SHM buffers: one per pipe, latest frame only */
#define SHM0 "/pipe0_frame"  // left: CSI3 OV5647
#define SHM1 "/pipe1_frame"  // right: CSI1 OV5647
typedef struct { volatile int ready; int size; int w, h; unsigned char data[640*480*3/2]; } frame_buf_t;

static frame_buf_t *g_fb[2] = {NULL, NULL};
static int g_fd[2] = {-1, -1};

typedef int32_t (*SCB_t)(uint32_t, void*);
typedef int32_t (*VCB_t)(uint32_t, VI_IMAGE_BUFFER_S*);
static SCB_t orig_SCB = NULL;
static VCB_t orig_cb[4] = {NULL};

static int secure_shm_fd(int fd) {
    struct group *group = getgrnam("elder-assistant");
    if (group == NULL) {
        fprintf(stderr, "[HOOK] group elder-assistant is unavailable\n");
        return -1;
    }
    if (fchown(fd, (uid_t)-1, group->gr_gid) < 0 || fchmod(fd, 0660) < 0) {
        perror("secure shared memory");
        return -1;
    }
    return 0;
}

static frame_buf_t* setup_shm(const char *name, int *out_fd) {
    int created = 0;
    int fd = shm_open(name, O_CREAT|O_EXCL|O_RDWR|O_CLOEXEC, 0660);
    if (fd >= 0) {
        created = 1;
    } else if (errno == EEXIST) {
        fd = shm_open(name, O_RDWR|O_CLOEXEC, 0);
    }
    if (fd < 0) { perror("shm_open"); return NULL; }
    if (secure_shm_fd(fd) < 0) {
        close(fd);
        if (created) shm_unlink(name);
        return NULL;
    }
    if (ftruncate(fd, sizeof(frame_buf_t)) < 0) {
        perror("resize shared memory");
        close(fd);
        if (created) shm_unlink(name);
        return NULL;
    }
    frame_buf_t *fb = mmap(NULL, sizeof(frame_buf_t), PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0);
    if (fb == MAP_FAILED) {
        close(fd);
        if (created) shm_unlink(name);
        return NULL;
    }
    memset(fb, 0, sizeof(*fb));
    *out_fd = fd;
    return fb;
}

static int32_t copy_frame(IMAGE_BUFFER_S *b, frame_buf_t *fb) {
    // NV12 multiplanar: plane[0]=Y, plane[1]=UV → interleave into single buffer
    if (b == NULL || fb == NULL || b->numPlanes < 1 ||
        b->planes[0].fd < 0 || b->size.width <= 0 || b->size.height <= 0 ||
        b->size.width > 640 || b->size.height > 480) return -1;
    size_t y_sz = (size_t)b->planes[0].length;
    size_t uv_sz = b->numPlanes >= 2 ? (size_t)b->planes[1].length : 0;
    size_t minimum_y = (size_t)b->size.width * (size_t)b->size.height;
    if (y_sz < minimum_y || y_sz > sizeof(fb->data) ||
        uv_sz > sizeof(fb->data) - y_sz) return -1;
    size_t total = y_sz + uv_sz;

    void *y_ptr = mmap(NULL, y_sz, PROT_READ, MAP_SHARED, b->planes[0].fd, 0);
    if (y_ptr == MAP_FAILED) return -1;
    void *uv_ptr = MAP_FAILED;
    if (uv_sz > 0) {
        if (b->planes[1].fd < 0) { munmap(y_ptr, y_sz); return -1; }
        uv_ptr = mmap(NULL, uv_sz, PROT_READ, MAP_SHARED, b->planes[1].fd, 0);
        if (uv_ptr == MAP_FAILED) { munmap(y_ptr, y_sz); return -1; }
    }

    /* Odd means writer active; even means the frame is stable. */
    __atomic_add_fetch(&fb->ready, 1, __ATOMIC_ACQ_REL);
    memcpy(fb->data, y_ptr, y_sz);
    if (uv_sz > 0) {
        memcpy(fb->data + y_sz, uv_ptr, uv_sz);
        munmap(uv_ptr, uv_sz);
    }
    munmap(y_ptr, y_sz);
    fb->size = (int)total; fb->w = b->size.width; fb->h = b->size.height;
    __atomic_add_fetch(&fb->ready, 1, __ATOMIC_RELEASE);
    return 0;
}

static int32_t wrap0(uint32_t ch, VI_IMAGE_BUFFER_S *vb) {
    if (g_fb[0] && vb && vb->buffer) copy_frame(vb->buffer, g_fb[0]);
    if (orig_cb[0]) return orig_cb[0](ch, vb);
    return 0;
}

static int32_t wrap1(uint32_t ch, VI_IMAGE_BUFFER_S *vb) {
    if (g_fb[1] && vb && vb->buffer) copy_frame(vb->buffer, g_fb[1]);
    if (orig_cb[1]) return orig_cb[1](ch, vb);
    return 0;
}

int32_t ASR_VI_SetCallback(uint32_t ch, int32_t (*cb)(uint32_t, VI_IMAGE_BUFFER_S*)) {
    if (!orig_SCB) orig_SCB = (SCB_t)dlsym(RTLD_NEXT, "ASR_VI_SetCallback");
    if (!orig_SCB) {
        fprintf(stderr, "[HOOK] cannot resolve ASR_VI_SetCallback\n");
        return -1;
    }
    fprintf(stderr, "[HOOK] SetCallback ch=%u cb=%p\n", ch, cb);
    if (ch >= sizeof(orig_cb) / sizeof(orig_cb[0])) return orig_SCB(ch, (void*)cb);
    orig_cb[ch] = cb;
    if (ch == 0) return orig_SCB(ch, (void*)wrap0);
    if (ch == 1) return orig_SCB(ch, (void*)wrap1);
    return orig_SCB(ch, (void*)cb);
}

__attribute__((constructor)) static void init(void) {
    g_fb[0] = setup_shm(SHM0, &g_fd[0]);
    g_fb[1] = setup_shm(SHM1, &g_fd[1]);
    fprintf(stderr, "[HOOK] FB0=%p FB1=%p\n", (void*)g_fb[0], (void*)g_fb[1]);
}
