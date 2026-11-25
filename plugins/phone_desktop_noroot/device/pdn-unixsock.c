/*
 * pdn-unixsock.so - Unix socket files -> abstract sockets, for Ubuntu under proot as
 * Android's shell user.
 *
 * Android's SELinux policy does not let the shell user create socket FILES anywhere it can
 * write (/data/local/tmp): bind() on a path fails with EACCES. Abstract sockets (no file) are
 * allowed. D-Bus, PulseAudio, RustDesk's IPC and Chromium's single-instance lock all bind
 * socket files, so this library (loaded into every program via /etc/ld.so.preload inside
 * Ubuntu) moves them into the abstract namespace:
 *
 *   bind("/run/user/1000/bus")    -> bind("\0pdnr:/run/user/1000/bus"), and an empty regular
 *                                    placeholder file at the path (programs stat/chmod/unlink it)
 *   connect("/run/user/1000/bus") -> connect("\0pdnr:/run/user/1000/bus"); if nothing listens
 *                                    there, the real path is tried as before
 *   getsockname/getpeername       -> report the original path again
 *
 * Only AF_UNIX path sockets are touched. Names too long for sun_path are hashed.
 * Access: abstract sockets are not files, so file permissions don't guard them. D-Bus and
 * PulseAudio still authenticate every client (cookie files that only Android's shell user
 * can read), and Android's SELinux policy keeps apps from connecting to shell sockets.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define PREFIX "pdnr:"

static int (*real_bind)(int, const struct sockaddr *, socklen_t);
static int (*real_connect)(int, const struct sockaddr *, socklen_t);
static int (*real_getsockname)(int, struct sockaddr *, socklen_t *);
static int (*real_getpeername)(int, struct sockaddr *, socklen_t *);

static void init(void) {
    if (!real_bind) {
        real_bind = dlsym(RTLD_NEXT, "bind");
        real_connect = dlsym(RTLD_NEXT, "connect");
        real_getsockname = dlsym(RTLD_NEXT, "getsockname");
        real_getpeername = dlsym(RTLD_NEXT, "getpeername");
    }
}

/* path socket address -> absolute path in out (0 = not a path socket) */
static int sock_path(const struct sockaddr *a, socklen_t len, char *out, size_t outsz) {
    if (!a || a->sa_family != AF_UNIX || len <= offsetof(struct sockaddr_un, sun_path))
        return 0;
    const struct sockaddr_un *u = (const struct sockaddr_un *)a;
    size_t n = len - offsetof(struct sockaddr_un, sun_path);
    if (n > sizeof u->sun_path) n = sizeof u->sun_path;
    if (n == 0 || u->sun_path[0] == '\0') return 0;            /* unnamed or already abstract */
    char p[sizeof u->sun_path + 1];
    memcpy(p, u->sun_path, n);
    p[n] = '\0';
    if (p[0] == '/') {
        snprintf(out, outsz, "%s", p);
    } else {
        char cwd[4096];
        if (!getcwd(cwd, sizeof cwd)) return 0;
        snprintf(out, outsz, "%s/%s", cwd, p);
    }
    return 1;
}

/* abstract address for a path */
static socklen_t abstract_addr(const char *path, struct sockaddr_un *u) {
    memset(u, 0, sizeof *u);
    u->sun_family = AF_UNIX;
    size_t room = sizeof u->sun_path - 1;
    char name[sizeof u->sun_path];
    if (strlen(PREFIX) + strlen(path) <= room) {
        snprintf(name, sizeof name, PREFIX "%s", path);
    } else {                                                   /* FNV-1a 64 */
        uint64_t h = 1469598103934665603ULL;
        for (const char *c = path; *c; c++) { h ^= (unsigned char)*c; h *= 1099511628211ULL; }
        snprintf(name, sizeof name, PREFIX "#%016llx", (unsigned long long)h);
    }
    size_t n = strlen(name);
    memcpy(u->sun_path + 1, name, n);
    return (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 1 + n);
}

/* getsockname/getpeername: fetch into our own full-size buffer, turn our abstract name back into
 * the original path, then copy into the caller's buffer only as much as it holds (the caller's
 * buffer may be smaller than a sockaddr_un; *len then reports the full size, as the kernel does). */
static int name_call(int (*fn)(int, struct sockaddr *, socklen_t *), int fd, struct sockaddr *addr, socklen_t *len) {
    if (!addr || !len) return fn(fd, addr, len);
    struct sockaddr_storage st;
    socklen_t sl = sizeof st;
    memset(&st, 0, sizeof st);
    int r = fn(fd, (struct sockaddr *)&st, &sl);
    if (r != 0) return r;
    struct sockaddr_un *u = (struct sockaddr_un *)&st;
    size_t pl = strlen(PREFIX);
    size_t n = sl > offsetof(struct sockaddr_un, sun_path) ? sl - offsetof(struct sockaddr_un, sun_path) : 0;
    if (u->sun_family == AF_UNIX && n >= 2 + pl && u->sun_path[0] == '\0' &&
        memcmp(u->sun_path + 1, PREFIX, pl) == 0 && u->sun_path[1 + pl] != '#') {
        char p[sizeof u->sun_path];
        size_t m = n - 1 - pl;
        if (m >= sizeof p) m = sizeof p - 1;
        memcpy(p, u->sun_path + 1 + pl, m);
        p[m] = '\0';
        memset(u->sun_path, 0, sizeof u->sun_path);
        memcpy(u->sun_path, p, m + 1);
        sl = (socklen_t)(offsetof(struct sockaddr_un, sun_path) + m + 1);
    }
    socklen_t room = *len;
    memcpy(addr, &st, room < sl ? room : sl);
    *len = sl;
    return 0;
}

int bind(int fd, const struct sockaddr *addr, socklen_t len) {
    init();
    char path[4300];
    if (!sock_path(addr, len, path, sizeof path)) return real_bind(fd, addr, len);
    struct sockaddr_un u;
    socklen_t ul = abstract_addr(path, &u);
    int r = real_bind(fd, (struct sockaddr *)&u, ul);
    if (r == 0) {                       /* placeholder, so the path "exists" like a socket file */
        int saved = errno;
        int f = open(path, O_WRONLY | O_CREAT | O_CLOEXEC, 0666);
        if (f >= 0) close(f);
        errno = saved;
    }
    return r;
}

int connect(int fd, const struct sockaddr *addr, socklen_t len) {
    init();
    char path[4300];
    if (!sock_path(addr, len, path, sizeof path)) return real_connect(fd, addr, len);
    struct sockaddr_un u;
    socklen_t ul = abstract_addr(path, &u);
    int r = real_connect(fd, (struct sockaddr *)&u, ul);
    if (r == 0 || (errno != ECONNREFUSED && errno != ENOENT)) return r;
    return real_connect(fd, addr, len);      /* nobody in the abstract namespace: a real socket file? */
}

int getsockname(int fd, struct sockaddr *addr, socklen_t *len) {
    init();
    return name_call(real_getsockname, fd, addr, len);
}

int getpeername(int fd, struct sockaddr *addr, socklen_t *len) {
    init();
    return name_call(real_getpeername, fd, addr, len);
}
