#!/usr/bin/env python3
"""oc-routes MITM capture proxy. Stdlib only + `openssl` CLI for certs.

Intercepts HTTP and HTTPS (CONNECT + dynamic per-host certs) so driving the
real opencode CLI reveals the exact endpoints it touches, even ones static
analysis misses. Chains upstream to the sandbox egress proxy.

  python3 harness/mitm/mitm.py --port 18080 --log captures/mitm.jsonl

Then point clients at it:

  export HTTP_PROXY=http://127.0.0.1:18080 HTTPS_PROXY=http://127.0.0.1:18080
  export NODE_EXTRA_CA_CERTS=$PWD/harness/mitm/certs/ca.crt
  export SSL_CERT_FILE=$PWD/harness/mitm/certs/ca.crt
  export CURL_CA_BUNDLE=$PWD/harness/mitm/certs/ca.crt

Modes:
  --intercept (default): full HTTPS interception. CONNECT is answered, client
      TLS is terminated with a per-host cert signed by our CA, inner HTTP is
      logged (method + full URL + headers), then re-originated to the real
      server through the upstream proxy.
  --no-intercept: SNI-only. CONNECT hosts are logged, bytes blind-relayed.
      Use as a control to prove nothing bypasses the proxy.

Log: JSONL, one object per CONNECT and per inner HTTP request/response.
Upstream: --upstream http://HOST:PORT (default from HTTP_PROXY env, else the
sandbox 169.254.169.1:36341). Direct mode --no-upstream for open networks.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import select
import socket
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CERTS = os.path.join(HERE, "certs")
SANDBOX_UPSTREAM = "http://169.254.169.1:36341"

_log_lock = threading.Lock()
_log_fp = None


def log(obj: dict):
    obj.setdefault("ts", datetime.datetime.now(datetime.timezone.utc).isoformat())
    line = json.dumps(obj)
    with _log_lock:
        _log_fp.write(line + "\n")
        _log_fp.flush()


def read_head(conn: socket.socket, timeout: float = 20, limit: int = 1 << 20) -> bytes:
    conn.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            raise RuntimeError("request head too large")
    return buf


def parse_head(head: bytes):
    try:
        header_part, _, rest = head.partition(b"\r\n\r\n")
        lines = header_part.split(b"\r\n")
        request_line = lines[0].decode("latin1")
        method, target, version = request_line.split(" ", 2)
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.decode("latin1").strip().lower()] = v.decode("latin1").strip()
        return method, target, version, headers, rest
    except Exception as e:
        raise RuntimeError(f"bad request head: {e}")


def read_body(conn: socket.socket, headers: dict, prefix: bytes, timeout: float = 30) -> bytes:
    cl = headers.get("content-length")
    if cl is not None:
        try:
            n = int(cl)
        except ValueError:
            n = 0
        body = prefix
        while len(body) < n:
            conn.settimeout(timeout)
            chunk = conn.recv(min(65536, n - len(body)))
            if not chunk:
                break
            body += chunk
        return body[:n]
    if headers.get("transfer-encoding", "").lower() == "chunked":
        # de-chunk request bodies (rare for our CLI drive; keep simple)
        raw = prefix
        body = b""
        conn.settimeout(timeout)
        while True:
            while b"\r\n" not in raw:
                chunk = conn.recv(65536)
                if not chunk:
                    return body
                raw += chunk
            line, _, raw = raw.partition(b"\r\n")
            try:
                size = int(line.split(b";")[0].strip(), 16)
            except ValueError:
                return body
            if size == 0:
                return body
            while len(raw) < size + 2:
                chunk = conn.recv(65536)
                if not chunk:
                    return body
                raw += chunk
            body += raw[:size]
            raw = raw[size + 2:]
    return prefix


def relay(a: socket.socket, b: socket.socket, timeout: float = 60):
    a.setblocking(False)
    b.setblocking(False)
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([a, b], [], [], 5)
        if not r:
            continue
        for s in r:
            other = b if s is a else a
            try:
                data = s.recv(65536)
            except (BlockingIOError, ssl.SSLWantReadError):
                continue
            except Exception:
                return
            if not data:
                return
            try:
                other.sendall(data)
            except Exception:
                return
            end = time.time() + timeout


# ---------------- certs (openssl CLI) ----------------

def ensure_ca(certs_dir: str):
    key = os.path.join(certs_dir, "ca.key")
    crt = os.path.join(certs_dir, "ca.crt")
    if os.path.exists(key) and os.path.exists(crt):
        return key, crt
    os.makedirs(certs_dir, exist_ok=True)
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key,
         "-out", crt, "-days", "3", "-nodes", "-subj", "/CN=oc-routes-mitm"],
        check=True, capture_output=True)
    os.chmod(key, 0o600)
    return key, crt


def ensure_cert(certs_dir: str, ca_key: str, ca_crt: str, host: str) -> tuple[str, str]:
    safe = re.sub(r"[^a-z0-9.-]", "_", host.lower())[:100] or "host"
    crt = os.path.join(certs_dir, f"{safe}.crt")
    key = os.path.join(certs_dir, f"{safe}.key")
    if os.path.exists(crt) and os.path.exists(key):
        return crt, key
    csr = os.path.join(certs_dir, f"{safe}.csr")
    ext = os.path.join(certs_dir, f"{safe}.ext")
    with open(ext, "w") as f:
        f.write(f"subjectAltName=DNS:{host}\n")
    subprocess.run(
        ["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
         "-out", csr, "-subj", f"/CN={host}"],
        check=True, capture_output=True)
    subprocess.run(
        ["openssl", "x509", "-req", "-in", csr, "-CA", ca_crt, "-CAkey", ca_key,
         "-CAcreateserial", "-out", crt, "-days", "3", "-extfile", ext],
        check=True, capture_output=True)
    try:
        os.remove(csr)
        os.remove(ext)
    except OSError:
        pass
    return crt, key


# ---------------- upstream ----------------

class Upstream:
    def __init__(self, url: str | None):
        self.url = url
        if url:
            parts = urlsplit(url)
            self.host = parts.hostname
            self.port = parts.port or 80
        else:
            self.host = None

    def connect(self, host: str, port: int, timeout: float = 20) -> socket.socket:
        if not self.host:
            return socket.create_connection((host, port), timeout=timeout)
        s = socket.create_connection((self.host, self.port), timeout=timeout)
        s.settimeout(timeout)
        req = (f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               f"Proxy-Connection: Keep-Alive\r\n\r\n").encode()
        s.sendall(req)
        head = read_head(s, timeout=timeout)
        if b" 200" not in head.split(b"\r\n", 1)[0]:
            s.close()
            raise RuntimeError(f"upstream CONNECT rejected: {head[:120]!r}")
        # consume any buffered bytes beyond head (should be none for CONNECT)
        return s

    def forward_plain(self, raw_head: bytes, body: bytes, timeout: float = 30) -> tuple[bytes, bytes]:
        """Forward a plain-HTTP proxy request through upstream, return (resp_head, resp_rest_prefix)."""
        if not self.host:
            # direct: parse absolute URL to find origin
            raise RuntimeError("direct plain forward not implemented; set --upstream")
        s = socket.create_connection((self.host, self.port), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(raw_head + body)
        resp_head = read_head(s, timeout=timeout)
        return resp_head, s  # caller relays rest


def origin_tls_via_upstream(up: Upstream, host: str, port: int, timeout: float = 20):
    raw = up.connect(host, port, timeout=timeout)
    raw.settimeout(timeout)
    try:
        ctx = ssl.create_default_context()
        return ctx.wrap_socket(raw, server_hostname=host), True
    except Exception:
        try:
            raw.close()
        except Exception:
            pass
        raw2 = up.connect(host, port, timeout=timeout)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx.wrap_socket(raw2, server_hostname=host), False


# ---------------- handlers ----------------

def handle_plain(conn: socket.socket, raw_head: bytes, up: Upstream, client: str, tls: bool = False):
    method, target, version, headers, rest = parse_head(raw_head)
    body = read_body(conn, headers, rest)
    if tls:
        host_hdr = headers.get("host", "")
        url = f"https://{host_hdr}{target}" if target.startswith("/") else target
    else:
        url = target
        if not url.startswith("http"):
            host_hdr = headers.get("host", "")
            url = f"http://{host_hdr}{target}"
    parts = urlsplit(url)
    host = parts.hostname or ""
    entry = {"kind": "http", "dir": "out", "method": method, "url": url,
             "host": host, "path": parts.path or "/",
             "query": parts.query or "", "tls": tls,
             "req_headers": {k: v for k, v in headers.items() if k not in ("authorization", "proxy-authorization")},
             "authed": bool(headers.get("authorization") or headers.get("proxy-authorization")),
             "req_bytes": len(body), "client": client}
    # forward
    try:
        if up.host:
            s = socket.create_connection((up.host, up.port), timeout=20)
            s.settimeout(30)
            # rebuild head with absolute URL (proxy form) for plain http
            s.sendall(raw_head + body if not tls else raw_head + body)
            resp_head = read_head(s, timeout=30)
        else:
            s = socket.create_connection((host, parts.port or (443 if tls else 80)), timeout=20)
            s.settimeout(30)
            if tls:
                ctx = ssl.create_default_context()
                s = ctx.wrap_socket(s, server_hostname=host)
            s.sendall(raw_head + body)
            resp_head = read_head(s, timeout=30)
        try:
            _, _, _, resp_headers, resp_rest = parse_head(resp_head)
            entry["resp_status"] = None
            first = resp_head.split(b"\r\n", 1)[0].decode("latin1", "replace")
            m = re.search(r"\s(\d{3})\s", first)
            entry["resp_status"] = int(m.group(1)) if m else -1
        except Exception:
            resp_rest = b""
        conn.sendall(resp_head)
        # stream the rest both ways (response body client<-server; we already sent head)
        # pump server->client until EOF in a thread, then close
        def pump():
            try:
                s.settimeout(30)
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    conn.sendall(chunk)
                    entry["resp_bytes"] = entry.get("resp_bytes", 0) + len(chunk)
            except Exception as e:
                entry["relay_error"] = str(e)[:120]
            finally:
                try:
                    s.close()
                except Exception:
                    pass
                log(entry)
        threading.Thread(target=pump, daemon=True).start()
    except Exception as e:
        entry["error"] = str(e)[:200]
        log(entry)
        try:
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        except Exception:
            pass


def handle_client(conn: socket.socket, addr, up: Upstream, certs_dir: str, ca_key: str, ca_crt: str,
                  intercept: bool):
    client = f"{addr[0]}:{addr[1]}"
    try:
        raw_head = read_head(conn)
        if not raw_head:
            conn.close()
            return
        method, target, _version, headers, rest = parse_head(raw_head)
        if method.upper() != "CONNECT":
            handle_plain(conn, raw_head, up, client, tls=False)
            return
        # CONNECT
        authority = target
        if ":" in authority:
            host, _, port_s = authority.rpartition(":")
            try:
                port = int(port_s)
            except ValueError:
                port = 443
        else:
            host, port = authority, 443
        log({"kind": "connect", "host": host, "port": port, "client": client,
             "intercept": bool(intercept and port == 443)})
        try:
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: oc-routes-mitm\r\n\r\n")
        except Exception:
            conn.close()
            return
        if not intercept or port != 443:
            try:
                server = up.connect(host, port)
                relay(conn, server)
            except Exception as e:
                log({"kind": "connect_error", "host": host, "port": port,
                     "client": client, "error": str(e)[:200]})
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            return
        # TLS intercept
        try:
            crt, key = ensure_cert(certs_dir, ca_key, ca_crt, host)
        except Exception as e:
            log({"kind": "cert_error", "host": host, "client": client, "error": str(e)[:200]})
            conn.close()
            return
        sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sctx.load_cert_chain(crt, key)
        try:
            tls_conn = sctx.wrap_socket(conn, server_side=True)
        except Exception as e:
            log({"kind": "tls_handshake_error", "host": host, "client": client,
                 "error": str(e)[:200]})
            conn.close()
            return
        # inner request loop over the same TLS session
        try:
            while True:
                try:
                    inner_head = read_head(tls_conn, timeout=30)
                except (socket.timeout, TimeoutError):
                    break
                if not inner_head:
                    break
                imethod, itarget, _iver, iheaders, irest = parse_head(inner_head)
                ibody = read_body(tls_conn, iheaders, irest)
                path = itarget if itarget.startswith("/") else urlsplit(itarget).path or "/"
                q = urlsplit(itarget).query if not itarget.startswith("/") else urlsplit(itarget).query
                url = f"https://{host}{itarget}" if itarget.startswith("/") else itarget
                parts = urlsplit(url)
                entry = {"kind": "http", "dir": "out", "method": imethod, "url": url,
                         "host": host, "path": parts.path or "/", "query": parts.query or "",
                         "tls": True, "intercepted": True,
                         "req_headers": {k: v for k, v in iheaders.items()
                                         if k not in ("authorization", "proxy-authorization")},
                         "authed": bool(iheaders.get("authorization")),
                         "auth_scheme": (iheaders.get("authorization", "").split(" ")[0]
                                         if iheaders.get("authorization") else ""),
                         "req_bytes": len(ibody), "client": client}
                # forward to origin through upstream
                try:
                    server, verified = origin_tls_via_upstream(up, host, port)
                    entry["upstream_verified"] = verified
                    # origin-form request line
                    origin_path = itarget if itarget.startswith("/") else (parts.path or "/") + \
                        (("?" + parts.query) if parts.query else "")
                    lines = inner_head.split(b"\r\n")
                    lines[0] = f"{imethod} {origin_path} HTTP/1.1".encode()
                    out_head = b"\r\n".join(lines)
                    # strip proxy-only headers
                    server.sendall(out_head + ibody)
                    resp_head = read_head(server, timeout=30)
                    first = resp_head.split(b"\r\n", 1)[0].decode("latin1", "replace")
                    m = re.search(r"\s(\d{3})\s", first)
                    entry["resp_status"] = int(m.group(1)) if m else -1
                    tls_conn.sendall(resp_head)
                    # stream body
                    server.settimeout(30)
                    tls_conn.settimeout(30)
                    total = 0
                    while True:
                        try:
                            chunk = server.recv(65536)
                        except (socket.timeout, TimeoutError):
                            break
                        if not chunk:
                            break
                        total += len(chunk)
                        try:
                            tls_conn.sendall(chunk)
                        except Exception:
                            break
                    entry["resp_bytes"] = total
                    log(entry)
                    try:
                        server.close()
                    except Exception:
                        pass
                    # honor Connection: close from either side
                    conn_hdr = iheaders.get("connection", "").lower()
                    if conn_hdr == "close":
                        break
                except Exception as e:
                    entry["error"] = str(e)[:200]
                    log(entry)
                    try:
                        tls_conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    except Exception:
                        pass
                    break
        finally:
            try:
                tls_conn.close()
            except Exception:
                pass
    except Exception as e:
        try:
            log({"kind": "handler_error", "client": client, "error": str(e)[:200]})
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="oc-routes MITM capture proxy")
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--log", default="captures/mitm.jsonl")
    ap.add_argument("--certs-dir", default=DEFAULT_CERTS)
    ap.add_argument("--upstream", default=None,
                    help="upstream proxy URL (default: $HTTPS_PROXY or sandbox)")
    ap.add_argument("--no-upstream", action="store_true")
    ap.add_argument("--no-intercept", action="store_true",
                    help="SNI-only: log CONNECT, blind relay")
    ap.add_argument("--ready-file", default=None, help="touch this file once listening")
    args = ap.parse_args()

    global _log_fp
    upstream_url = None if args.no_upstream else (args.upstream or os.environ.get("HTTPS_PROXY")
                                                  or os.environ.get("HTTP_PROXY") or SANDBOX_UPSTREAM)
    up = Upstream(upstream_url)
    os.makedirs(os.path.dirname(os.path.abspath(args.log)) or ".", exist_ok=True)
    _log_fp = open(args.log, "a", buffering=1)
    ca_key, ca_crt = ensure_ca(args.certs_dir)
    print(f"mitm listening on {args.bind}:{args.port} upstream={upstream_url} "
          f"intercept={not args.no_intercept} ca={ca_crt}", flush=True)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(100)
    if args.ready_file:
        open(args.ready_file, "w").write("ready")
    try:
        while True:
            conn, addr = srv.accept()
            t = threading.Thread(target=handle_client,
                                 args=(conn, addr, up, args.certs_dir, ca_key, ca_crt,
                                       not args.no_intercept),
                                 daemon=True)
            t.start()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
