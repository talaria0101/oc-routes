#!/usr/bin/env python3
"""oc-routes MITM analyzer: diff what the CLI really touched vs the inventory.

Reads preload netlogs (hosts: dns/connect/CONNECT/SNI, spawns: exec) +
fetch tap logs (exact method+URL) and writes curated, human-readable
outputs alongside the JSON. Rendering is offline; rerun any time.

Outputs (atomic, guarded):
  captures/endpoints.json    every observed endpoint (hits, statuses, seen-in)
  captures/tap-hosts.json    per-host allowlist verdict (hits, evidence, runs)
  captures/REPORT.md         run table + endpoint table + spawns + tap verdict
  captures/REPORT.txt        plain-text twin
  captures/NEW_ENDPOINTS.md  runtime-observed endpoints missing from tables
                             + tap verdict + hook blind spots

Exit contract: 0 clean, 1 unknown hosts (needs a reason before joining the
allowlist), 2 validation refusal (bad/empty capture, prior outputs kept).

Usage:
  python3 harness/mitm/analyze.py --caps captures/ --routes spec/routes.json
                                  [--spec spec/opencode-models.json]
                                  [--force] [--min-events-per-run 1.0]
  python3 harness/mitm/analyze.py --report-only --caps captures/
      Re-render REPORT twins from committed curated JSONs only (no raw
      logs, no validation floors); for fresh clones and offline use.

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
from guards import atomic_write_json, atomic_write_text  # noqa: E402
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

# Transport-only evidence: the egress proxy address, never a destination.
TRANSPORT_IPS = {"169.254.169.1"}

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


def is_new_report_only(e):
    if e.get("in_static") is False:
        return True
    if e.get("in_spec") is False:
        return True
    return False


def report_only(args):
    """Re-render human reports from committed curated JSONs only."""
    cap = args.caps
    try:
        ep_doc = json.load(open(os.path.join(cap, "endpoints.json")))
        out_eps = ep_doc["endpoints"] if isinstance(ep_doc, dict) else ep_doc
    except FileNotFoundError:
        print(f"report-only needs curated JSONs: endpoints.json missing in {cap}")
        return 2
    except json.JSONDecodeError as e:
        print(f"endpoints.json corrupt: {e}; refusing")
        return 2
    try:
        tap_summary = json.load(open(os.path.join(cap, "tap-hosts.json")))
    except FileNotFoundError:
        tap_summary = []
    except json.JSONDecodeError as e:
        print(f"tap-hosts.json corrupt: {e}; refusing")
        return 2
    try:
        man = json.load(open(os.path.join(cap, "manifest.json")))
        run_list = man.get("runs", [])
        cli_version = man.get("cli", "opencode")
        window = man.get("window", "?")
    except (FileNotFoundError, json.JSONDecodeError):
        # Manifest is a drive-time convenience, not required for re-render.
        by_run: dict[str, int] = {}
        for fp in sorted(glob.glob(os.path.join(cap, "net-*.log"))):
            by_run[tag_of(fp)] = len(load_jsonl(fp))
        run_list = [{"name": k, "rc": "?", "seconds": "?", "events": v}
                    for k, v in sorted(by_run.items())]
        cli_version, window = "opencode", "?"
    unknown = [t["host"] for t in tap_summary if not t.get("known")]
    new = [e for e in out_eps if is_new_report_only(e)]
    total_events = sum(r.get("events", 0) for r in run_list
                       if isinstance(r.get("events"), int))
    rep = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "cli_version": cli_version,
        "runner": "LD_PRELOAD tap + fetch hook",
        "runs": len(run_list),
        "events": total_events,
        "run_list": run_list,
        "endpoints": [{"endpoint": e["endpoint"], "hits": e["hits"],
                       "statuses": e.get("statuses", []),
                       "seen_in": e.get("seen_in", [])} for e in out_eps],
        "spawns": [],
        "new_surface": new,
        "new_hosts": unknown,
        "tap_summary": tap_summary,
        "unknown": unknown,
        "window": window,
    }
    try:
        spawns_j = json.load(open(os.path.join(cap, "spawns.json")))
        rep["spawns"] = spawns_j if isinstance(spawns_j, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    atomic_write_text(os.path.join(cap, "REPORT.md"), render_caps_md(rep))
    atomic_write_text(os.path.join(cap, "REPORT.txt"), render_caps_txt(rep))
    print(f"report-only: {len(run_list)} runs, {len(out_eps)} endpoints "
          f"re-rendered -> {cap}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--caps", default="captures")
    ap.add_argument("--routes", default="spec/routes.json")
    ap.add_argument("--spec", default="spec/opencode-models.json")
    ap.add_argument("--cli-version", default=None)
    ap.add_argument("--force", action="store_true",
                    help="write outputs even when validation floors fail")
    ap.add_argument("--min-events-per-run", type=float, default=1.0,
                    help="refuse to overwrite unless avg events/run >= this")
    ap.add_argument("--report-only", action="store_true",
                    help="re-render REPORT.md/.txt from committed curated "
                    "JSONs only (no raw logs, no validation floors)")
    args = ap.parse_args()

    if args.report_only:
        return report_only(args)

    hosts: dict[str, dict] = {}  # host -> {runs:set, evs:set, hits}
    ep_hits: dict[str, dict] = {}  # endpoint -> {hits, statuses:set, seen_in:set}
    spawns: dict[str, dict] = {}   # cmd -> {hits, runs:set, call}
    total_events = 0
    tap_files = 0

    def note_host(h: str, run: str, ev: str):
        v = hosts.setdefault(h, {"runs": set(), "evs": set(), "hits": 0})
        v["hits"] += 1
        v["runs"].add(run)
        v["evs"].add(ev)

    def bump_endpoint(ep: str, run: str, status=None):
        e = ep_hits.setdefault(ep, {"hits": 0, "statuses": set(), "seen_in": set()})
        e["hits"] += 1
        e["seen_in"].add(run)
        if status is not None:
            e["statuses"].add(status)

    for fp in sorted(glob.glob(os.path.join(args.caps, "net-*.log"))):
        tap_files += 1
        t = tag_of(fp)
        for r in load_jsonl(fp):
            total_events += 1
            ev = r.get("ev")
            if ev == "dns" and r.get("node"):
                node = r["node"]
                if node in TRANSPORT_IPS:
                    continue
                note_host(node, t, ev)
            elif ev == "tls_sni" and r.get("sni"):
                note_host(r["sni"], t, ev)
                bump_endpoint(f"TLS {r['sni']}:443", t)
            elif ev == "proxy_connect" and r.get("target"):
                h = r["target"].split(" ")[0].rsplit(":", 1)[0]
                if h in TRANSPORT_IPS:
                    continue
                note_host(h, t, ev)
            elif ev == "connect" and r.get("ip"):
                if r["ip"] in TRANSPORT_IPS:
                    continue
                note_host(r["ip"], t, ev)
            elif ev == "exec" and r.get("path"):
                key = r["path"] + " " + " ".join(r.get("argv", [])[:4])
                s = spawns.setdefault(key[:160], {"hits": 0, "runs": set(),
                                                  "call": "execve"})
                s["hits"] += 1
                s["runs"].add(t)
    hook_hosts: set[str] = set()
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
            if parts.hostname:
                hook_hosts.add(parts.hostname)
                note_host(parts.hostname, t, "fetch")
            bump_endpoint(f"{r.get('method', 'GET')} {parts.hostname}{parts.path or '/'}",
                          t, r.get("resp_status"))

    # run list: manifest.json when drive.sh wrote it, else inferred
    run_list = []
    man_path = os.path.join(args.caps, "manifest.json")
    if os.path.exists(man_path):
        try:
            with open(man_path, encoding="utf-8") as f:
                man = json.load(f)
            run_list = man.get("runs", [])
        except (json.JSONDecodeError, OSError):
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
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        static_paths = set()

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

    tap_summary = [{"host": h, "hits": v["hits"],
                    "evidence": sorted(v["evs"]),
                    "runs": sorted(v["runs"])[:6],
                    "known": h in KNOWN_HOSTS,
                    "seen_by_hook": h in hook_hosts}
                   for h, v in sorted(hosts.items())]
    unknown = sorted(h for h in hosts if h not in KNOWN_HOSTS)
    blind = sorted(h for h in hosts
                   if h not in hook_hosts and h not in TRANSPORT_IPS
                   and h not in ("127.0.0.1", "localhost"))

    # ---- validation floors: never overwrite good data with bad/empty ----
    n_runs = len(run_list)
    avg = (total_events / n_runs) if n_runs else 0
    problems = []
    if n_runs == 0:
        problems.append("no runs in manifest/captures")
    if total_events == 0:
        problems.append("zero captured events (tap dead?)")
    if avg < args.min_events_per_run:
        problems.append(f"avg events/run {avg:.2f} < floor "
                        f"{args.min_events_per_run} (partial capture?)")
    if len(endpoints) == 0:
        problems.append("zero unique endpoints (network down?)")
    if tap_files == 0:
        problems.append("no netlog tap files (LD_PRELOAD ineffective? "
                        "cross-check missing)")
    if problems and not args.force:
        print("REFUSING to overwrite curated outputs:")
        for p in problems:
            print(f"  - {p}")
        print("kept previous outputs; re-run or pass --force")
        return 2
    for p in problems:
        print(f"WARNING (--force): {p}")

    atomic_write_json(os.path.join(args.caps, "endpoints.json"),
                      {"endpoints": endpoints, "hosts": sorted(hosts),
                       "new_hosts": unknown})
    atomic_write_json(os.path.join(args.caps, "tap-hosts.json"), tap_summary)

    lines = ["# Runtime-observed endpoints missing from static tables",
             "",
             f"Observed {len(endpoints)} unique endpoints ({total_events} events); "
             f"{len(new_surface)} not covered by static/spec tables.",
             ""]
    if not new_surface:
        lines += ["None: every runtime endpoint is already in the static tables.", ""]
    for n in new_surface:
        lines += [f"## `{n['endpoint']}`",
                  f"- hits: {n['hits']}, statuses: {n['statuses']}",
                  f"- runs: {', '.join(n['seen_in'])}", ""]
    lines += ["## Tap host allowlist verdict", ""]
    if tap_summary:
        for t in tap_summary:
            mark = "ok" if t["known"] else "UNKNOWN"
            why = KNOWN_HOSTS.get(t["host"], "NOT IN ALLOWLIST")
            lines.append(f"- [{mark}] `{t['host']}` hits={t['hits']} "
                         f"via {','.join(t['evidence'])} ({why})")
    else:
        lines.append("- no tap data (files missing or tap ineffective)")
    if blind:
        lines += ["", "Hook blind spots (tap saw host, hook saw no URL):"]
        for h in blind:
            lines.append(f"- `{h}`")
    lines.append("")
    atomic_write_text(os.path.join(args.caps, "NEW_ENDPOINTS.md"),
                      "\n".join(lines))

    rep = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "cli_version": args.cli_version or "opencode",
        "runner": "LD_PRELOAD tap + fetch hook",
        "runs": n_runs,
        "events": total_events,
        "run_list": run_list,
        "endpoints": endpoints,
        "spawns": [{"call": v["call"], "cmd": k, "hits": v["hits"],
                    "runs": sorted(v["runs"])} for k, v in sorted(spawns.items())],
        "new_surface": new_surface,
        "new_hosts": unknown,
        "tap_summary": tap_summary,
        "unknown": unknown,
        "window": "?",
    }
    stamps = []
    for fp in sorted(glob.glob(os.path.join(args.caps, "fetch-*.jsonl"))):
        for r in load_jsonl(fp):
            if isinstance(r.get("ts"), str):
                stamps.append(r["ts"])
    if stamps:
        rep["window"] = f"{min(stamps)} .. {max(stamps)}"

    atomic_write_text(os.path.join(args.caps, "REPORT.md"), render_caps_md(rep))
    atomic_write_text(os.path.join(args.caps, "REPORT.txt"), render_caps_txt(rep))

    print(f"hosts: {len(hosts)}  endpoints: {len(endpoints)}  events: {total_events}")
    for h in sorted(hosts):
        print(f"  host {h:30} known={bool(KNOWN_HOSTS.get(h))}")
    if unknown:
        print(f"RESULT: FAIL, unknown hosts: {', '.join(unknown)}")
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
