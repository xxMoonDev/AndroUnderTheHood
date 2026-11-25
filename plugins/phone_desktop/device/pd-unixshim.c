/*
 * pd-unixshim: Unix sockets for PhoneDesktop's no-root (proot) mode.
 *
 * Android's SELinux policy does not let the adb shell user create socket files anywhere it can
 * write (/data/local/tmp is shell_data_file: no sock_file create). D-Bus, PulseAudio, X11/ICE,
 * KDE and RustDesk all use socket files. Abstract-namespace sockets need no file and are allowed,
 * so this library (in /etc/ld.so.preload inside the proot Ubuntu only) turns
 *     bind/connect(AF_UNIX, "/some/path")  ->  bind/connect(AF_UNIX, "\0pdesk/some/path")
 * Both ends run under the same preload, so they meet. After a successful bind an empty regular
 * file is left at the path, so programs that check for the socket file still find something.
 * Abstract names (already starting with \0) and non-Unix sockets are not touched.
 *
 * Build (pd-chroot-setup does it):  gcc -O2 -shared -fPIC -o pd-unixshim.so pd-unixshim.c -ldl
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define PFX "pdesk"
#define PFXLEN (sizeof(PFX) - 1)

static int (*real_bind)(int, const struct sockaddr *, socklen_t);
static int (*real_connect)(int, const struct sockaddr *, socklen_t);

/* path from a filesystem AF_UNIX address, or 0 when it isn't one */
static size_t unix_path(const struct sockaddr *a, socklen_t len, const char **path) {
    const size_t off = offsetof(struct sockaddr_un, sun_path);
    if (!a || a->sa_family != AF_UNIX || len <= off)
        return 0;
    const struct sockaddr_un *u = (const struct sockaddr_un *)a;
    if (u->sun_path[0] == '\0')
        return 0;                                   /* abstract already */
    size_t n = strnlen(u->sun_path, len - off);
    if (n == 0 || n + 1 + PFXLEN > sizeof(u->sun_path))
        return 0;                                   /* too long to map: leave it */
    *path = u->sun_path;
    return n;
}

static socklen_t to_abstract(const char *path, size_t n, struct sockaddr_un *out) {
    memset(out, 0, sizeof(*out));
    out->sun_family = AF_UNIX;
    memcpy(out->sun_path + 1, PFX, PFXLEN);
    memcpy(out->sun_path + 1 + PFXLEN, path, n);
    return (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 1 + PFXLEN + n);
}

int bind(int fd, const struct sockaddr *a, socklen_t len) {
    if (!real_bind)
        real_bind = (int (*)(int, const struct sockaddr *, socklen_t))dlsym(RTLD_NEXT, "bind");
    const char *path;
    size_t n = unix_path(a, len, &path);
    if (!n)
        return real_bind(fd, a, len);
    struct sockaddr_un m;
    socklen_t ml = to_abstract(path, n, &m);
    int r = real_bind(fd, (const struct sockaddr *)&m, ml);
    if (r == 0) {
        char p[sizeof(m.sun_path) + 1];
        memcpy(p, path, n);
        p[n] = 0;
        int saved = errno;
        int f = open(p, O_CREAT | O_WRONLY | O_CLOEXEC, 0666);   /* placeholder for existence checks */
        if (f >= 0)
            close(f);
        errno = saved;
    }
    return r;
}

int connect(int fd, const struct sockaddr *a, socklen_t len) {
    if (!real_connect)
        real_connect = (int (*)(int, const struct sockaddr *, socklen_t))dlsym(RTLD_NEXT, "connect");
    const char *path;
    size_t n = unix_path(a, len, &path);
    if (!n)
        return real_connect(fd, a, len);
    struct sockaddr_un m;
    socklen_t ml = to_abstract(path, n, &m);
    int r = real_connect(fd, (const struct sockaddr *)&m, ml);
    if (r < 0 && errno == ECONNREFUSED) {
        /* nobody listens: with a real socket file that is ECONNREFUSED only when the file exists
         * (a stale socket), otherwise ENOENT. PulseAudio's stale-socket check relies on that. */
        char p[sizeof(m.sun_path) + 1];
        memcpy(p, path, n);
        p[n] = 0;
        if (access(p, F_OK) != 0)
            errno = ENOENT;
        else
            errno = ECONNREFUSED;
    }
    return r;
}
