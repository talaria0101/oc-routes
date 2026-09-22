#!/usr/bin/env python3
"""oc-routes report_lib: shared human-report renderers (markdown + plain text).

Every JSON artifact gets MD and TXT twins rendered straight from the JSON,
so humans can read the results without parsing anything. Rendering is
offline: `*_report_only` modes rebuild the twins from existing JSON with
zero network.

Stdlib only. Plain ASCII, fixed-width TXT tables.
"""
from __future__ import annotations


def md_table(headers, rows) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "/") for c in r) + " |")
    return "\n".join(out) + "\n"


def txt_table(headers, rows) -> str:
    widths = [len(h) for h in headers]
    strrows = [[str(c) for c in r] for r in rows]
    for r in strrows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], min(len(c), 60))
    def fmt(r):
        return "  ".join(c[:60].ljust(widths[i]) for i, c in enumerate(r)).rstrip()
    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in strrows]
    return "\n".join(lines) + "\n"


def money(v) -> str:
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == 0:
        return "Free"
    return f"${f:g}"


def render_spec_md(spec: dict) -> str:
    L = [f"# opencode routes spec ({spec.get('generated_at', '?')})", ""]
    stale = spec.get("stale")
    if stale:
        L += [f"> STALE: {stale}", ""]
    for w in spec.get("warnings", []) or []:
        L += [f"> WARN: {w}", ""]
    c = spec.get("counts", {})
    L += ["## Counts", "",
          f"- catalog providers: {c.get('catalog_providers', '?')}",
          f"- catalog opencode models: {c.get('catalog_opencode_models', '?')} "
          f"({c.get('catalog_opencode_free', '?')} free by price)",
          f"- live zen: {c.get('live_zen', '?')} "
          f"({c.get('live_zen_free_by_price', '?')} free, "
          f"{c.get('live_zen_unknown', '?')} unknown)",
          f"- live go: {c.get('live_go', '?')}", ""]
    drift = spec.get("drift", {})
    L += ["## Drift (catalog vs live)", "",
          f"- catalog-not-live ({len(drift.get('catalog_not_live', []))}): "
          + ", ".join(drift.get("catalog_not_live", [])[:25]),
          f"- live-not-catalog ({len(drift.get('live_not_catalog', []))}): "
          + ", ".join(drift.get("live_not_catalog", []) or ["none"]), ""]
    L += ["## Free models live",
          (", ".join(spec.get("free_models_live", [])) or "none"), ""]
    cheap = spec.get("cheapest_paid_top10", [])
    L += ["## Cheapest paid (per 1M tokens)", "",
          md_table(["model", "in", "out", "cache_read"],
                   [[m["id"], money(m["cost"].get("input")),
                     money(m["cost"].get("output")),
                     money(m["cost"].get("cache_read"))] for m in cheap])]
    L += ["## Auth probes", "",
          md_table(["probe", "HTTP", "model", "type", "message"],
                   [[k, v.get("status", "?"), v.get("model", ""),
                     v.get("error_type", ""),
                     (v.get("message", "") or "")[:90]]
                    for k, v in spec.get("auth_probes", {}).items()]), ""]
    eps = spec.get("model_endpoints", {})
    if eps:
        L += ["## Per-model serving endpoint (from /docs/zen table)", "",
              f"Models with a docs-table entry: {eps.get('coverage', '?')}. "
              "Anything not listed serves /chat/completions.", ""]
        nonchat = eps.get("non_chat", [])
        if nonchat:
            L += [md_table(["model", "endpoint", "sdk"],
                           [[m["id"], m["endpoint"], m.get("sdk", "")]
                            for m in nonchat]), ""]
    L += ["## Full tables", "",
          "- `opencode-models.json`: every model (prices, free, live flags, endpoint)",
          "- `routes.json`: merged endpoint inventory with drift both ways", ""]
    return "\n".join(L)


def render_spec_txt(spec: dict) -> str:
    L = [f"OPENCODE ROUTES SPEC ({spec.get('generated_at', '?')})", "=" * 60, ""]
    if spec.get("stale"):
        L += [f"STALE: {spec['stale']}", ""]
    for w in spec.get("warnings", []) or []:
        L += [f"WARN: {w}", ""]
    c = spec.get("counts", {})
    L += ["COUNTS",
          f"  catalog providers : {c.get('catalog_providers', '?')}",
          f"  catalog opencode  : {c.get('catalog_opencode_models', '?')} "
          f"({c.get('catalog_opencode_free', '?')} free)",
          f"  live zen          : {c.get('live_zen', '?')} "
          f"({c.get('live_zen_free_by_price', '?')} free)",
          f"  live go           : {c.get('live_go', '?')}", ""]
    free_live = ", ".join(spec.get("free_models_live", [])) or "none"
    L += ["FREE LIVE", f"  {free_live}", ""]
    cheap = spec.get("cheapest_paid_top10", [])
    L += ["CHEAPEST PAID (per 1M)",
          txt_table(["model", "in", "out", "cache_r"],
                    [[m["id"], money(m["cost"].get("input")),
                      money(m["cost"].get("output")),
                      money(m["cost"].get("cache_read"))] for m in cheap])]
    L += ["AUTH PROBES",
          txt_table(["probe", "HTTP", "model", "type", "message"],
                    [[k, v.get("status", "?"), (v.get("model", "") or "")[:28],
                      v.get("error_type", ""),
                      (v.get("message", "") or "")[:60]]
                     for k, v in spec.get("auth_probes", {}).items()]), ""]
    return "\n".join(L)


