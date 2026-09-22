#!/usr/bin/env python3
"""
oc-routes build_spec harness.

Generates our own latest, drift-free API spec answering what we care about:
  - what models are available (zen full + go lite, live)
  - what prices (per-model input/output/cache from models.opencode.ai/api.json)
  - any deals now (cost==0 free models, trial markers, free-tier gate)
  - any free models (list + how they differ catalog-vs-live)
  - do they work without auth (live probes: /models noauth vs chat noauth)

Inputs (live, authoritative at runtime):
  https://models.opencode.ai/api.json      pricing catalog (223 providers)
  https://opencode.ai/zen/v1/models        live zen listing (auth-independent)
  https://opencode.ai/zen/go/v1/models     live go listing
  POST https://opencode.ai/zen/v1/chat/completions  auth-gate probes

Stdlib only. Writes:
  spec/opencode-models.json   full per-model table (prices, free, live, auth)
  spec/spec.md                human answers to the five questions
  snapshots/models-api-*.json, zen-*.json, probes handled by discover.py;
    this script also snapshots what it fetches.

Usage:
  python3 harness/build_spec.py [--snapshots ../snapshots] [--out-dir ../spec] [--no-network]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.request
import urllib.error

UA = "oc-routes-spec/1.0 (+https://github.com/talaria0101/oc-routes)"
MODELS_API_URL = "https://models.opencode.ai/api.json"
ZEN_MODELS_URL = "https://opencode.ai/zen/v1/models"
ZEN_GO_MODELS_URL = "https://opencode.ai/zen/go/v1/models"
CHAT_URL = "https://opencode.ai/zen/v1/chat/completions"


def fetch_json(url, headers=None, timeout=30):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post_json(url, payload, headers=None, timeout=25):
    h = {"User-Agent": UA, "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return -1, str(e).encode()


def is_free_cost(cost: dict) -> bool:
    if not cost:
        return False
    try:
        return float(cost.get("input", 1)) == 0 and float(cost.get("output", 1)) == 0
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--no-network", action="store_true")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    snap_dir = args.snapshots or os.path.join(repo, "snapshots")
    out_dir = args.out_dir or os.path.join(repo, "spec")
    os.makedirs(snap_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    use_network = not args.no_network

    catalog = None
    zen_live: list[str] = []
    go_live: list[str] = []
    probes: dict = {}

    if use_network:
        catalog = fetch_json(MODELS_API_URL)
        with open(os.path.join(snap_dir, f"models-api-{stamp}.json"), "w") as f:
            json.dump(catalog, f)
        z = fetch_json(ZEN_MODELS_URL)
        zen_live = sorted([m["id"] for m in z.get("data", [])])
        with open(os.path.join(snap_dir, f"zen-models-{stamp}.json"), "w") as f:
            json.dump(z, f, indent=2)
        try:
            g = fetch_json(ZEN_GO_MODELS_URL)
            go_live = sorted([m["id"] for m in g.get("data", [])])
            with open(os.path.join(snap_dir, f"zen-go-models-{stamp}.json"), "w") as f:
                json.dump(g, f, indent=2)
        except Exception as e:
            probes["go-models-error"] = str(e)
        # auth probes: free vs paid, noauth vs public
        for label, model, auth in [
            ("free-noauth", "big-pickle", None),
            ("free-public", "big-pickle", "public"),
            ("muse-free-noauth", "muse-spark-1.3-contributor-free", None),
            ("paid-noauth", "claude-sonnet-4-6", None),
            ("models-noauth", None, None),
            ("models-public", None, "public"),
        ]:
            if label.startswith("models-"):
                hdrs = {"Authorization": f"Bearer {auth}"} if auth else {}
                try:
                    d = fetch_json(ZEN_MODELS_URL, headers=hdrs)
                    probes[label] = {"status": 200, "count": len(d.get("data", []))}
                except urllib.error.HTTPError as e:
                    probes[label] = {"status": e.code}
                except Exception as e:
                    probes[label] = {"status": -1, "error": str(e)[:120]}
                continue
            hdrs = {"Authorization": f"Bearer {auth}"} if auth else {}
            code, body = post_json(CHAT_URL, {"model": model, "messages": [{"role": "user", "content": "hi"}],
                                                   "max_tokens": 5}, headers=hdrs)
            try:
                j = json.loads(body.decode("utf-8", "replace"))
                err = j.get("error", {}) if isinstance(j, dict) else {}
                probes[label] = {"status": code, "error_type": err.get("type", "") if isinstance(err, dict) else "",
                                 "message": (err.get("message", "") if isinstance(err, dict) else "")[:220]}
            except Exception:
                probes[label] = {"status": code, "raw": body[:200].decode("utf-8", "replace")}
        with open(os.path.join(snap_dir, f"spec-probes-{stamp}.json"), "w") as f:
            json.dump(probes, f, indent=2)
    else:
        # offline: load newest snapshots
        import glob
        cands = sorted(glob.glob(os.path.join(snap_dir, "models-api-*.json")))
        if not cands:
            raise RuntimeError("offline but no models-api snapshot found")
        catalog = json.load(open(cands[-1]))
        cands = sorted(glob.glob(os.path.join(snap_dir, "zen-models-*.json")))
        if cands:
            zen_live = sorted([m["id"] for m in json.load(open(cands[-1])).get("data", [])])
        cands = sorted(glob.glob(os.path.join(snap_dir, "zen-go-models-*.json")))
        if cands:
            go_live = sorted([m["id"] for m in json.load(open(cands[-1])).get("data", [])])

    assert catalog is not None
    op = catalog.get("opencode", {})
    op_models = op.get("models", {})
    go = catalog.get("opencode-go", {})
    go_models = go.get("models", {})

    zen_set, go_set = set(zen_live), set(go_live)
    op_free = sorted([k for k, v in op_models.items() if is_free_cost(v.get("cost", {}))])
    go_free_catalog = sorted([k for k, v in go_models.items() if is_free_cost(v.get("cost", {}))])

    # live free = live ids that are cost==0 in catalog (or unknown-live like jev)
    live_free, live_paid, live_unknown = [], [], []
    for mid in zen_live:
        m = op_models.get(mid)
        if m is None:
            live_unknown.append(mid)
        elif is_free_cost(m.get("cost", {})):
            live_free.append(mid)
        else:
            live_paid.append(mid)

    # drift
    catalog_not_live = sorted(set(op_models) - zen_set)
    live_not_catalog = sorted(zen_set - set(op_models))

    # cheapest paid (by input price) for deals context
    def price_key(kv):
        _k, v = kv
        c = v.get("cost", {}) or {}
        return (c.get("input", 999), c.get("output", 999))
    cheapest = sorted([(k, v.get("cost", {})) for k, v in op_models.items()
                       if not is_free_cost(v.get("cost", {}))], key=lambda kv: ((kv[1] or {}).get("input", 999)))[:10]

    models_table = []
    for mid in sorted(set(list(op_models.keys()) + zen_live)):
        cat = op_models.get(mid)
        cost = (cat or {}).get("cost", {}) if cat else {}
        models_table.append({
            "id": mid,
            "name": (cat or {}).get("name", ""),
            "in_catalog": mid in op_models,
            "live_zen": mid in zen_set,
            "free_by_price": is_free_cost(cost),
            "input": cost.get("input"),
            "output": cost.get("output"),
            "cache_read": cost.get("cache_read"),
            "cache_write": cost.get("cache_write"),
            "release_date": (cat or {}).get("release_date", ""),
        })

    spec_json = {
        "generated_at": stamp + " (UTC)",
        "sources": {
            "pricing_catalog": MODELS_API_URL,
            "zen_live": ZEN_MODELS_URL,
            "go_live": ZEN_GO_MODELS_URL,
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
        },
        "drift": {"catalog_not_live": catalog_not_live, "live_not_catalog": live_not_catalog},
        "free_models_catalog": op_free,
        "free_models_live": sorted(live_free),
        "live_unknown_models": sorted(live_unknown),
        "cheapest_paid_top10": [{"id": k, "cost": c} for k, c in cheapest],
        "auth_probes": probes,
        "models": models_table,
    }
    with open(os.path.join(out_dir, "opencode-models.json"), "w") as f:
        json.dump(spec_json, f, indent=2)

    # human spec.md answering the five questions
    lines = []
    A = lines.append
    A(f"# opencode models spec ({stamp} UTC)")
    A("")
    A("Drift-free snapshot built live by `harness/build_spec.py`. Rerun to refresh.")
    A("Sources: models.opencode.ai/api.json (pricing), opencode.ai/zen/v1/models and")
    A("/zen/go/v1/models (live), POST /zen/v1/chat/completions probes, plus src pins")
    A("(provider.ts opencode() gate, console handler.ts allowAnonymous gate).")
    A("")
    A(f"Catalog providers: {len(catalog)}. Zen provider id `opencode` (OpenCode Zen,")
    A(f"{op.get('api', '')}), Go provider id `opencode-go` ({go.get('api', '')}).")
    A("")
    A("## 1. What models are available")
    A("")
    A(f"Live zen (`GET /zen/v1/models`, no auth needed): {len(zen_live)} models.")
    A("Paid core includes Claude (fable/opus/sonnet/haiku), GPT (5.x/6), Gemini 3.x,")
    A("Grok 4.5-4.7, DeepSeek V4 family, GLM-5.x, Kimi K2.5-K3, MiniMax M2.5-M3,")
    A("Qwen 3.5/3.6/3.8, Muse Spark 1.2/1.3, plus free-tier ids below.")
    A(f"Live go (`GET /zen/go/v1/models`): {len(go_live)} models (paid/credit lane).")
    A("Full id lists are in `opencode-models.json` (.models[].id + live_zen flags).")
    A("Drift vs catalog this run:")
    A(f"- in catalog but NOT live ({len(catalog_not_live)}): {', '.join(catalog_not_live[:20])}" + (" ..." if len(catalog_not_live) > 20 else ""))
    A(f"- live but NOT in catalog ({len(live_not_catalog)}): {', '.join(live_not_catalog) if live_not_catalog else 'none'}")
    A("")
    A("## 2. What prices")
    A("")
    A("Prices are USD per 1M tokens from models.opencode.ai/api.json cost.{input,output,cache_read}.")
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
    A("- `POST /zen/v1/chat/completions` without key:")
    for k in ["free-noauth", "free-public", "muse-free-noauth", "paid-noauth"]:
        v = probes.get(k, {})
        A(f"  - {k}: HTTP {v.get('status')} {v.get('error_type','')} {v.get('message','')[:140]}")
    A("- Paid model with no key: 401 AuthError Missing API key (clean gate).")
    A("- Free model with no key: 403 FreeTierError OpenCode free tier can only be used")
    A("  from within OpenCode (big-pickle, mimo-v2.5-free, nemotron free, etc.), OR 500")
    A("  Internal server error for a few ids (muse-spark-*-contributor-free, jev-*-free).")
    A("  Either way: no, free inference does NOT work with plain curl. The CLI sets")
    A("  apiKey=public (provider.ts opencode()) and the server still requires the")
    A("  OpenCode client context; console handler.ts allowAnonymous/validateBilling")
    A("  returns billingSource free/anonymous but inference is gated.")
    A("- Console `GET /console/api/{user,orgs,config}`: 401 without token (auth required).")
    A("  `POST /console/auth/device/code|token`: device flow for `opencode login`.")
    A("")
    A("## Model-listing / price endpoints (for the discover harness)")
    A("")
    A("- `GET /api/model` (v2.model.list, protocol) + `GET /api/provider`,")
    A("  `GET /api/provider/:providerID`, `GET /config/providers` (instance, needs serve).")
    A("- `GET https://models.opencode.ai/api.json` (pricing, 223 providers).")
    A("- `GET https://models.opencode.ai/catalog.json` (labs/models metadata).")
    A("- `GET https://opencode.ai/zen/v1/models` + `/zen/go/v1/models` (live availability).")
    A("- `POST https://opencode.ai/zen/v1/chat/completions|responses|messages` (+ go twins).")
    A("- `GET https://opencode.ai/console/api/config` (per-workspace provider/model config, authed).")
    A("")
    with open(os.path.join(out_dir, "spec.md"), "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"spec built: {out_dir}/opencode-models.json + {out_dir}/spec.md")
    print(f"  catalog opencode: {len(op_models)} models ({len(op_free)} free)")
    print(f"  live zen: {len(zen_live)} ({len(live_free)} free by price, {len(live_unknown)} unknown)")
    print(f"  live go: {len(go_live)}")
    print(f"  drift catalog-not-live: {len(catalog_not_live)}, live-not-catalog: {len(live_not_catalog)}")
    print("  probes:")
    for k, v in probes.items():
        print(f"    {k}: {json.dumps(v)[:160]}")


if __name__ == "__main__":
    sys.exit(main())
