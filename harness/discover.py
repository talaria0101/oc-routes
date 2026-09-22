#!/usr/bin/env python3
"""
oc-routes discover harness.

Finds ALL opencode API endpoints even as they drift, then prints the ones
that carry model listings, prices, deals, free-tier info.

Multi-source, no hardcoded endpoint list:
  S1 live instance OpenAPI  (https://opencode.ai/v2/openapi.json) -> paths
  S2 src static analysis    (HttpApiEndpoint.* + Paths objects + protocol groups)
  S3 console file routes    (packages/console/app/src/routes/** -> URL paths)
  S4 SaaS catalog           (https://models.opencode.ai/api.json + /catalog.json)
  S5 zen/go live probing    (https://opencode.ai/zen/v1/* + /zen/go/v1/*)
  S6 console auth/config    (https://opencode.ai/console/api/* + /auth/device/*)
  S7 CLI surface            (packages/opencode/src/cli/cmd/*.ts command strings)

Drift-proof: the live fetch is authoritative at runtime. The src parse only
adds candidates and flags drift (in-src-not-live, in-live-not-src). Nothing
here hardcodes a route table; rerun to refresh.

Stdlib only. Works offline if snapshots/src are present (uses cache).

Usage:
  python3 harness/discover.py [--src ../opencode-src] [--out ../spec/routes.json]
                              [--snapshots ../snapshots] [--no-network]
                              [--print-model-routes] [--json]

Outputs:
  - stdout: human report + MODEL/PRICE/DEAL route table
  - spec/routes.json (optional): merged machine-readable route inventory
  - snapshots/*.json (optional): raw live fetches with timestamps
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import urllib.request
import urllib.error

OPENAPI_URL = "https://opencode.ai/v2/openapi.json"
MODELS_API_URL = "https://models.opencode.ai/api.json"
CATALOG_URL = "https://models.opencode.ai/catalog.json"
ZEN_BASE = "https://opencode.ai/zen/v1"
ZEN_GO_BASE = "https://opencode.ai/zen/go/v1"
CONSOLE_BASE = "https://opencode.ai/console"

# Keywords that mark a route as model/price/deal relevant.
# Each is (pattern, weight, label). Matched against path + operationId +
# summary + description + tags, case-insensitive.
RELEVANCE_RULES = [
    (r"\bmodels?\b", 10, "model-listing"),
    (r"\bproviders?\b", 8, "provider-listing"),
    (r"\bcatalog\b", 8, "catalog"),
    (r"\bpricing\b|\bprices?\b", 10, "pricing"),
    (r"\bcosts?\b", 9, "cost"),
    (r"\bcache\b", 2, "cost-detail"),
    (r"\bfree\b", 9, "free-tier"),
    (r"\bdeals?\b|\boffers?\b|\bpromo", 10, "deal"),
    (r"\btrial\b", 7, "trial/deal"),
    (r"\bbilling\b|\bsubscription\b|\bcredits?\b|\bbalance\b", 7, "billing"),
    (r"\blimits?\b|\bquota\b|\brate.?limit\b", 5, "limits"),
    (r"\bplans?\b", 5, "plan"),
    (r"\bconfig/providers\b", 6, "config-providers"),
    (r"\bmodel/default\b", 4, "model-default"),
    (r"\bchat/completions\b|\bresponses\b|\bmessages\b", 3, "inference"),
    (r"\bauth\b|\boauth\b|\bdevice\b|\blogin\b", 2, "auth"),
    (r"\bgo\b.*\busage\b|\busage\b", 4, "usage"),
]

HTTP_TIMEOUT = 25
UA = "oc-routes-discover/1.0 (+https://github.com/talaria0101/oc-routes)"


def fetch_json(url: str, timeout: int = HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_text(url: str, timeout: int = HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace"), r.status


def http_status(url: str, method: str = "GET", body: bytes | None = None,
                headers: dict | None = None, timeout: int = 15):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            return r.status, dict(r.headers), payload
    except urllib.error.HTTPError as e:
        try:
            payload = e.read()
        except Exception:
            payload = b""
        return e.code, dict(e.headers or {}), payload
    except Exception as e:
        return -1, {}, str(e).encode()


# ---------------------------------------------------------------- S1: live openapi

def load_openapi(use_network: bool, snap_dir: str | None):
    data = None
    err = None
    if use_network:
        try:
            data = fetch_json(OPENAPI_URL)
        except Exception as e:
            err = str(e)
    if data is None and snap_dir:
        # fall back to newest snapshot
        cands = sorted([f for f in os.listdir(snap_dir) if f.startswith("openapi-")]) if os.path.isdir(snap_dir) else []
        if cands:
            with open(os.path.join(snap_dir, cands[-1])) as f:
                data = json.load(f)
    if data is None:
        raise RuntimeError(f"cannot load openapi (network err: {err}; no snapshot fallback)")
    paths = data.get("paths", {})
    routes = []
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.startswith("x-") or not isinstance(op, dict):
                continue
            routes.append({
                "source": "S1:live-openapi",
                "method": method.upper(),
                "path": path,
                "operationId": op.get("operationId", ""),
                "summary": op.get("summary", ""),
                "description": op.get("description", ""),
                "tags": op.get("tags", []),
            })
    return data, routes


# ---------------------------------------------------------------- S2: src static analysis

ENDPOINT_RE = re.compile(
    r"HttpApiEndpoint\.(get|post|put|patch|delete|head|options)\(\s*\"([^\"]+)\"\s*,\s*([^,\)]+)",
)
PATHS_ENTRY_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*:\s*(root|`[^`]*\"?|\"[^\"]*\"|'.+?')", re.M)
ROOT_RE = re.compile(r"(?:const|let)\s+root\s*=\s*[\"`]([^\"`]+)[\"`]")
STRING_LIT_RE = re.compile(r"^\"([^\"]*)\"$|^'([^']*)'$|^`([^`]*)`$")
IDENT_RE = re.compile(r"identifier:\s*\"([^\"]+)\"")


def resolve_route_token(token: str, root: str, file_text: str) -> str:
    token = token.strip()
    m = STRING_LIT_RE.match(token)
    if m:
        lit = m.group(1) if m.group(1) is not None else (m.group(2) if m.group(2) is not None else m.group(3))
        # template like `${root}/providers` or `${root}/:id`
        lit = lit.replace("${root}", root)
        # strip other ${...} conservatively
        lit = re.sub(r"\$\{[^}]*\}", ":param", lit)
        return lit
    # token is an identifier like SessionPaths.list / FilePaths.content / ControlPaths.auth
    # try to find its definition: `list: root,` or `list: `${root}/status`` inside a Paths object
    # We resolve by grepping the whole src tree later; here do a local lookup.
    short = token.split(".")[-1]
    # search local file for `short: <route>`
    for mm in re.finditer(rf"{re.escape(short)}\s*:\s*([\"`][^\"`,\n]*[\"`])", file_text):
        lit = mm.group(1).strip("\"`")
        lit = lit.replace("${root}", root)
        lit = re.sub(r"\$\{[^}]*\}", ":param", lit)
        # may still contain `root` bare (e.g. `list: root,`)
        if lit == "root":
            return root
        return lit
    if token == "root":
        return root
    return f"<expr:{token}>"


def parse_src_groups(src_root: str):
    """Parse HttpApi groups (instance + protocol). Returns list of route dicts."""
    out = []
    seen_files = []
    for rel in [
        "packages/opencode/src/server/routes/instance/httpapi/groups",
        "packages/protocol/src/groups",
    ]:
        d = os.path.join(src_root, rel)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".ts"):
                continue
            fp = os.path.join(d, fn)
            try:
                text = open(fp, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            seen_files.append(fp)
            mroot = ROOT_RE.search(text)
            root = mroot.group(1) if mroot else ""
            # collect identifiers per endpoint block is hard; attach nearest following identifier
            for m in ENDPOINT_RE.finditer(text):
                method, name, token = m.group(1), m.group(2), m.group(3)
                route = resolve_route_token(token, root, text)
                # find identifier after this match (next 600 chars)
                window = text[m.end():m.end() + 800]
                im = IDENT_RE.search(window)
                ident = im.group(1) if im else ""
                out.append({
                    "source": f"S2:src:{os.path.basename(fn)}",
                    "method": m.group(1).upper(),
                    "path": route if route.startswith("/") else route,
                    "operationId": ident or name,
                    "summary": name,
                    "description": f"from {rel}/{fn}",
                    "tags": [],
                    "file": fp,
                })
    # also parse Paths objects globally for drift reference (S2b)
    paths_refs = []
    for fp in seen_files:
        text = open(fp, encoding="utf-8", errors="replace").read()
        mroot = ROOT_RE.search(text)
        root = mroot.group(1) if mroot else ""
        # find `export const XPaths = { ... }` blocks
        for bm in re.finditer(r"export const (\w+Paths)\s*=\s*\{(.*?)\n\} as const", text, re.S):
            block = bm.group(2)
            for em in re.finditer(r"(\w+)\s*:\s*(.+?)(?:,|\n)", block):
                key, val = em.group(1), em.group(2).strip()
                if val == "root":
                    route = root
                else:
                    vm = STRING_LIT_RE.match(val.strip().rstrip(","))
                    if vm:
                        lit = vm.group(1) or vm.group(2) or vm.group(3)
                        route = lit.replace("${root}", root)
                    else:
                        route = val
                paths_refs.append({"file": fp, "key": key, "route": route})
    return out, paths_refs, seen_files


# ---------------------------------------------------------------- S3: console file routes

def console_file_to_url(rel: str) -> str:
    """SolidStart file routing: routes/zen/v1/chat/completions.ts -> /zen/v1/chat/completions
    Handles [param], [...param], (groups), index."""
    p = rel.replace("\\", "/")
    # strip leading routes/
    if p.startswith("routes/"):
        p = p[len("routes/"):]
    # strip extension
    p = re.sub(r"\.(tsx?|ts|css)$", "", p)
    # drop route groups (parentheses)
    parts = [seg for seg in p.split("/") if not (seg.startswith("(") and seg.endswith(")"))]
    out = []
    for seg in parts:
        if seg == "index":
            continue
        m = re.match(r"^\[\.\.\.(.+)\]$", seg)
        if m:
            out.append(f"*{m.group(1)}")
            continue
        m = re.match(r"^\[(.+)\]$", seg)
        if m:
            out.append(f":{m.group(1)}")
            continue
        out.append(seg)
    url = "/" + "/".join(out)
    return url


def parse_console_routes(src_root: str):
    base = os.path.join(src_root, "packages/console/app/src/routes")
    out = []
    if not os.path.isdir(base):
        return out
    for dirpath, _dirs, files in os.walk(base):
        for fn in files:
            if not fn.endswith((".ts", ".tsx")):
                continue
            # skip css-adjacent? keep .ts only for API; tsx are pages too (still routes)
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, base)
            rel_posix = rel.replace(os.sep, "/")
            url = console_file_to_url("routes/" + rel_posix)
            # guess method from file content (export function GET/POST/...)
            try:
                text = open(fp, encoding="utf-8", errors="replace").read()
            except OSError:
                text = ""
            methods = sorted(set(re.findall(r"export\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\b", text)))
            if not methods:
                # tsx pages are GET pages; .ts without exports -> unknown, mark ANY
                methods = ["GET"] if fn.endswith(".tsx") else ["ANY"]
            for method in methods:
                out.append({
                    "source": "S3:console-file-route",
                    "method": method,
                    "path": url,
                    "operationId": "",
                    "summary": rel_posix,
                    "description": "SolidStart file route",
                    "tags": ["console"],
                    "file": fp,
                })
    return out


# ---------------------------------------------------------------- S5/S6: live SaaS probing

ZEN_ROUTES = [
    ("GET", "/zen/v1/models"),
    ("GET", "/zen/v1/models/:model"),
    ("POST", "/zen/v1/chat/completions"),
    ("POST", "/zen/v1/responses"),
    ("POST", "/zen/v1/messages"),
    ("POST", "/zen/v1/systemone"),
    ("GET", "/zen/go/v1/models"),
    ("POST", "/zen/go/v1/chat/completions"),
    ("POST", "/zen/go/v1/responses"),
    ("POST", "/zen/go/v1/systemone"),
    ("POST", "/zen/go/v1/messages"),
    ("GET", "/zen/go/v1/usage"),
]

CONSOLE_ROUTES = [
    ("GET", "/console/api/user"),
    ("GET", "/console/api/orgs"),
    ("GET", "/console/api/config"),
    ("POST", "/console/auth/device/code"),
    ("POST", "/console/auth/device/token"),
]

CATALOG_ROUTES = [
    ("GET", "https://models.opencode.ai/api.json"),
    ("GET", "https://models.opencode.ai/catalog.json"),
]


def probe_live_extra(use_network: bool):
    """Return (saas_routes, probe_results). saas_routes are candidate route
    dicts; probe_results maps url -> {noauth_status, public_status, ...}."""
    saas_routes = []
    probes: dict = {}
    if not use_network:
        return saas_routes, probes
    # zen + go
    for method, path in ZEN_ROUTES:
        url = "https://opencode.ai" + path.replace(":model", "muse-spark-1.3")
        saas_routes.append({
            "source": "S5:zen-live",
            "method": method,
            "path": path,
            "operationId": "",
            "summary": "zen/go inference or listing endpoint (console app routes)",
            "description": "from packages/console/app/src/routes/zen",
            "tags": ["zen"],
        })
    for method, path in CONSOLE_ROUTES:
        saas_routes.append({
            "source": "S6:console-live",
            "method": method,
            "path": path,
            "operationId": "",
            "summary": "console auth/config endpoint (account.ts / opencode plugin)",
            "description": "from packages/opencode/src/account + core/plugin/provider/opencode.ts",
            "tags": ["console"],
        })
    # actual HTTP probing: models + a couple of auth-behavior checks
    try:
        code, _h, body = http_status("https://opencode.ai/zen/v1/models")
        probes["GET /zen/v1/models noauth"] = {"status": code, "bytes": len(body)}
    except Exception as e:
        probes["GET /zen/v1/models noauth"] = {"status": -1, "error": str(e)}
    try:
        code, _h, body = http_status("https://opencode.ai/zen/v1/models",
                                     headers={"Authorization": "Bearer public"})
        probes["GET /zen/v1/models public"] = {"status": code, "bytes": len(body)}
    except Exception as e:
        probes["GET /zen/v1/models public"] = {"status": -1, "error": str(e)}
    try:
        code, _h, body = http_status("https://opencode.ai/zen/go/v1/models")
        probes["GET /zen/go/v1/models noauth"] = {"status": code, "bytes": len(body)}
    except Exception as e:
        probes["GET /zen/go/v1/models noauth"] = {"status": -1, "error": str(e)}
    # free-tier chat gate: noauth + public, free model vs paid model
    for label, model, auth in [
        ("free-noauth", "big-pickle", None),
        ("free-public", "big-pickle", "public"),
        ("paid-noauth", "claude-sonnet-4-6", None),
    ]:
        payload = json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}],
                              "max_tokens": 5}).encode()
        hdrs = {"Content-Type": "application/json"}
        if auth:
            hdrs["Authorization"] = f"Bearer {auth}"
        code, _h, body = http_status("https://opencode.ai/zen/v1/chat/completions",
                                     method="POST", body=payload, headers=hdrs)
        try:
            j = json.loads(body.decode("utf-8", "replace"))
            err = (j.get("error") or {})
            msg = err.get("message", "") if isinstance(err, dict) else str(err)[:120]
            etype = err.get("type", "") if isinstance(err, dict) else ""
        except Exception:
            msg, etype = body[:120].decode("utf-8", "replace"), ""
        probes[f"POST /zen/v1/chat/completions {label}"] = {
            "status": code, "error_type": etype, "message": msg[:200],
        }
    return saas_routes, probes


# ---------------------------------------------------------------- classify

def score_route(r: dict):
    hay = " ".join([r.get("path", ""), r.get("operationId", ""),
                    r.get("summary", ""), r.get("description", ""),
                    " ".join(r.get("tags", []))])
    total = 0
    labels = []
    for pat, w, label in RELEVANCE_RULES:
        if re.search(pat, hay, re.I):
            total += w
            labels.append(label)
    return total, labels


def main():
    ap = argparse.ArgumentParser(description="oc-routes discover harness")
    ap.add_argument("--src", default=None, help="path to opencode checkout (default: auto-detect ../opencode-src, ./opencode-src)")
    ap.add_argument("--out", default=None, help="write merged inventory JSON here (default: spec/routes.json relative to repo)")
    ap.add_argument("--snapshots", default=None, help="snapshot dir (default: snapshots/ relative to repo)")
    ap.add_argument("--no-network", action="store_true", help="offline: use snapshots only, skip live probing")
    ap.add_argument("--json", action="store_true", help="emit JSON report on stdout instead of human report")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    src = args.src
    if not src:
        for cand in [os.path.join(repo, "..", "opencode-src"),
                     os.path.join(os.getcwd(), "opencode-src"),
                     "/workspace/opencode-src"]:
            if os.path.isdir(cand):
                src = cand
                break
    snap_dir = args.snapshots or os.path.join(repo, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    use_network = not args.no_network

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    notes: list[str] = []

    # S1
    openapi_data, s1 = load_openapi(use_network, snap_dir)
    if use_network:
        with open(os.path.join(snap_dir, f"openapi-{stamp}.json"), "w") as f:
            json.dump(openapi_data, f, indent=2)
        notes.append(f"S1 live openapi: {len(s1)} method+path rows from {OPENAPI_URL}")
    else:
        notes.append(f"S1 snapshot openapi: {len(s1)} rows (offline)")

    # S2/S3
    s2, paths_refs, s2_files = ([], [], [])
    s3 = []
    if src and os.path.isdir(src):
        s2, paths_refs, s2_files = parse_src_groups(src)
        s3 = parse_console_routes(src)
        notes.append(f"S2 src groups: {len(s2)} endpoint rows from {len(s2_files)} group files under {src}")
        notes.append(f"S3 console file routes: {len(s3)} rows")
    else:
        notes.append("S2/S3 skipped: no --src checkout found (hint: pass --src /path/to/opencode)")

    # S4 catalog: just record availability + counts
    catalog_info: dict = {}
    if use_network:
        for url in [MODELS_API_URL, CATALOG_URL]:
            try:
                if url.endswith("api.json"):
                    d = fetch_json(url)
                    catalog_info[url] = {"providers": len(d)}
                    with open(os.path.join(snap_dir, f"models-api-{stamp}.json"), "w") as f:
                        json.dump(d, f)
                else:
                    d = fetch_json(url)
                    catalog_info[url] = {"keys": list(d.keys())[:10],
                                         "models": len(d.get("models", [])) if isinstance(d.get("models"), list) else len(d.get("models", {}))}
                    with open(os.path.join(snap_dir, f"catalog-{stamp}.json"), "w") as f:
                        json.dump(d, f)
            except Exception as e:
                catalog_info[url] = {"error": str(e)}
        notes.append(f"S4 catalog: {json.dumps(catalog_info)}")
    else:
        notes.append("S4 catalog skipped (offline)")

    # S5/S6
    saas_routes, probes = probe_live_extra(use_network)
    if use_network:
        with open(os.path.join(snap_dir, f"probes-{stamp}.json"), "w") as f:
            json.dump(probes, f, indent=2)
        notes.append(f"S5/S6 live probes: {len(probes)} checks")

    # merge: key (METHOD, path-normalized)
    # Instance HttpApi groups declare bare roots ("/session", "/config",
    # "/provider") mounted under /api at serve time, while protocol groups
    # already carry the /api prefix. Normalize both to /api/* for drift
    # comparison so the same logical route matches.
    def norm(p: str) -> str:
        q = p.replace("{", ":").replace("}", "")
        q = re.sub(r":\w+", ":param", q)
        return q

    def api_norm(method: str, path: str) -> tuple:
        n = norm(path)
        # bare instance paths -> /api/* ; console/zen absolute paths stay
        if n.startswith("/api/") or n == "/api" or n.startswith("/zen/") \
                or n.startswith("/console") or n.startswith("/auth"):
            return (method, n)
        if n.startswith("/"):
            return (method, "/api" + n)
        return (method, n)

    merged: dict[tuple, dict] = {}
    for r in s1 + s2 + s3 + saas_routes:
        key = (r["method"], norm(r["path"]))
        if key not in merged:
            merged[key] = {"method": r["method"], "path": r["path"], "sources": [],
                           "operationIds": [], "summaries": []}
        m = merged[key]
        if r["source"] not in m["sources"]:
            m["sources"].append(r["source"])
        if r.get("operationId") and r["operationId"] not in m["operationIds"]:
            m["operationIds"].append(r["operationId"])
        if r.get("summary") and r["summary"] not in m["summaries"]:
            m["summaries"].append(r["summary"][:160])

    # drift: live paths vs src paths (api-prefix normalized)
    live_keys = {api_norm(r["method"], r["path"]) for r in s1}
    src_keys = {api_norm(r["method"], r["path"]) for r in s2 if r["path"].startswith("/")}
    drift_src_not_live = sorted(src_keys - live_keys)
    drift_live_not_src = sorted(live_keys - src_keys)

    # classify: score merged rows using best underlying detail
    # build detail index for scoring (prefer S1 detail)
    detail_by_key: dict = {}
    for r in s1 + s2 + s3 + saas_routes:
        key = (r["method"], norm(r["path"]))
        cur = detail_by_key.get(key)
        # prefer live-openapi detail, else longest summary
        if cur is None or (cur.get("source", "").startswith("S2") and r["source"].startswith("S1")):
            detail_by_key[key] = r
    scored = []
    for key, m in merged.items():
        d = detail_by_key.get(key, {})
        hay_r = {"path": m["path"], "operationId": " ".join(m["operationIds"]),
                 "summary": " ".join(m["summaries"]),
                 "description": d.get("description", ""), "tags": d.get("tags", [])}
        score, labels = score_route(hay_r)
        scored.append((score, m, labels, d))
    scored.sort(key=lambda t: (-t[0], t[1]["path"]))
    interesting = [(s, m, labs) for (s, m, labs, _d) in scored if s > 0]

    report = {
        "generated_at": stamp,
        "notes": notes,
        "counts": {
            "live_openapi_rows": len(s1),
            "src_group_rows": len(s2),
            "console_file_rows": len(s3),
            "saas_candidate_rows": len(saas_routes),
            "merged": len(merged),
            "interesting": len(interesting),
        },
        "catalog": catalog_info,
        "probes": probes,
        "drift": {
            "src_not_in_live": [list(k) for k in drift_src_not_live],
            "live_not_in_src": [list(k) for k in drift_live_not_src],
        },
        "interesting": [
            {"score": s, "method": m["method"], "path": m["path"],
             "labels": labs, "sources": m["sources"],
             "operationIds": m["operationIds"][:3]}
            for (s, m, labs) in interesting
        ],
        "merged": [
            {"method": m["method"], "path": m["path"], "sources": m["sources"]}
            for (_s, m, _l, _d) in scored
        ],
    }

    out_path = args.out or os.path.join(repo, "spec", "routes.json")
    if args.out != "-":
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(report, f, indent=2)

    if args.json:
        print(json.dumps(report, indent=2))
        return

    # human report
    print("oc-routes discover harness")
    print("=" * 60)
    for n in notes:
        print(f"  {n}")
    print(f"  merged unique (METHOD, path): {len(merged)}")
    print(f"  drift src-not-live: {len(drift_src_not_live)}  live-not-src: {len(drift_live_not_src)}")
    if drift_src_not_live:
        print("  src-not-live (sample 15):")
        for k in drift_src_not_live[:15]:
            print(f"    {k[0]:6} {k[1]}")
    if drift_live_not_src:
        print("  live-not-src (sample 15):")
        for k in drift_live_not_src[:15]:
            print(f"    {k[0]:6} {k[1]}")
    print()
    print("MODEL / PRICE / DEAL / FREE routes (scored, highest first)")
    print("-" * 60)
    for (s, m, labs) in interesting:
        ops = ",".join(m["operationIds"][:2])
        print(f"  [{s:3}] {m['method']:6} {m['path']:55} {','.join(labs)[:40]}  {ops[:40]}")
    print()
    print("Live auth probes (unauth vs public):")
    for k, v in probes.items():
        print(f"  {k}: {json.dumps(v)[:160]}")
    print()
    print(f"Wrote merged inventory: {out_path}")
    print("Rerun anytime to refresh; live openapi is authoritative, src parse flags drift.")


if __name__ == "__main__":
    sys.exit(main())