def render_caps_md(rep: dict) -> str:
    L = [f"# CLI drive report ({rep.get('cli_version', '?')}, {rep.get('runner', '?')} runner)", "",
         f"{rep.get('runs', '?')} runs, {rep.get('events', '?')} captured events, "
         f"{len(rep.get('endpoints', []))} unique endpoints, "
         f"{len(rep.get('new_surface', []))} new surface. "
         f"Window (UTC): {rep.get('window', '?')}.", ""]
    L += ["## Runs", "",
          md_table(["Run", "Exit", "Time", "Events"],
                   [[r["name"], r["rc"], f"{r['seconds']}s", r["events"]]
                    for r in rep.get("run_list", [])]), ""]
    L += ["## Endpoints observed", "",
          md_table(["Endpoint", "Hits", "Statuses", "Seen in"],
                   [[e["endpoint"], e["hits"], ",".join(map(str, e["statuses"])),
                     ", ".join(e["seen_in"][:4])] for e in rep.get("endpoints", [])]), ""]
    sp = rep.get("spawns", [])
    if sp:
        L += ["## Subprocesses spawned", "",
              md_table(["Call", "Command", "Hits", "Runs"],
                       [[s["call"], s["cmd"][:70], s["hits"], ", ".join(s["runs"][:3])]
                        for s in sp]), ""]
    L += ["## New surface (not in static tables)", ""]
    if rep.get("new_surface"):
        for n in rep["new_surface"]:
            L.append(f"- `{n['endpoint']}` (hits {n['hits']}, statuses {n['statuses']}, runs: {', '.join(n.get('seen_in', n.get('runs', [])))})")
    else:
        L.append("none: every captured endpoint matches the modeled surface")
    tap = rep.get("tap_summary", [])
    if tap:
        L += ["", "## Tap host allowlist verdict", ""]
        for t in tap:
            mark = "ok" if t.get("known") else "UNKNOWN"
            L.append(f"- [{mark}] `{t['host']}` hits={t['hits']} "
                     f"via {','.join(t.get('evidence', []))}")
        unknown = rep.get("unknown", rep.get("new_hosts", []))
        if unknown:
            L += ["", "Verdict: FAIL - unknown hosts need a reason "
                   "before they join the allowlist."]
        else:
            L += ["", "Verdict: PASS - no unknown hosts."]
    L += ["", "## Limits", "",
          "- No TCP bind in the sandbox: capture is in-process "
          "(LD_PRELOAD dns/connect/SNI tap + fetch hook), same host visibility as a TLS proxy.",
          "- Raw logs stay in this directory; curated tables above are what to cite.",
          "", "## Reproduce", "",
          "```sh", "./harness/mitm/drive.sh captures/  # live CLI battery (needs binary + network)",
          "python3 harness/mitm/analyze.py --caps captures  # offline re-curation",
          "```", ""]
    return "\n".join(L)


def render_caps_txt(rep: dict) -> str:
    L = [f"CLI DRIVE REPORT ({rep.get('cli_version', '?')})", "=" * 60, ""]
    L += ["RUNS",
          txt_table(["run", "exit", "time", "events"],
                    [[r["name"], r["rc"], f"{r['seconds']}s", r["events"]]
                     for r in rep.get("run_list", [])]), ""]
    L += ["ENDPOINTS",
          txt_table(["endpoint", "hits", "status", "seen_in"],
                    [[e["endpoint"], e["hits"], ",".join(map(str, e["statuses"])),
                      ",".join(e["seen_in"][:2])] for e in rep.get("endpoints", [])]), ""]
    if rep.get("new_surface"):
        L += ["NEW SURFACE"] + [f"  {n['endpoint']}" for n in rep["new_surface"]] + [""]
    else:
        L += ["NEW SURFACE: none", ""]
    tap = rep.get("tap_summary", [])
    if tap:
        L += ["TAP HOSTS"]
        for t in tap:
            mark = "ok" if t.get("known") else "UNKNOWN"
            L.append(f"  [{mark}] {t['host']:30} hits={t['hits']} "
                     f"{','.join(t.get('evidence', []))}")
        unknown = rep.get("unknown", rep.get("new_hosts", []))
        L.append("  verdict: " + ("FAIL - unknown hosts" if unknown else "PASS"))
        L += [""]
    return "\n".join(L)


def render_new_endpoints_md(new_surface: list, bundle: str, total: int, events: int) -> str:
    L = ["# Runtime-observed endpoints missing from static tables", "",
         f"Observed {total} unique endpoints ({events} events); "
         f"{len(new_surface)} not covered by static/spec tables.", ""]
    if not new_surface:
        L += ["None. Every captured endpoint matches the modeled surface.", ""]
        return "\n".join(L)
    for n in new_surface:
        L += [f"## `{n['endpoint']}`",
              f"- hits: {n['hits']}, statuses: {n['statuses']}",
              f"- in static bundle: {n.get('in_static', 'None')}, "
              f"in prior spec: {n.get('in_spec', 'None')}",
              f"- runs: {', '.join(n['runs'])}", ""]
    _ = bundle
    return "\n".join(L)
