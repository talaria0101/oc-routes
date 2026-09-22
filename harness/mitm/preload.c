/* oc-routes net tap: LD_PRELOAD logger for every egress the CLI attempts.
 *
 * Why this instead of a listen-socket MITM: the sandbox denies bind(2) on
 * INET sockets (EPERM) and ptrace, so no local proxy can listen and no
 * strace/tcpdump can attach. This shim needs neither: it lives inside the
 * target process and logs, for every connection attempt and every plaintext
 * proxy/TLS handshake it can see:
 *   - getaddrinfo() node/service           (DNS intent, real hostnames)
 *   - connect() family ip:port             (where it really connects;
 *                                           via egress proxy this is the proxy)
 *   - send()/sendto()/write() scans:       ("CONNECT host:port" proxy lines
 *                                           and TLS ClientHello SNI, which the
 *                                           egress proxy itself sees in clear)
 *
 * Together those three give the same host-level visibility a CONNECT proxy
 * log would, end-to-end encrypted traffic included, with zero sockets bound
 * and zero CA games. Full URL paths come from the fetch tap (tap_fetch.js)
 * when running the CLI from source; this shim covers any binary, including
 * the prebuilt opencode platform binary.
 *
 * Build: gcc -shared -fPIC -O2 -o netlog.so preload.c -ldl
 * Use:   OC_ROUTES_NETLOG=/tmp/net.log LD_PRELOAD=$PWD/netlog.so <cmd>
 * Log:   JSONL, O_APPEND writes. No malloc in hot path, no recursion
 *        (logging uses only write(2) + stack buffers + inet_ntop).
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <netdb.h>
#include <netinet/in.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>

static int log_fd = -1;
static char comm_name[64] = "?";

static ssize_t (*real_send)(int, const void *, size_t, int) = NULL;
static ssize_t (*real_sendto)(int, const void *, size_t, int,
                              const struct sockaddr *, socklen_t) = NULL;
static ssize_t (*real_write)(int, const void *, size_t) = NULL;
static int (*real_connect)(int, const struct sockaddr *, socklen_t) = NULL;
static int (*real_getaddrinfo)(const char *, const char *,
                               const struct addrinfo *,
                               struct addrinfo **) = NULL;
static int (*real_execve)(const char *, char *const[], char *const[]) = NULL;

static void emit(const char *msg) {
  if (log_fd < 0) return;
  char line[2048];
  struct timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  int n = snprintf(line, sizeof(line), "{\"ts\":%lld,\"pid\":%d,\"comm\":\"%s\",%s}\n",
                   (long long)ts.tv_sec, (int)getpid(), comm_name, msg);
  if (n > 0) {
    if ((size_t)n >= sizeof(line)) n = sizeof(line) - 1;
    /* O_APPEND single write: no interleaving for small lines */
    (void)write(log_fd, line, (size_t)n);
  }
}

static void json_escape(const char *src, char *dst, size_t cap, size_t maxin) {
  size_t o = 0;
  for (size_t i = 0; i < maxin && src[i] && o + 6 < cap; i++) {
    unsigned char c = (unsigned char)src[i];
    if (c == '"' || c == '\\') { dst[o++] = '\\'; dst[o++] = (char)c; }
    else if (c >= 0x20 && c < 0x7f) dst[o++] = (char)c;
    else if (c == '\n') { dst[o++] = '\\'; dst[o++] = 'n'; }
    else if (c == '\r') { dst[o++] = '\\'; dst[o++] = 'r'; }
    else if (c == '\t') { dst[o++] = '\\'; dst[o++] = 't'; }
    else { o += (size_t)snprintf(dst + o, cap - o, "\\u%04x", c); }
  }
  dst[o] = 0;
}

static void sockaddr_str(const struct sockaddr *sa, char *ip, size_t ipcap,
                         char *port, size_t portcap) {
  if (!sa) { snprintf(ip, ipcap, "?"); snprintf(port, portcap, "?"); return; }
  if (sa->sa_family == AF_INET) {
    const struct sockaddr_in *s = (const struct sockaddr_in *)sa;
    inet_ntop(AF_INET, &s->sin_addr, ip, (socklen_t)ipcap);
    snprintf(port, portcap, "%d", ntohs(s->sin_port));
  } else if (sa->sa_family == AF_INET6) {
    const struct sockaddr_in6 *s = (const struct sockaddr_in6 *)sa;
    inet_ntop(AF_INET6, &s->sin6_addr, ip, (socklen_t)ipcap);
    snprintf(port, portcap, "%d", ntohs(s->sin6_port));
  } else if (sa->sa_family == AF_UNIX) {
    snprintf(ip, ipcap, "unix");
    snprintf(port, portcap, "-");
  } else {
    snprintf(ip, ipcap, "fam%d", sa->sa_family);
    snprintf(port, portcap, "?");
  }
}

