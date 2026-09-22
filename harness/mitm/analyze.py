#!/usr/bin/env python3
"""oc-routes MITM analyzer: diff what the CLI really touched vs the inventory.

Reads preload netlogs (hosts: dns/connect/CONNECT/SNI, spawns: exec) +
fetch tap logs (exact method+URL) and writes curated, human-readable
outputs alongside the JSON. Rendering is offline; rerun any time.

Outputs (atomic, guarded: zero total events -> refuse, keep prior):
  captures/endpoints.json    every observed endpoint (hits, statuses, seen-in)
  captures/REPORT.md         run table + endpoint table + spawns + new surface
  captures/REPORT.txt        plain-text twin
  captures/NEW_ENDPOINTS.md  runtime-observed endpoints missing from tables

Usage:
  python3 harness/mitm/analyze.py --caps captures/ --routes spec/routes.json
                                  [--spec spec/opencode-models.json]

Known-host allowlist lives in this file (update with reason when adding).
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import re
import sys
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # harness/: guards, report_lib
from guards import GuardError, atomic_write_json, atomic_write_text  # noqa: E402
from report_lib import (render_caps_md, render_caps_txt,  # noqa: E402
                        render_new_endpoints_md)

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

# SaaS paths already modeled (spec/routes.json covers the instance /api/*)
KNOWN_PATH_RES = [
    r"^/api\.json$",
    r"^/console/auth/device/(code|token)$",
    r"^/console/device.*$",
    r"^/zen/(go/)?v1/(models|chat/completions|responses|messages|systemone|usage).*$",
    r"^/v2/openapi\.json$",
    r"^/docs/zen$",
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


def tag_of(path):
    m = re.search(r"(?:net|fetch)-(.+?)(?:\.log|\.jsonl)$", os.path.basename(path))
    return m.group(1) if m else os.path.basename(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--caps", default="captures")
    ap.add_argument("--routes", default="spec/routes.json")
    ap.add_argument("--spec", default="spec/opencode-models.json")
    ap.add_argument("--cli-version", default=None)
    args = ap.parse_args()

    hosts: dict[str, set[str]] = {}
    ep_hits: dict[str, dict] = {}  # endpoint -> {hits, statuses:set, seen_in:set}
    spawns: dict[str, dict] = {}   # cmd -> {hits, runs:set, call}
    total_events = 0

    def bump_endpoint(ep: str, run: str, status=None):
        e = ep_hits.setdefault(ep, {"hits": 0, "statuses": set(), "seen_in": set()})
        e["hits"] += 1
        e["seen_in"].add(run)
        if status is not None:
            e["statuses"].add(status)

    for fp in sorted(glob.glob(os.path.join(args.caps, "net-*.log"))):
        t = tag_of(fp)
        for r in load_jsonl(fp):
            total_events += 1
            ev = r.get("ev")
            if ev == "dns" and r.get("node"):
                hosts.setdefault(r["node"], set()).add(t)
            elif ev == "tls_sni" and r.get("sni"):
                hosts.setdefault(r["sni"], set()).add(t)
                bump_endpoint(f"TLS {r['sni']}:443", t)
            elif ev == "proxy_connect" and r.get("target"):
                h = r["target"].split(" ")[0]
                hosts.setdefault(h.rsplit(":", 1)[0], set()).add(t)
            elif ev == "exec" and r.get("path"):
                key = r["path"] + " " + " ".join(r.get("argv", [])[:4])
                s = spawns.setdefault(key[:160], {"hits": 0, "runs": set(),
                                                  "call": "execve"})
                s["hits"] += 1
                s["runs"].add(t)
    for fp in sorted(glob.glob(os.path.join(args.caps, "fetch-*.jsonl"))):
        t = tag_of(fp)
        for r in load_jsonl(fp):
            if r.get("ev") == "tap_ready":
                total_events += 1
                continue
            if r.get("ev") != "fetch" or not r.get("url"):
                continue
            total_events += 1
            u = r["url"]
            parts = urlsplit(u)
            hosts.setdefault(parts.hostname or "?", set()).add(t)
            bump_endpoint(f"{r.get('method', 'GET')} {parts.hostname}{parts.path or '/'}",
                          t, r.get("resp_status"))

    if total_events == 0:
        print("GUARD FAIL: zero events across captures; keeping prior outputs")
        sys.exit(2)

    # run list: manifest.json when drive.sh wrote it, else inferred
    run_list = []
    man_path = os.path.join(args.caps, "manifest.json")
    if os.path.exists(man_path):
        try:
            with open(man_path, encoding="utf-8") as f:
                man = json.load(f)
            run_list = man.get("runs", [])
        except Exception:
            run_list = []
    if not run_list:
        by_run: dict[str, int] = {}
        for fp in sorted(glob.glob(os.path.join(args.caps, "net-*.log"))):
            by_run[tag_of(fp)] = len(load_jsonl(fp))
        run_list = [{"name": k, "rc": "?", "seconds": "?", "events": v}
                    for k, v in sorted(by_run.items())]

    endpoints = [{"endpoint": ep,
                  "hits": v["hits"],
                  "statuses": sorted(s for s in v["statuses"] if s is not None),
                  "seen_in": sorted(v["seen_in"])}
                 for ep, v in sorted(ep_hits.items())]

    # coverage: static routes.json paths + KNOWN_PATH_RES
    static_paths: set[str] = set()
    try:
        with open(args.routes, encoding="utf-8") as f:
            rj = json.load(f)
        for m in rj.get("merged", []):
            static_paths.add(re.sub(r":\w+", ":param", m.get("path", "")))
        for it in rj.get("interesting", []):
            static_paths.add(re.sub(r":\w+", ":param", it.get("path", "")))
    except Exception:
        static_paths = set()
    spec_ids: set[str] = set()
    try:
        with open(args.spec, encoding="utf-8") as f:
            sj = json.load(f)
        spec_ids = {m.get("id") for m in sj.get("models", [])}
    except Exception:
        spec_ids = set()

    new_surface = []
    for e in endpoints:
        ep = e["endpoint"]
        if ep.startswith("TLS "):
            continue  # host-level row; hosts table covers it
        try:
            p = urlsplit("x://" + ep.split(" ", 1)[1]).path or "/"
        except Exception:
            p = "?"
        if any(re.match(rx, p) for rx in KNOWN_PATH_RES):
            continue
        if p in static_paths:
            continue
        new_surface.append({**e, "in_static": None, "in_spec": None})

    new_hosts = sorted(h for h in hosts if h not in KNOWN_HOSTS)

    rep = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "cli_version": args.cli_version or "opencode",
        "runner": "LD_PRELOAD tap + fetch hook",
        "runs": len(run_list),
        "events": total_events,
        "run_list": run_list,
        "endpoints": endpoints,
        "spawns": [{"call": v["call"], "cmd": k, "hits": v["hits"],
                    "runs": sorted(v["runs"])} for k, v in sorted(spawns.items())],
        "new_surface": new_surface,
        "new_hosts": new_hosts,
        "window": "?",
    }
    # window from event timestamps when present
    stamps = []
    for fp in sorted(glob.glob(os.path.join(args.caps, "fetch-*.jsonl"))):
        for r in load_jsonl(fp):
            if isinstance(r.get("ts"), str):
                stamps.append(r["ts"])
    if stamps:
        rep["window"] = f"{min(stamps)} .. {max(stamps)}"

    atomic_write_json(os.path.join(args.caps, "endpoints.json"),
                      {"endpoints": endpoints, "hosts": sorted(hosts),
                       "new_hosts": new_hosts})
    atomic_write_text(os.path.join(args.caps, "REPORT.md"), render_caps_md(rep))
    atomic_write_text(os.path.join(args.caps, "REPORT.txt"), render_caps_txt(rep))
    atomic_write_text(os.path.join(args.caps, "NEW_ENDPOINTS.md"),
                      render_new_endpoints_md(new_surface, "", len(endpoints), total_events))

    print(f"hosts: {len(hosts)}  endpoints: {len(endpoints)}  events: {total_events}")
    for h in sorted(hosts):
        print(f"  host {h:30} known={bool(KNOWN_HOSTS.get(h))}")
    if new_hosts:
        print(f"RESULT: FAIL, unknown hosts: {', '.join(new_hosts)}")
        return 1
    if new_surface:
        print(f"RESULT: REVIEW, {len(new_surface)} endpoints outside tables (see NEW_ENDPOINTS.md)")
        for n in new_surface:
            print(f"  {n['endpoint']}")
        return 0
    print("RESULT: PASS, no egress outside the known inventory")
    return 0


if __name__ == "__main__":
    sys.exit(main())
