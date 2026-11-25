/*
 * pd-shmshim: System V shared memory for X11 screen capture on Android kernels.
 *
 * Android kernels are built without SysV IPC, so shmget() fails with ENOSYS
 * and RustDesk's X11 capturer (XCB MIT-SHM) can't start. This LD_PRELOAD
 * library emulates the shm* calls with memfd and turns xcb_shm_attach()
 * (which passes a SysV id to the X server) into xcb_shm_attach_fd() (MIT-SHM
 * 1.2: passes the memfd over the X socket), which Xvfb supports.
 *
 * Only IPC_PRIVATE segments are supported (that is all xcb/Xlib clients use).
 * Build:  gcc -O2 -shared -fPIC -o pd-shmshim.so pd-shmshim.c -ldl
 * Use:    LD_PRELOAD=/usr/local/lib/pd-shmshim.so rustdesk --server
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <string.h>
#include <sys/ipc.h>
#include <sys/mman.h>
#include <sys/shm.h>
#include <unistd.h>
#include <xcb/shm.h>
#include <xcb/xcb.h>

#define MAXSEG 256
#define ID_BASE 0x5d000000

struct seg { int used, fd, removed, nattch; size_t size; void *addr[8]; };
static struct seg segs[MAXSEG];
static pthread_mutex_t lk = PTHREAD_MUTEX_INITIALIZER;

static struct seg *byid(int id) {
  int i = id - ID_BASE;
  if (i < 0 || i >= MAXSEG || !segs[i].used) return NULL;
  return &segs[i];
}

static void maybe_free(struct seg *s) {
  if (s->removed && s->nattch == 0) { close(s->fd); memset(s, 0, sizeof *s); }
}

int shmget(key_t key, size_t size, int flg) {
  (void)flg;
  if (key != IPC_PRIVATE) { errno = ENOSYS; return -1; }
  int fd = memfd_create("pd-shm", MFD_CLOEXEC);
  if (fd < 0) return -1;
  if (ftruncate(fd, (off_t)size) < 0) { int e = errno; close(fd); errno = e; return -1; }
  pthread_mutex_lock(&lk);
  for (int i = 0; i < MAXSEG; i++) if (!segs[i].used) {
    segs[i] = (struct seg){ .used = 1, .fd = fd, .size = size };
    pthread_mutex_unlock(&lk);
    return ID_BASE + i;
  }
  pthread_mutex_unlock(&lk);
  close(fd); errno = ENOSPC; return -1;
}

void *shmat(int id, const void *want, int flg) {
  pthread_mutex_lock(&lk);
  struct seg *s = byid(id);
  if (!s) { pthread_mutex_unlock(&lk); errno = EINVAL; return (void *)-1; }
  int prot = PROT_READ | ((flg & SHM_RDONLY) ? 0 : PROT_WRITE);
  void *p = mmap((void *)want, s->size, prot, MAP_SHARED, s->fd, 0);
  if (p == MAP_FAILED) { pthread_mutex_unlock(&lk); return (void *)-1; }
  for (int k = 0; k < 8; k++) if (!s->addr[k]) { s->addr[k] = p; break; }
  s->nattch++;
  pthread_mutex_unlock(&lk);
  return p;
}

int shmdt(const void *p) {
  pthread_mutex_lock(&lk);
  for (int i = 0; i < MAXSEG; i++) {
    struct seg *s = &segs[i];
    if (!s->used) continue;
    for (int k = 0; k < 8; k++) if (s->addr[k] == p) {
      munmap((void *)p, s->size);
      s->addr[k] = NULL; s->nattch--;
      maybe_free(s);
      pthread_mutex_unlock(&lk);
      return 0;
    }
  }
  pthread_mutex_unlock(&lk);
  errno = EINVAL; return -1;
}

int shmctl(int id, int cmd, struct shmid_ds *buf) {
  pthread_mutex_lock(&lk);
  struct seg *s = byid(id);
  if (!s) { pthread_mutex_unlock(&lk); errno = EINVAL; return -1; }
  int r = 0;
  if (cmd == IPC_RMID) { s->removed = 1; maybe_free(s); }
  else if (cmd == IPC_STAT && buf) {
    memset(buf, 0, sizeof *buf);
    buf->shm_segsz = s->size; buf->shm_nattch = s->nattch;
    buf->shm_perm.mode = 0600; buf->shm_perm.uid = getuid(); buf->shm_perm.cuid = getuid();
  } else if (cmd != IPC_SET) { errno = EINVAL; r = -1; }
  pthread_mutex_unlock(&lk);
  return r;
}

/* hand the memfd to the X server instead of a SysV id */
static int seg_fd(uint32_t shmid) {
  pthread_mutex_lock(&lk);
  struct seg *s = byid((int)shmid);
  int fd = s ? dup(s->fd) : -1;
  pthread_mutex_unlock(&lk);
  return fd;
}

xcb_void_cookie_t xcb_shm_attach(xcb_connection_t *c, xcb_shm_seg_t seg, uint32_t shmid, uint8_t ro) {
  int fd = seg_fd(shmid);
  if (fd >= 0) return xcb_shm_attach_fd(c, seg, fd, ro);
  static xcb_void_cookie_t (*real)(xcb_connection_t *, xcb_shm_seg_t, uint32_t, uint8_t);
  if (!real) real = dlsym(RTLD_NEXT, "xcb_shm_attach");
  return real(c, seg, shmid, ro);
}

xcb_void_cookie_t xcb_shm_attach_checked(xcb_connection_t *c, xcb_shm_seg_t seg, uint32_t shmid, uint8_t ro) {
  int fd = seg_fd(shmid);
  if (fd >= 0) return xcb_shm_attach_fd_checked(c, seg, fd, ro);
  static xcb_void_cookie_t (*real)(xcb_connection_t *, xcb_shm_seg_t, uint32_t, uint8_t);
  if (!real) real = dlsym(RTLD_NEXT, "xcb_shm_attach_checked");
  return real(c, seg, shmid, ro);
}