/* --- TLS ClientHello SNI parse. Returns 1 and fills sni on success. --- */
static int parse_sni(const unsigned char *b, size_t n, char *sni, size_t cap) {
  if (n < 6) return 0;
  if (b[0] != 0x16 || b[1] != 0x03) return 0;         /* TLS record, handshake */
  size_t reclen = ((size_t)b[3] << 8) | b[4];
  if (reclen + 5 > n) return 0;
  size_t p = 5;
  if (b[p] != 0x01) return 0;                          /* ClientHello */
  if (p + 4 > n) return 0;
  size_t hlen = ((size_t)b[p+1] << 16) | ((size_t)b[p+2] << 8) | b[p+3];
  p += 4;
  size_t hend = p + hlen;
  if (hend > n) return 0;
  p += 2 + 32;                                        /* version + random */
  if (p + 1 > hend) return 0;
  p += 1 + b[p];                                      /* session id */
  if (p + 2 > hend) return 0;
  size_t cslen = ((size_t)b[p] << 8) | b[p+1];
  p += 2 + cslen;
  if (p + 1 > hend) return 0;
  p += 1 + b[p];                                      /* compression */
  if (p + 2 > hend) return 0;
  size_t extlen = ((size_t)b[p] << 8) | b[p+1];
  p += 2;
  size_t eend = p + extlen;
  if (eend > hend) return 0;
  while (p + 4 <= eend) {
    unsigned etype = ((unsigned)b[p] << 8) | b[p+1];
    unsigned elen = ((unsigned)b[p+2] << 8) | b[p+3];
    p += 4;
    if (p + elen > eend) return 0;
    if (etype == 0 && elen >= 5) {
      size_t q = p + 2;                               /* skip list len */
      if (q + 3 > p + elen) return 0;
      if (b[q] != 0) return 0;                        /* host_name */
      unsigned nlen = ((unsigned)b[q+1] << 8) | b[q+2];
      q += 3;
      if (q + nlen > p + elen || nlen == 0 || nlen >= cap) return 0;
      memcpy(sni, b + q, nlen);
      sni[nlen] = 0;
      return 1;
    }
    p += elen;
  }
  return 0;
}

static void *memfind(const void *h, size_t hn, const void *nd, size_t ndn) {
  if (!ndn || ndn > hn) return NULL;
  const unsigned char *p = h;
  for (size_t i = 0; i + ndn <= hn; i++)
    if (memcmp(p + i, nd, ndn) == 0) return (void *)(p + i);
  return NULL;
}

static void sniff_bytes(const void *buf, size_t len, const char *fn) {
  if (!buf || len < 9) return;
  size_t scan = len > 4096 ? 4096 : len;
  const unsigned char *b = buf;
  /* CONNECT host:port (plaintext proxy request, also inside our own chain) */
  const unsigned char *c = memfind(b, scan, "CONNECT ", 8);
  if (c) {
    const unsigned char *e = memfind(c, scan - (size_t)(c - b), "\r\n", 2);
    size_t tlen = e ? (size_t)(e - (c + 8)) : 0;
    if (tlen > 0 && tlen < 300) {
      char target[320], esc[640];
      memcpy(target, c + 8, tlen);
      target[tlen] = 0;
      json_escape(target, esc, sizeof(esc), tlen);
      char msg[900];
      snprintf(msg, sizeof(msg), "\"ev\":\"proxy_connect\",\"via\":\"%s\",\"target\":\"%s\"",
               fn, esc);
      emit(msg);
    }
  }
  /* TLS ClientHello SNI */
  if (scan > 6 && b[0] == 0x16 && b[1] == 0x03) {
    char sni[256];
    if (parse_sni(b, scan, sni, sizeof(sni))) {
      char esc[512];
      json_escape(sni, esc, sizeof(esc), sizeof(sni));
      char msg[700];
      snprintf(msg, sizeof(msg), "\"ev\":\"tls_sni\",\"via\":\"%s\",\"sni\":\"%s\"", fn, esc);
      emit(msg);
    }
  }
}

__attribute__((constructor)) static void init(void) {
  real_send = dlsym(RTLD_NEXT, "send");
  real_sendto = dlsym(RTLD_NEXT, "sendto");
  real_write = dlsym(RTLD_NEXT, "write");
  real_connect = dlsym(RTLD_NEXT, "connect");
  real_getaddrinfo = dlsym(RTLD_NEXT, "getaddrinfo");
  real_execve = dlsym(RTLD_NEXT, "execve");
  int fd = open("/proc/self/comm", O_RDONLY);
  if (fd >= 0) {
    ssize_t n = read(fd, comm_name, sizeof(comm_name) - 1);
    if (n > 0) {
      comm_name[n] = 0;
      char *nl = strchr(comm_name, '\n');
      if (nl) *nl = 0;
    }
    close(fd);
  }
  const char *path = getenv("OC_ROUTES_NETLOG");
  if (!path || !*path) return;
  log_fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0644);
}

