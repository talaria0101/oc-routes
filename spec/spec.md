# opencode models spec (20260922T103236Z UTC)

Drift-free snapshot built live by `harness/build_spec.py` (guarded: aborts
before writing rather than publishing bad data). Rerun to refresh.
Sources: models.opencode.ai/api.json (pricing), opencode.ai/zen/v1/models and
/zen/go/v1/models (live), /docs/zen model+pricing tables (S8), endpoint-correct
probes, plus src pins (provider.ts opencode() gate, console handler.ts gate).

Catalog providers: 223. Zen provider id `opencode` (OpenCode Zen,
https://opencode.ai/zen/v1), Go provider id `opencode-go` (https://opencode.ai/zen/go/v1).
Docs tables: 66 models, 86 price rows (57 match catalog).

## 1. What models are available

Live zen (`GET /zen/v1/models`, no auth needed): 76 models.
Paid core includes Claude (fable/opus/sonnet/haiku), GPT (5.x/6), Gemini 3.x,
Grok 4.5-4.7, DeepSeek V4 family, GLM-5.x, Kimi K2.5-K3, MiniMax M2.5-M3,
Qwen 3.5/3.6/3.8, Muse Spark 1.2/1.3, plus free-tier ids below.
Live go (`GET /zen/go/v1/models`): 40 models (paid/credit lane).
Serving endpoint varies per model (docs table): most serve
`/chat/completions`; muse-spark-*-contributor-free serves `/responses`
(@ai-sdk/openai); jev-* serves `/systemone`. Full mapping in
`opencode-models.json` (.models[].endpoint).
Full id lists are in `opencode-models.json` (.models[].id + live_zen flags).
Drift vs catalog this run:
- in catalog but NOT live (32): claude-3-5-haiku, claude-opus-4-1, gemini-3-pro, glm-4.6, glm-4.7, glm-4.7-free, glm-5-free, grok-code, hy3-free, hy3-preview-free, kimi-k2, kimi-k2-thinking, kimi-k2.5-free, laguna-s-2.1-free, ling-2.6-flash-free, ling-3.0-flash-free, ling-3.0-tiny-free, longcat-2.0-free, mimo-v2-flash-free, mimo-v2-omni-free ...
- live but NOT in catalog (3): grok-4.7, jev-1.13, jev-1.13-free

## 2. What prices

Prices are USD per 1M tokens from models.opencode.ai/api.json cost.{input,output,cache_read},
cross-checked against the /docs/zen pricing table (57 rows agree).
Selection (input/output per 1M):
- gpt-5-nano: in=0.05 out=0.4 cache_r=0.005
- deepseek-v4-flash-vision-exp: in=0.14 out=0.28 cache_r=0.028
- deepseek-v4-flash: in=0.14 out=0.28 cache_r=0.028
- glm-5.3-flash: in=0.15 out=0.5 cache_r=0.03
- qwen3.8-flash: in=0.15 out=0.47 cache_r=0.016
- gpt-5.6-luna: in=0.2 out=1.2 cache_r=0.02
- gpt-5.4-nano: in=0.2 out=1.25 cache_r=0.02
- qwen3.5-plus: in=0.2 out=1.2 cache_r=0.02
- gpt-5.1-codex-mini: in=0.25 out=2 cache_r=0.025
- minimax-m2.1: in=0.3 out=1.2 cache_r=0.1
- Claude Sonnet 4-4.6: 3/15; Opus 4.5+: 5/25; Haiku 4.5: 1/5; Fable 5: 10/50.
- GPT-5 nano 0.05/0.4; GPT-5 base 1.07/8.5; GPT-5.4 Pro / 5.5 Pro 30/180.
- Gemini 3 Flash 0.5/3; Gemini 3/3.1 Pro 2/12; DeepSeek V4 Flash 0.14/0.28.
- GLM-5.3-Flash 0.15/0.5; GLM-5.x base 1-1.4/3.2-4.4; Muse Spark 1.x 1.25/4.25.
See `opencode-models.json` for every model.

## 3. Any deals now

- Free-by-price lane: cost input==0 AND output==0. Catalog lists 32 such ids under provider `opencode`; 9 of them are live right now.
- No separate coupon/deal endpoint exists. Deals == free models + trial routing
  (console trialProvider/trialLimiter) + cheapest paid above. There is no
  /api/deals or /api/pricing on console; we probed and they 404.
- Notable: `big-pickle` is cost 0/0 and live; several `-free` ids exist only in
  catalog (not live), so the live free set is smaller than the catalog free set.

## 4. Any free models

Live free by price (9): big-pickle, deepseek-v4-flash-free, ling-3.0-flash-fin-free, mimo-v2.5-free, mimo-v2.6-flash-free, muse-spark-1.2-contributor-free, muse-spark-1.3-contributor-free, nemotron-3-ultra-free, nemotron-3.5-lightning-free.
Catalog free but not live (23): glm-4.7-free, glm-5-free, grok-code, hy3-free, hy3-preview-free, kimi-k2.5-free, laguna-s-2.1-free, ling-2.6-flash-free, ling-3.0-flash-free, ling-3.0-tiny-free, longcat-2.0-free, mimo-v2-flash-free, mimo-v2-omni-free, mimo-v2-pro-free, minimax-m2.1-free, minimax-m2.5-free, minimax-m3-free, nemotron-3-super-free, north-mini-code-free, qwen3.6-plus-free, ring-2.6-1t-free, trinity-large-preview-free, x-preview-f-free
Live ids with no catalog entry (3): grok-4.7, jev-1.13, jev-1.13-free.
Go lane is paid-only by catalog (no cost==0 under opencode-go except ox-alpha-free).

## 5. Do they work without auth

- `GET /zen/v1/models` (and `/zen/go/v1/models`): YES, 200 with no Authorization
  header. `Bearer public` returns the identical list. Listing is public.
- `POST /zen/v1/chat/completions|responses` without key (this run, endpoint-correct):
  - free-noauth model=big-pickle: HTTP 429 FreeUsageLimitError Rate limit exceeded. Please try again later.
  - free-public model=big-pickle: HTTP 429 FreeUsageLimitError Rate limit exceeded. Please try again later.
  - muse-free-responses-noauth model=muse-spark-1.3-contributor-free: HTTP 429 FreeUsageLimitError Rate limit exceeded. Please try again later.
  - paid-noauth model=claude-sonnet-4: HTTP 401 AuthError Missing API key.
- Paid model with no key: 401 AuthError Missing API key (clean gate, stable across runs).
- Free model with no key: gated. Earlier runs saw 403 FreeTierError (only within
  OpenCode); this run saw 429 FreeUsageLimitError (rate limited). Both mean no
  free inference with plain curl. Probing a model on the wrong endpoint gives
  500 (muse-spark serves /responses only, jev serves /systemone). The CLI sets
  apiKey=public (provider.ts opencode()) but the server still requires the
  OpenCode client context; console handler.ts allowAnonymous/validateBilling
  gates inference.
- Console `GET /console/api/{user,orgs,config}`: 401 without token (auth required).
  `POST /console/auth/device/code|token`: device flow for `opencode console login`.

## Model-listing / price endpoints (for the discover harness)

- `GET /api/model` (v2.model.list, protocol) + `GET /api/provider`,
  `GET /api/provider/:providerID`, `GET /config/providers` (instance, needs serve).
- `GET https://models.opencode.ai/api.json` (pricing, 223 providers).
- `GET https://models.opencode.ai/catalog.json` (labs/models metadata).
- `GET https://opencode.ai/zen/v1/models` + `/zen/go/v1/models` (live availability).
- `POST https://opencode.ai/zen/v1/chat/completions|responses|messages|systemone` (+ go twins).
- `GET https://opencode.ai/docs/zen` model table (per-model endpoint+sdk) + pricing table.
- `GET https://opencode.ai/console/api/config` (per-workspace provider/model config, authed).

