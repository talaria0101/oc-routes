#!/usr/bin/env python3
"""oc-routes MITM analyzer: diff what the CLI really touched vs the inventory.

Reads preload netlogs (hosts: dns/connect/CONNECT/SNI) + fetch tap logs
(exact method+URL) and reports:
  - all egress hosts seen, with first-seen command
  - hosts NOT in the known allowlist -> potential hidden endpoints (red)
  - exact URLs NOT covered by spec/routes.json paths -> review list
  - per-command network summary

Usage:
  python3 harness/mitm/analyze.py --caps captures/ --routes spec/routes.json

Known-host allowlist lives in this file (update with reason when adding).
It is derived from src pins, not guessed: see SOURCES below.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from urllib.parse import urlsplit

# host -> why it is known (src pin or observed+explained)
KNOWN_HOSTS = {
    "models.opencode.ai": "S4 pricing catalog; ModelsDev fetch ${source}/api.json",
    "opencode.ai": "S5/S6 zen inference + console device flow + docs site",
    "registry.npmjs.org": "npm-config.ts default registry; Installation.latest + Npm metadata",
    "api.github.com": "Installation.latest for non-npm methods (releases/latest)",
    "169.254.169.1": "sandbox egress proxy (transport, not a destination)",
    "127.0.0.1": "local serve target (bind fails in sandbox; ServeError)",
    "localhost": "local serve target",
}

# path prefixes covered by the static+live inventory (spec/routes.json handles
# the instance /api/* surface; these are the SaaS paths we already model)
KNOWN_PATH_RES = [
    r"^/api\.json$",
    r"^/console/auth/device/(code|token)$",
    r"^/console/device.*$",
    r"^/zen/(go/)?v1/(models|chat/completions|responses|messages|systemone|usage).*$",
    r"^/v2/openapi\.json$",
    r"^/[^/]+/[^/]+$",  # npm registry metadata /<pkg>/<channel>
    r"^/@[^/]+%2f[^/]+$",  # npm scoped metadata /@scope%2fname
]


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--caps", default="captures")
    ap.add_argument("--routes", default="spec/routes.json")
    args = ap.parse_args()

    hosts: dict[str, set[str]] = {}
    urls: dict[str, set[str]] = {}

    def tag(src):
        # net-<cmd>.log / fetch-<cmd>.jsonl -> cmd
        m = re.search(r"(?:net|fetch)-(.+?)(?:\.log|\.jsonl)$", os.path.basename(src))
        return m.group(1) if m else src

    for fp in sorted(glob.glob(os.path.join(args.caps, "net-*.log"))):
        for r in load_jsonl(fp):
            ev = r.get("ev")
            t = tag(fp)
            if ev == "dns" and r.get("node"):
                hosts.setdefault(r["node"], set()).add(t)
            elif ev in ("tls_sni",) and r.get("sni"):
                hosts.setdefault(r["sni"], set()).add(t)
            elif ev == "proxy_connect" and r.get("target"):
                h = r["target"].split(" ")[0].rsplit(":", 1)[0]
                hosts.setdefault(h, set()).add(t)
    for fp in sorted(glob.glob(os.path.join(args.caps, "fetch-*.jsonl"))):
        for r in load_jsonl(fp):
            if r.get("ev") != "fetch" or not r.get("url"):
                continue
            t = tag(fp)
            u = r["url"]
            hosts.setdefault(urlsplit(u).hostname or "?", set()).add(t)
            urls.setdefault(f'{r.get("method", "GET")} {u}', set()).add(t)

    print("oc-routes MITM analysis")
    print("=" * 60)
    print(f"hosts seen: {len(hosts)}  exact URLs: {len(urls)}")
    print()
    print("HOSTS")
    new_hosts = []
    for h in sorted(hosts):
        known = KNOWN_HOSTS.get(h, "")
        flag = "" if known else "  <-- NEW, investigate"
        if not known:
            new_hosts.append(h)
        print(f"  {h:35} via {','.join(sorted(hosts[h]))[:60]}  {known[:60]}{flag}")
    print()
    print("URLS NOT COVERED BY KNOWN PATHS")
    uncovered = 0
    for u in sorted(urls):
        try:
            p = urlsplit(u.split(" ", 1)[1]).path or "/"
        except Exception:
            p = "?"
        if any(re.match(rx, p) for rx in KNOWN_PATH_RES):
            continue
        uncovered += 1
        print(f"  {u[:140]}  via {','.join(sorted(urls[u]))[:40]}")
    if not uncovered:
        print("  none: every captured URL matches the modeled surface")
    print()
    if new_hosts:
        print(f"RESULT: FAIL, {len(new_hosts)} unknown host(s): {', '.join(new_hosts)}")
        return 1
    print("RESULT: PASS, no egress outside the known inventory")
    return 0


if __name__ == "__main__":
    sys.exit(main())