int connect(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
  if (real_connect) {
    char ip[128], port[16];
    sockaddr_str(addr, ip, sizeof(ip), port, sizeof(port));
    char msg[400];
    snprintf(msg, sizeof(msg), "\"ev\":\"connect\",\"ip\":\"%s\",\"port\":\"%s\",\"fam\":%d",
             ip, port, addr ? addr->sa_family : -1);
    emit(msg);
    return real_connect(sockfd, addr, addrlen);
  }
  /* extremely unlikely: no RTLD_NEXT */
  int (*libc)(int, const struct sockaddr *, socklen_t);
  libc = dlsym(RTLD_NEXT, "connect");
  return libc(sockfd, addr, addrlen);
}

int getaddrinfo(const char *node, const char *service,
                const struct addrinfo *hints, struct addrinfo **res) {
  if (real_getaddrinfo) {
    char nb[320] = "", sb[64] = "", ne[640] = "", se[128] = "";
    if (node) { strncpy(nb, node, sizeof(nb) - 1); json_escape(nb, ne, sizeof(ne), sizeof(nb)); }
    if (service) { strncpy(sb, service, sizeof(sb) - 1); json_escape(sb, se, sizeof(se), sizeof(sb)); }
    char msg[900];
    snprintf(msg, sizeof(msg), "\"ev\":\"dns\",\"node\":\"%s\",\"service\":\"%s\"",
             node ? ne : "", service ? se : "");
    emit(msg);
    return real_getaddrinfo(node, service, hints, res);
  }
  int (*libc)(const char *, const char *, const struct addrinfo *, struct addrinfo **);
  libc = dlsym(RTLD_NEXT, "getaddrinfo");
  return libc(node, service, hints, res);
}

int execve(const char *path, char *const argv[], char *const envp[]) {
  if (real_execve) {
    char args[768];
    size_t o = 0;
    args[0] = 0;
    if (argv) {
      for (int i = 0; i < 6 && argv[i] && o + 4 < sizeof(args); i++) {
        char esc[160];
        size_t len = 0;
        while (argv[i][len] && len < 100) len++;
        json_escape(argv[i], esc, sizeof(esc), len > 100 ? 100 : len);
        o += (size_t)snprintf(args + o, sizeof(args) - o, "%s\"%s\"",
                              i ? "," : "", esc);
      }
    }
    char pe[320];
    json_escape(path ? path : "?", pe, sizeof(pe), 200);
    char msg[1152];
    snprintf(msg, sizeof(msg), "\"ev\":\"exec\",\"path\":\"%s\",\"argv\":[%s]",
             pe, args);
    emit(msg);
    return real_execve(path, argv, envp);
  }
  int (*libc)(const char *, char *const[], char *const[]);
  libc = dlsym(RTLD_NEXT, "execve");
  return libc(path, argv, envp);
}

ssize_t send(int sockfd, const void *buf, size_t len, int flags) {
  if (real_send) {
    sniff_bytes(buf, len, "send");
    return real_send(sockfd, buf, len, flags);
  }
  ssize_t (*libc)(int, const void *, size_t, int);
  libc = dlsym(RTLD_NEXT, "send");
  return libc(sockfd, buf, len, flags);
}

ssize_t sendto(int sockfd, const void *buf, size_t len, int flags,
               const struct sockaddr *dest, socklen_t addrlen) {
  if (real_sendto) {
    sniff_bytes(buf, len, "sendto");
    return real_sendto(sockfd, buf, len, flags, dest, addrlen);
  }
  ssize_t (*libc)(int, const void *, size_t, int, const struct sockaddr *, socklen_t);
  libc = dlsym(RTLD_NEXT, "sendto");
  return libc(sockfd, buf, len, flags, dest, addrlen);
}

ssize_t write(int fd, const void *buf, size_t count) {
  static __thread int guard = 0;
  if (real_write) {
    if (!guard && fd != log_fd && count >= 9 && count <= 100000) {
      guard = 1;
      sniff_bytes(buf, count, "write");
      guard = 0;
    }
    return real_write(fd, buf, count);
  }
  ssize_t (*libc)(int, const void *, size_t);
  libc = dlsym(RTLD_NEXT, "write");
  return libc(fd, buf, count);
}
