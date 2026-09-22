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

Src checkout used for this pass: `anomalyco/opencode @ fe3f3a4` (dev,
2026-09-22). The harness accepts any checkout via `--src`.

## Quick start

```bash
./run.sh                          # discover + build spec (needs network)
./run.sh --src /path/to/opencode  # pin a checkout for S2/S3
python3 harness/discover.py --help
python3 harness/build_spec.py --help
python3 harness/discover.py --no-network   # offline from snapshots/
```

Stdlib only (python3, no pip deps). `curl` not required.

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
  `getaddrinfo` (DNS intent), `connect` (real peer), and scans `send` /
  `write` for `CONNECT host:port` proxy lines and TLS ClientHello SNI.
  Works on any binary, encrypted traffic included, nothing bound, no CA.
- `tap_fetch.js` (`bun --preload`): logs plaintext method + full URL for
  every fetch/http request when running the CLI from source.
- `drive.sh`: safe battery (`timeout -k`, stdin /dev/null) over models,
  providers, stats, console login, auth login, serve.
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
  `POST /zen/v1/chat/completions` NO: paid -> 401 Missing API key; free ->
  403 FreeTierError (only within OpenCode) or 500 for a few ids. Console
  `/api/{user,orgs,config}` -> 401. CLI unauth keeps only `cost.input==0`
  with `apiKey=public` (`provider.ts opencode()`), but inference still gated
  server-side (`handler.ts` allowAnonymous/validateBilling).

## Layout

- `harness/discover.py` - multi-source endpoint finder + model/price/deal printer
- `harness/build_spec.py` - latest drift-free models/prices/deals/auth spec builder
- `spec/routes.json` - merged inventory (393 unique method+path, interesting ranked)
- `spec/opencode-models.json` - per-model table (prices, free, live, catalog flags)
- `spec/spec.md` - human answers to the five questions
- `snapshots/` - small raw fetches (openapi, zen listings, probes). Large
  `models-api-*.json` / `catalog-*.json` are refetched live, gitignored.
- `run.sh` - one-command refresh
