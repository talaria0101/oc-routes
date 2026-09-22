#!/usr/bin/env python3
"""oc-routes guards: good data is never overwritten with bad/empty data.

Every fetch is validated (HTTP 200, minimum bytes, JSON shape) before it
replaces anything. Every JSON write is atomic (tmp + fsync + rename) and
gated on a validator; on failure the previous file is kept and the run is
flagged stale instead of publishing an empty spec.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error

UA = "oc-routes/1.0 (+https://github.com/talaria0101/oc-routes)"


class GuardError(Exception):
    pass


def fetch_bytes(url: str, min_bytes: int = 1, timeout: int = 30,
                headers: dict | None = None):
    """GET url. Returns (payload, status) or raises GuardError."""
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        raise GuardError(f"{url}: HTTP {e.code} ({len(body)} bytes)")
    except Exception as e:
        raise GuardError(f"{url}: {type(e).__name__}: {str(e)[:160]}")
    if status != 200:
        raise GuardError(f"{url}: HTTP {status}")
    if len(body) < min_bytes:
        raise GuardError(f"{url}: only {len(body)} bytes (< {min_bytes})")
    return body, status


def fetch_json(url: str, min_bytes: int = 100, timeout: int = 30,
               headers: dict | None = None):
    body, _ = fetch_bytes(url, min_bytes, timeout, headers)
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except Exception as e:
        raise GuardError(f"{url}: invalid JSON: {str(e)[:160]}")


def post_json(url: str, payload: dict, timeout: int = 25,
              headers: dict | None = None):
    """POST JSON. Returns (status, parsed-or-raw). Never raises."""
    h = {"User-Agent": UA, "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        code = e.code
    except Exception as e:
        return -1, {"_transport_error": f"{type(e).__name__}: {str(e)[:160]}"}
    try:
        return code, json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return code, {"_raw": body[:300].decode("utf-8", "replace")}


def validate(cond: bool, msg: str):
    if not cond:
        raise GuardError(msg)


def atomic_write_bytes(path: str, data: bytes):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def keep_prev(path: str):
    """Keep the current file as <path>.prev.json (local backup, gitignored).
    No-op when nothing exists yet. Never raises."""
    try:
        if not os.path.exists(path):
            return
        with open(path, "rb") as f:
            data = f.read()
        atomic_write_bytes(path + ".prev.json", data)
    except OSError:
        pass


def write_json_with_prev(path: str, obj):
    """Atomic JSON write that keeps the prior file as .prev.json first."""
    keep_prev(path)
    atomic_write_json(path, obj)


def atomic_write_json(path: str, obj):
    atomic_write_bytes(path, (json.dumps(obj, indent=2) + "\n").encode())


def atomic_write_text(path: str, text: str):
    atomic_write_bytes(path, text.encode())


def load_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
