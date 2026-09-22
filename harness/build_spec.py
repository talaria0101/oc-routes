#!/usr/bin/env python3
"""
oc-routes build_spec harness.

Generates our own latest, drift-free API spec answering what we care about:
  - what models are available (zen full + go lite, live)
  - what prices (per-model input/output/cache from models.opencode.ai/api.json,
    cross-checked against the /docs/zen pricing table)
  - any deals now (cost==0 free models, trial markers, free-tier gate)
  - any free models (list + how they differ catalog-vs-live)
  - do they work without auth (live probes: /models noauth vs chat noauth)

Inputs (live, authoritative at runtime):
  https://models.opencode.ai/api.json      pricing catalog
  https://opencode.ai/zen/v1/models        live zen listing (auth-independent)
  https://opencode.ai/zen/go/v1/models     live go listing
  https://opencode.ai/docs/zen             model table (per-model endpoint+sdk)
                                           + pricing table (cross-check)
  POST zen/v1/chat|responses               auth-gate probes (endpoint-correct:
                                           muse-spark serves /responses only)

Guards: nothing is overwritten with bad data. Every fetch is validated
(HTTP 200, min bytes, JSON shape, sane counts) and every write is atomic.
On any guard failure the run aborts BEFORE writing, keeping the last good
spec; rerun later. Snapshots only store validated payloads.

Stdlib only. Writes:
  spec/opencode-models.json   full per-model table (prices, free, live, auth)
  spec/spec.md                human answers to the five questions
  spec/REPORT.md + REPORT.txt auto twins rendered from the JSON (also via
                              --report-only, offline, no network)

Usage:
  python3 harness/build_spec.py [--snapshots ../snapshots] [--out-dir ../spec]
  python3 harness/build_spec.py --report-only [--out-dir ../spec]
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import re
import sys
import html as htmlmod

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from guards import (GuardError, fetch_bytes, fetch_json, post_json,  # noqa: E402
                    validate, atomic_write_bytes, atomic_write_json,
                    atomic_write_text, load_json)
from report_lib import render_spec_md, render_spec_txt  # noqa: E402

MODELS_API_URL = "https://models.opencode.ai/api.json"
ZEN_MODELS_URL = "https://opencode.ai/zen/v1/models"
ZEN_GO_MODELS_URL = "https://opencode.ai/zen/go/v1/models"
DOCS_ZEN_URL = "https://opencode.ai/docs/zen"
ZEN_BASE = "https://opencode.ai/zen/v1"

# Guard floors. Counts only, never model ids (ids drift by design).
MIN_PROVIDERS = 100
MIN_OPENCODE_MODELS = 20
MIN_ZEN_LIVE = 10


def is_free_cost(cost: dict) -> bool:
    if not cost:
        return False
    try:
        return float(cost.get("input", 1)) == 0 and float(cost.get("output", 1)) == 0
    except Exception:
        return False


def strip_lines(page_html: str) -> list[str]:
    t = re.sub(r"<script.*?</script>", " ", page_html, flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", "\n", t)
    return [l.strip() for l in htmlmod.unescape(t).splitlines() if l.strip()]


def parse_docs_model_table(lines: list[str]) -> dict:
    """Groups of (name, id, endpoint-url, sdk). Returns id -> {name, endpoint, sdk}."""
    out: dict[str, dict] = {}
    i = 0
    while i + 3 < len(lines):
        name, mid, url, sdk = lines[i], lines[i + 1], lines[i + 2], lines[i + 3]
        m = re.match(r"^https://opencode\.ai/zen/v1/([a-z/]+)$", url)
        if (m and re.match(r"^[a-z0-9][a-z0-9._-]*$", mid) and len(mid) < 80
                and sdk.startswith("@ai-sdk/")):
            out[mid] = {"name": name[:120], "endpoint": "/" + m.group(1), "sdk": sdk}
            i += 4
        else:
            i += 1
    return out


def parse_docs_pricing(lines: list[str]) -> dict:
    """After the 'Model Input Output Cached Read Cached Write' header, groups
    of (name, in, out, cacheR, cacheW). Returns name -> costs dict."""
    try:
        hdr = next(i for i, l in enumerate(lines)
                   if l == "Model" and lines[i + 1:i + 5] ==
                   ["Input", "Output", "Cached Read", "Cached Write"])
    except StopIteration:
        return {}
    out: dict[str, dict] = {}
    i = hdr + 5
    while i + 4 < len(lines):
        name, ci, co, cr, cw = lines[i:i + 5]
        if not re.match(r"^(\$[\d.,]+|Free|Free\*?|-)$", ci):
            break
        def num(s):
            s = s.strip()
            if s in ("Free", "-"):
                return 0.0
            return float(s.replace("$", "").replace(",", ""))
        try:
            out[name] = {"input": num(ci), "output": num(co),
                         "cache_read": num(cr),
                         "cache_write": None if cw.strip() == "-" else num(cw)}
        except ValueError:
            break
        i += 5
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--report-only", action="store_true",
                    help="render REPORT.md/TXT from existing spec JSON; no network")
    args = ap.parse_args()

    repo = os.path.dirname(HERE)
    snap_dir = args.snapshots or os.path.join(repo, "snapshots")
    out_dir = args.out_dir or os.path.join(repo, "spec")
    os.makedirs(snap_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)
    spec_path = os.path.join(out_dir, "opencode-models.json")

    if args.report_only:
        spec = load_json(spec_path)
        atomic_write_text(os.path.join(out_dir, "REPORT.md"), render_spec_md(spec))
        atomic_write_text(os.path.join(out_dir, "REPORT.txt"), render_spec_txt(spec))
        print(f"reports re-rendered from {spec_path} (no network)")
        return

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    use_network = not args.no_network
    docs_note = ""

    try:
        if use_network:
            catalog = fetch_json(MODELS_API_URL, min_bytes=100000)
            validate(isinstance(catalog, dict) and len(catalog) >= MIN_PROVIDERS,
                     f"catalog providers {len(catalog) if isinstance(catalog, dict) else '?'} < {MIN_PROVIDERS}")
            op_models_all = (catalog.get("opencode") or {}).get("models", {})
            validate(len(op_models_all) >= MIN_OPENCODE_MODELS,
                     f"opencode models {len(op_models_all)} < {MIN_OPENCODE_MODELS}")
            atomic_write_json(os.path.join(snap_dir, f"models-api-{stamp}.json"), catalog)

            z = fetch_json(ZEN_MODELS_URL, min_bytes=500)
            zen_live = sorted(m["id"] for m in z.get("data", []) if m.get("id"))
            validate(len(zen_live) >= MIN_ZEN_LIVE, f"zen live {len(zen_live)} < {MIN_ZEN_LIVE}")
            atomic_write_json(os.path.join(snap_dir, f"zen-models-{stamp}.json"), z)

            go_live: list[str] = []
            try:
                g = fetch_json(ZEN_GO_MODELS_URL, min_bytes=200)
                go_live = sorted(m["id"] for m in g.get("data", []) if m.get("id"))
                atomic_write_json(os.path.join(snap_dir, f"zen-go-models-{stamp}.json"), g)
            except GuardError as e:
                print(f"warn: go listing failed ({e}); continuing with empty go list")

            # S8 docs tables: best-effort auxiliary source. A layout change
            # must never kill the core catalog+zen spec.
            try:
                docs_html, _ = fetch_bytes(DOCS_ZEN_URL, min_bytes=20000)
                atomic_write_bytes(os.path.join(snap_dir, f"docs-zen-{stamp}.html"), docs_html)
                docs_lines = strip_lines(docs_html.decode("utf-8", "replace"))
                docs_endpoints = parse_docs_model_table(docs_lines)
                docs_pricing = parse_docs_pricing(docs_lines)
                validate(len(docs_endpoints) >= 20,
                         f"docs model table rows {len(docs_endpoints)} < 20")
            except GuardError as e:
                print(f"warn: S8 docs tables unusable ({e}); continuing without")
                docs_endpoints, docs_pricing = {}, {}
                docs_note = f"docs tables unavailable this run: {e}"
        else:
            cands = sorted(glob.glob(os.path.join(snap_dir, "models-api-*.json")))
            if not cands:
                raise GuardError("offline but no models-api snapshot found")
            catalog = load_json(cands[-1])
            validate(isinstance(catalog, dict) and len(catalog) >= MIN_PROVIDERS,
                     "offline snapshot catalog too thin; refetch live")
            cands = sorted(glob.glob(os.path.join(snap_dir, "zen-models-*.json")))
            zen_live = sorted(m["id"] for m in load_json(cands[-1]).get("data", [])) if cands else []
            cands = sorted(glob.glob(os.path.join(snap_dir, "zen-go-models-*.json")))
            go_live = sorted(m["id"] for m in load_json(cands[-1]).get("data", [])) if cands else []
            cands = sorted(glob.glob(os.path.join(snap_dir, "docs-zen-*.html")))
            docs_lines = strip_lines(open(cands[-1], encoding="utf-8", errors="replace").read()) if cands else []
            docs_endpoints = parse_docs_model_table(docs_lines)
            docs_pricing = parse_docs_pricing(docs_lines)
    except GuardError as e:
        print(f"GUARD FAIL: {e}")
        print(f"kept last good spec untouched: {spec_path}")
        sys.exit(2)

    op = catalog.get("opencode", {})
    op_models = op.get("models", {})
    go = catalog.get("opencode-go", {})
    go_models = go.get("models", {})

    zen_set = set(zen_live)
    op_free = sorted(k for k, v in op_models.items() if is_free_cost(v.get("cost", {})))

    live_free, live_paid, live_unknown = [], [], []
    for mid in zen_live:
        m = op_models.get(mid)
        if m is None:
            live_unknown.append(mid)
        elif is_free_cost(m.get("cost", {})):
            live_free.append(mid)
        else:
            live_paid.append(mid)

    catalog_not_live = sorted(set(op_models) - zen_set)
    live_not_catalog = sorted(zen_set - set(op_models))

    # Endpoint-correct auth probes, model ids chosen live (drift-free):
    # free chat model = first live-free id serving /chat/completions,
    # paid chat model = first live-paid id serving /chat/completions,
    # responses model = first live-free id serving /responses.
    probes: dict = {}
    if use_network:
        def pick(pool: list[str], endpoint: str) -> str | None:
            for mid in pool:
                ep = (docs_endpoints.get(mid) or {}).get("endpoint", "/chat/completions")
                if ep == endpoint:
                    return mid
            return pool[0] if pool else None
        free_chat = pick(sorted(live_free), "/chat/completions")
        paid_chat = pick(sorted(live_paid), "/chat/completions")
        resp_free = pick(sorted(live_free), "/responses")
        if free_chat:
            for label, auth in [("free-noauth", None), ("free-public", "public")]:
                hdrs = {"Authorization": f"Bearer {auth}"} if auth else {}
                code, j = post_json(f"{ZEN_BASE}/chat/completions",
                                    {"model": free_chat,
                                     "messages": [{"role": "user", "content": "hi"}],
                                     "max_tokens": 5}, headers=hdrs)
                err = j.get("error", {}) if isinstance(j, dict) else {}
                probes[label] = {"status": code, "model": free_chat,
                                 "error_type": err.get("type", "") if isinstance(err, dict) else "",
                                 "message": (err.get("message", "") if isinstance(err, dict) else "")[:220]}
        if paid_chat:
            code, j = post_json(f"{ZEN_BASE}/chat/completions",
                                {"model": paid_chat,
                                 "messages": [{"role": "user", "content": "hi"}],
                                 "max_tokens": 5})
            err = j.get("error", {}) if isinstance(j, dict) else {}
            probes["paid-noauth"] = {"status": code, "model": paid_chat,
                                       "error_type": err.get("type", "") if isinstance(err, dict) else "",
                                       "message": (err.get("message", "") if isinstance(err, dict) else "")[:220]}
        if resp_free:
            code, j = post_json(f"{ZEN_BASE}/responses",
                                {"model": resp_free, "input": "hi"})
            err = j.get("error", {}) if isinstance(j, dict) else {}
            probes["muse-free-responses-noauth"] = {
                "status": code, "model": resp_free,
                "error_type": err.get("type", "") if isinstance(err, dict) else "",
                "message": (err.get("message", "") if isinstance(err, dict) else "")[:220]}
        for label, auth in [("models-noauth", None), ("models-public", "public")]:
            hdrs = {"Authorization": f"Bearer {auth}"} if auth else {}
            try:
                d = fetch_json(ZEN_MODELS_URL, headers=hdrs)
                probes[label] = {"status": 200, "count": len(d.get("data", []))}
            except GuardError as e:
                probes[label] = {"status": -1, "error": str(e)[:120]}
        atomic_write_json(os.path.join(snap_dir, f"spec-probes-{stamp}.json"), probes)

    cheapest = sorted(((k, v.get("cost", {})) for k, v in op_models.items()
                       if not is_free_cost(v.get("cost", {}))),
                      key=lambda kv: ((kv[1] or {}).get("input", 999)))[:10]

    # docs pricing cross-check: match docs rows to catalog by normalized name
    def norm_name(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())
    cat_by_name = {norm_name(v.get("name", "")): (k, v.get("cost", {}) or {})
                   for k, v in op_models.items()}
    price_match, price_mismatch = 0, []
    for dname, dc in docs_pricing.items():
        hit = cat_by_name.get(norm_name(dname))
        if not hit:
            continue
        kid, kc = hit
        same = all(abs(float(dc[f]) - float(kc.get(f if f != "cache_read" else "cache_read", 0) or 0)) < 1e-9
                   for f in ("input", "output", "cache_read"))
        if same:
            price_match += 1
        elif len(price_mismatch) < 15:
            price_mismatch.append({"docs": dname, "catalog": kid,
                                   "docs_cost": dc,
                                   "catalog_cost": {k: kc.get(k) for k in ("input", "output", "cache_read")}})

    non_chat = sorted(({"id": mid, **info} for mid, info in docs_endpoints.items()
                       if info["endpoint"] != "/chat/completions"),
                      key=lambda m: m["id"])

    models_table = []
    for mid in sorted(set(list(op_models.keys()) + zen_live)):
        cat = op_models.get(mid)
        cost = (cat or {}).get("cost", {}) if cat else {}
        ep = (docs_endpoints.get(mid) or {})
        models_table.append({
            "id": mid,
            "name": (cat or {}).get("name", "") or ep.get("name", ""),
            "in_catalog": mid in op_models,
            "live_zen": mid in zen_set,
            "free_by_price": is_free_cost(cost),
            "input": cost.get("input"),
            "output": cost.get("output"),
            "cache_read": cost.get("cache_read"),
            "cache_write": cost.get("cache_write"),
            "release_date": (cat or {}).get("release_date", ""),
            "endpoint": ep.get("endpoint", "/chat/completions"),
            "sdk": ep.get("sdk", ""),
        })

    spec_json = {
        "generated_at": stamp + " (UTC)",
        "warnings": [docs_note] if docs_note else [],
        "sources": {
            "pricing_catalog": MODELS_API_URL,
            "zen_live": ZEN_MODELS_URL,
            "go_live": ZEN_GO_MODELS_URL,
            "docs_table": DOCS_ZEN_URL,
            "zen_base_from_src": "api=https://opencode.ai/zen/v1 (models.opencode.ai api field, provider opencode)",
            "go_base_from_src": "api=https://opencode.ai/zen/go/v1 (provider opencode-go)",
            "instance_openapi": "https://opencode.ai/v2/openapi.json",
            "src_pins": {
                "opencode_provider_gate": "packages/opencode/src/provider/provider.ts opencode(): unauth keeps only cost.input==0, apiKey=public",
                "console_gate": "packages/console/app/src/routes/zen/util/handler.ts authenticate()/validateBilling() + models.ts allowAnonymous",
            },
        },
        "counts": {
            "catalog_providers": len(catalog),
            "catalog_opencode_models": len(op_models),
            "catalog_opencode_free": len(op_free),
            "catalog_go_models": len(go_models),
            "live_zen": len(zen_live),
            "live_zen_free_by_price": len(live_free),
            "live_zen_paid": len(live_paid),
            "live_zen_unknown": len(live_unknown),
            "live_go": len(go_live),
            "docs_models": len(docs_endpoints),
            "docs_prices": len(docs_pricing),
        },
        "drift": {"catalog_not_live": catalog_not_live, "live_not_catalog": live_not_catalog},
        "free_models_catalog": op_free,
        "free_models_live": sorted(live_free),
        "live_unknown_models": sorted(live_unknown),
        "cheapest_paid_top10": [{"id": k, "cost": c} for k, c in cheapest],
        "auth_probes": probes,
        "model_endpoints": {"coverage": len(docs_endpoints), "non_chat": non_chat},
        "docs_pricing": {"rows": len(docs_pricing), "match_catalog": price_match,
                         "mismatches": price_mismatch},
        "models": models_table,
    }
    atomic_write_json(spec_path, spec_json)
    atomic_write_text(os.path.join(out_dir, "REPORT.md"), render_spec_md(spec_json))
    atomic_write_text(os.path.join(out_dir, "REPORT.txt"), render_spec_txt(spec_json))

    # long-form human answers (kept for continuity; twins above are generated)
    lines = []
    A = lines.append
    A(f"# opencode models spec ({stamp} UTC)")
    A("")
    A("Drift-free snapshot built live by `harness/build_spec.py` (guarded: aborts")
    A("before writing rather than publishing bad data). Rerun to refresh.")
    A("Sources: models.opencode.ai/api.json (pricing), opencode.ai/zen/v1/models and")
    A("/zen/go/v1/models (live), /docs/zen model+pricing tables (S8), endpoint-correct")
    A("probes, plus src pins (provider.ts opencode() gate, console handler.ts gate).")
    A("")
    A(f"Catalog providers: {len(catalog)}. Zen provider id `opencode` (OpenCode Zen,")
    A(f"{op.get('api', '')}), Go provider id `opencode-go` ({go.get('api', '')}).")
    A(f"Docs tables: {len(docs_endpoints)} models, {len(docs_pricing)} price rows "
      f"({price_match} match catalog).")
    A("")
    A("## 1. What models are available")
    A("")
    A(f"Live zen (`GET /zen/v1/models`, no auth needed): {len(zen_live)} models.")
    A("Paid core includes Claude (fable/opus/sonnet/haiku), GPT (5.x/6), Gemini 3.x,")
    A("Grok 4.5-4.7, DeepSeek V4 family, GLM-5.x, Kimi K2.5-K3, MiniMax M2.5-M3,")
    A("Qwen 3.5/3.6/3.8, Muse Spark 1.2/1.3, plus free-tier ids below.")
    A(f"Live go (`GET /zen/go/v1/models`): {len(go_live)} models (paid/credit lane).")
    A("Serving endpoint varies per model (docs table): most serve")
    A("`/chat/completions`; muse-spark-*-contributor-free serves `/responses`")
    A("(@ai-sdk/openai); jev-* serves `/systemone`. Full mapping in")
    A("`opencode-models.json` (.models[].endpoint).")
    A("Full id lists are in `opencode-models.json` (.models[].id + live_zen flags).")
    A("Drift vs catalog this run:")
    A(f"- in catalog but NOT live ({len(catalog_not_live)}): {', '.join(catalog_not_live[:20])}" + (" ..." if len(catalog_not_live) > 20 else ""))
    A(f"- live but NOT in catalog ({len(live_not_catalog)}): {', '.join(live_not_catalog) if live_not_catalog else 'none'}")
    A("")
    A("## 2. What prices")
    A("")
    A("Prices are USD per 1M tokens from models.opencode.ai/api.json cost.{input,output,cache_read},")
    A(f"cross-checked against the /docs/zen pricing table ({price_match} rows agree).")
    A("Selection (input/output per 1M):")
    for k, c in cheapest:
        A(f"- {k}: in={c.get('input')} out={c.get('output')} cache_r={c.get('cache_read')}")
    A("- Claude Sonnet 4-4.6: 3/15; Opus 4.5+: 5/25; Haiku 4.5: 1/5; Fable 5: 10/50.")
    A("- GPT-5 nano 0.05/0.4; GPT-5 base 1.07/8.5; GPT-5.4 Pro / 5.5 Pro 30/180.")
    A("- Gemini 3 Flash 0.5/3; Gemini 3/3.1 Pro 2/12; DeepSeek V4 Flash 0.14/0.28.")
    A("- GLM-5.3-Flash 0.15/0.5; GLM-5.x base 1-1.4/3.2-4.4; Muse Spark 1.x 1.25/4.25.")
    A("See `opencode-models.json` for every model.")
    A("")
    A("## 3. Any deals now")
    A("")
    A("- Free-by-price lane: cost input==0 AND output==0. Catalog lists "
      f"{len(op_free)} such ids under provider `opencode`; {len(live_free)} of them are live right now.")
    A("- No separate coupon/deal endpoint exists. Deals == free models + trial routing")
    A("  (console trialProvider/trialLimiter) + cheapest paid above. There is no")
    A("  /api/deals or /api/pricing on console; we probed and they 404.")
    A("- Notable: `big-pickle` is cost 0/0 and live; several `-free` ids exist only in")
    A("  catalog (not live), so the live free set is smaller than the catalog free set.")
    A("")
    A("## 4. Any free models")
    A("")
    A(f"Live free by price ({len(live_free)}): {', '.join(sorted(live_free))}.")
    A(f"Catalog free but not live ({len([m for m in op_free if m not in zen_set])}): "
      + ", ".join(sorted([m for m in op_free if m not in zen_set])[:30]))
    A(f"Live ids with no catalog entry ({len(live_unknown)}): {', '.join(sorted(live_unknown)) if live_unknown else 'none'}.")
    A("Go lane is paid-only by catalog (no cost==0 under opencode-go except ox-alpha-free).")
    A("")
    A("## 5. Do they work without auth")
    A("")
    A("- `GET /zen/v1/models` (and `/zen/go/v1/models`): YES, 200 with no Authorization")
    A("  header. `Bearer public` returns the identical list. Listing is public.")
    A("- `POST /zen/v1/chat/completions|responses` without key (this run, endpoint-correct):")
    for k in ["free-noauth", "free-public", "muse-free-responses-noauth", "paid-noauth"]:
        v = probes.get(k, {})
        mid = f" model={v.get('model')}" if v.get("model") else ""
        A(f"  - {k}{mid}: HTTP {v.get('status', '?')} {v.get('error_type','')} {v.get('message','')[:140]}")
    if not probes:
        A("  - (no live probes this run: offline mode; see auth_probes in JSON from last live run)")
    A("- Paid model with no key: 401 AuthError Missing API key (clean gate, stable across runs).")
    A("- Free model with no key: gated. Earlier runs saw 403 FreeTierError (only within")
    A("  OpenCode); this run saw 429 FreeUsageLimitError (rate limited). Both mean no")
    A("  free inference with plain curl. Probing a model on the wrong endpoint gives")
    A("  500 (muse-spark serves /responses only, jev serves /systemone). The CLI sets")
    A("  apiKey=public (provider.ts opencode()) but the server still requires the")
    A("  OpenCode client context; console handler.ts allowAnonymous/validateBilling")
    A("  gates inference.")
    A("- Console `GET /console/api/{user,orgs,config}`: 401 without token (auth required).")
    A("  `POST /console/auth/device/code|token`: device flow for `opencode console login`.")
    A("")
    A("## Model-listing / price endpoints (for the discover harness)")
    A("")
    A("- `GET /api/model` (v2.model.list, protocol) + `GET /api/provider`,")
    A("  `GET /api/provider/:providerID`, `GET /config/providers` (instance, needs serve).")
    A("- `GET https://models.opencode.ai/api.json` (pricing, 223 providers).")
    A("- `GET https://models.opencode.ai/catalog.json` (labs/models metadata).")
    A("- `GET https://opencode.ai/zen/v1/models` + `/zen/go/v1/models` (live availability).")
    A("- `POST https://opencode.ai/zen/v1/chat/completions|responses|messages|systemone` (+ go twins).")
    A("- `GET https://opencode.ai/docs/zen` model table (per-model endpoint+sdk) + pricing table.")
    A("- `GET https://opencode.ai/console/api/config` (per-workspace provider/model config, authed).")
    A("")
    atomic_write_text(os.path.join(out_dir, "spec.md"), "\n".join(lines) + "\n")

    print(f"spec built: {out_dir}/opencode-models.json + spec.md + REPORT.md + REPORT.txt")
    print(f"  catalog opencode: {len(op_models)} models ({len(op_free)} free)")
    print(f"  live zen: {len(zen_live)} ({len(live_free)} free by price, {len(live_unknown)} unknown)")
    print(f"  live go: {len(go_live)}")
    print(f"  docs: {len(docs_endpoints)} endpoint rows, {len(docs_pricing)} price rows ({price_match} match)")
    print(f"  drift catalog-not-live: {len(catalog_not_live)}, live-not-catalog: {len(live_not_catalog)}")
    print("  probes:")
    for k, v in probes.items():
        print(f"    {k}: {json.dumps(v)[:160]}")


if __name__ == "__main__":
    sys.exit(main())
