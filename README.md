# oc-routes

Drift-proof inventory of every opencode API endpoint, with a curated spec
answering what we actually care about: models, prices, deals, free models,
and whether they work without auth.

No hardcoded route table. Rerun the harness and the live sources are
re-fetched; src parsing only flags drift.

## Sources enumerated (not just the first match)

- S1 live instance OpenAPI: `https://opencode.ai/v2/openapi.json`
  (136 method+path rows on 2026-09-22; 113 paths). Authoritative for the
  local `opencode serve` surface. Human docs mirror: `/v2/docs/api`.
- S2 src static analysis: `packages/opencode/src/server/routes/instance/httpapi/groups/*.ts`
  (20 groups) + `packages/protocol/src/groups/*.ts` (19 groups). Parses every
  `HttpApiEndpoint.get/post/put/patch/delete(name, route)` plus `*Paths`
  objects. On 2026-09-22: 188 endpoint rows from 39 group files.
- S3 console file routes: `packages/console/app/src/routes/**` (SolidStart
  file routing). 114 rows. Covers `zen/v1/*`, `zen/go/v1/*`, `api/*`,
  `auth/*`, workspace/billing pages.
- S4 SaaS catalog: `https://models.opencode.ai/api.json` (223 providers,
  pricing) + `https://models.opencode.ai/catalog.json` (422 labs/models).
  Pricing source of truth; large payloads are refetched, not committed.
- S5 zen/go live: `GET /zen/v1/models`, `GET /zen/go/v1/models`,
  `POST /zen/v1/chat/completions|responses|messages` (+ go twins),
  `GET /zen/v1/models/:model`. Probed noauth vs `Bearer public`.
- S6 console auth/config: `GET /console/api/{user,orgs,config}` (401 without
  token), `POST /console/auth/device/{code,token}` (device flow for
  `opencode login`). Pins: `packages/opencode/src/account/account.ts`,
  `packages/core/src/plugin/provider/opencode.ts`.
- S7 CLI surface: `packages/opencode/src/cli/cmd/*.ts` (`models [provider]`,
  `providers`, `login/logout/orgs`, `serve`, `auth`, `run`, `debug/*`, ...).
  `opencode models --verbose` prints per-model cost metadata; `--refresh`
  refetches the models cache.
- S8 docs tables: `https://opencode.ai/docs/zen` embeds a per-model table
  (id, serving endpoint, SDK package) and a pricing table (per 1M tokens).
  Parsed live every run: per-model endpoint mapping (muse-spark serves
  `/responses` only, jev serves `/systemone`) plus a price cross-check
  against the catalog (57/86 rows agree; mismatches recorded in-spec).
  The site JS bundle has no pricing data (checked, negative result).

Src checkout used for this pass: `anomalyco/opencode @ fe3f3a4` (dev,
2026-09-22). The harness accepts any checkout via `--src`.

## Quick start

```bash
./run.sh                          # full refresh (needs network)
./run.sh --offline                # rebuild specs from snapshots only
./run.sh --report-only            # re-render all MD/TXT twins offline
./run.sh --src /path/to/opencode  # pin a checkout for S2/S3
./run.sh --force                  # override validation refusals (records WARNING)
python3 harness/discover.py --report-only     # routes twins only
python3 harness/build_spec.py --report-only   # spec twins only
python3 harness/mitm/analyze.py --report-only --caps captures  # captures twins
```

Stdlib only (python3, no pip deps). `curl` not required. Gentle by
 design: sequential requests, short timeouts, no auth (except `Bearer
 public` comparisons), no retries. Quota instruments stop at the wall.

Guards (good data is never overwritten with bad data): every fetch is
 validated (HTTP 200, min bytes, JSON shape, sane counts) and every JSON
 write is atomic (tmp + rename) with the prior kept as `.prev.json`
 (local, gitignored). Validation gates refuse the overwrite with exit 2:
 spec needs non-empty models plus no 50pc count collapse vs prior plus
 model-listing present; inventory needs non-empty merged plus no 50pc
 collapse; captures need runs plus events plus endpoints plus tap files.
 `--force` overrides and logs WARNING. Missing/corrupt snapshots or specs
 exit 2 with a clean message, never a traceback. Large `models-api` /
 `catalog` snapshots are gitignored and refetched live.

## What the harness does

`harness/discover.py` merges S1+S2+S3+S5+S6 into one inventory keyed by
`(METHOD, path)`, scores every route against model/price/deal/free/billing
keywords, and prints the interesting table highest-first. It also reports
drift both ways (`src-not-live`, `live-not-src`) with `/api`-prefix
normalization (instance groups declare bare `/session`, served under `/api`).

`harness/build_spec.py` fetches the pricing catalog + live zen/go listings,
probes the auth gate (free-noauth, free-public, paid-noauth, models-noauth),
and writes the drift-free spec.

## MITM drive: the CLI under a tap (`harness/mitm/`)

Static analysis can miss endpoints, so the harness also drives the real CLI
1.18.32 and records everything it touches. The sandbox denies INET bind(2)
and ptrace, so a listen-socket proxy is impossible here; the instrument is
equivalent without listening:

- `preload.c` -> `netlog.so` (LD_PRELOAD, C, no deps): logs every
  `getaddrinfo` (DNS intent), `connect` (real peer), `execve` (subprocess
  spawns), and scans `send` / `write` for `CONNECT host:port` proxy lines
  and TLS ClientHello SNI. Works on any binary, encrypted traffic
  included, nothing bound, no CA.
- `tap_fetch.js` (`bun --preload`): logs plaintext method + full URL for
  every fetch/http request when running the CLI from source.
- `drive.sh`: safe battery (`timeout -k`, stdin /dev/null) over models,
  providers, stats, console login, auth login, serve. Writes
  `captures/manifest.json`; analyzer falls back to inferred runs without it.
- `pty_drive.py`: pty runner with absolute deadline + SIGKILL for
  interactive flows (console login polls forever, ignores SIGTERM).
  Exits 3 where the sandbox has no pty devices; drive.sh path covers it.
- `analyze.py`: diffs captured hosts/URLs against the inventory. PASS on
  2026-09-22: 3 real hosts (models.opencode.ai, opencode.ai,
  registry.npmjs.org), 4 exact URLs, zero outside the modeled surface.
- `mitm.py`: classic intercepting proxy for open networks (needs bind).

Raw evidence in `captures/` (net-*.log host taps, fetch-*.jsonl exact URLs).

## Answers (2026-09-22, see `spec/spec.md` + `spec/opencode-models.json`)

- Available: live zen 76 models, live go 40. Catalog has 105 under `opencode`
  (32 free by price) but only 9 free are live; 32 catalog ids not live, 3 live
  ids not in catalog (`grok-4.7`, `jev-1.13`, `jev-1.13-free`).
- Prices: USD/1M tokens from `cost.{input,output,cache_read}`. Cheapest paid:
  gpt-5-nano 0.05/0.4, deepseek-v4-flash 0.14/0.28, glm-5.3-flash 0.15/0.5.
  Sonnet 3/15, Opus 5/25, Fable 10/50, GPT-5.4 Pro 30/180.
- Deals: no `/api/deals` endpoint (404). Deals == free-by-price lane + trial
  routing + cheapest paid. `big-pickle` (0/0, live) is the notable free id.
- Free live (9): big-pickle, deepseek-v4-flash-free,
  ling-3.0-flash-fin-free, mimo-v2.5-free, mimo-v2.6-flash-free,
  muse-spark-1.2-contributor-free, muse-spark-1.3-contributor-free,
  nemotron-3-ultra-free, nemotron-3.5-lightning-free.
- Without auth: `GET /zen/v1/models` YES (200, identical with `Bearer public`).
  `POST /zen/v1/chat/completions|responses` NO: paid -> 401 Missing API key
  (stable); free -> gated (403 FreeTierError on earlier runs, 429
  FreeUsageLimitError this run). Wrong-endpoint probes give 500
  (muse-spark serves /responses only, jev serves /systemone, claude serves
  /messages). Console `/api/{user,orgs,config}` -> 401. CLI unauth keeps
  only `cost.input==0` with `apiKey=public` (`provider.ts opencode()`), but
  inference still gated server-side (`handler.ts` allowAnonymous/validateBilling).
- Docs cross-check: 57/86 price rows agree; 2 real mismatches recorded
  (Kimi K2.5 cache_read docs 0.1 vs catalog 0.08; DeepSeek V4 Pro output
  docs 3.48 vs catalog 3.84).

## What we took from cc-routes (sister repo)

- Every JSON artifact gets MD + TXT twins rendered from the JSON itself,
  with `--report-only` offline re-render on all three harnesses.
- Guarded writes: validate (HTTP 200, min bytes, shape, sane counts) then
  atomic tmp+fsync+rename; abort before writing on failure, never publish
  empty data. Snapshot dirs keep latest-per-kind only.
- MITM curation: `endpoints.json` + run/endpoint tables + `NEW_ENDPOINTS.md`
  surfacing only runtime-new surface, plus subprocess-spawn capture
  (here via `execve` in the preload tap) and a `manifest.json` run log.
- Best-effort auxiliaries must not kill the core spec (docs tables warn
  and continue; go listing warns and continues).
- Probe models picked live from the current listing, never a pinned id list.

## Layout

- `harness/discover.py` - multi-source endpoint finder + model/price/deal printer
- `harness/build_spec.py` - latest drift-free models/prices/deals/auth spec builder
- `spec/routes.json` + `routes-REPORT.md`/`.txt` - merged inventory twins
- `spec/opencode-models.json` + `REPORT.md`/`.txt` - per-model table twins
- `spec/spec.md` - long-form answers to the five questions
- `captures/endpoints.json` + `tap-hosts.json` + `REPORT.md`/`.txt` +
  `NEW_ENDPOINTS.md` - curated CLI-drive evidence (tap allowlist verdict
  and hook blind spots included; committed device codes scrubbed to ***).
  Raw taps stay beside them; exit 0 clean, 1 unknown hosts, 2 refusal.
- `harness/guards.py` - fetch validators + atomic writes (no bad overwrites)
- `harness/report_lib.py` - shared MD/TXT renderers (offline re-render)
- `snapshots/` - small raw fetches (openapi, zen listings, probes). Large
  `models-api-*.json` / `catalog-*.json` are refetched live, gitignored.
- `run.sh` - one-command refresh
