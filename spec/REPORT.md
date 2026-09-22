# opencode routes spec (20260922T103236Z (UTC))

## Counts

- catalog providers: 223
- catalog opencode models: 105 (32 free by price)
- live zen: 76 (9 free, 3 unknown)
- live go: 40

## Drift (catalog vs live)

- catalog-not-live (32): claude-3-5-haiku, claude-opus-4-1, gemini-3-pro, glm-4.6, glm-4.7, glm-4.7-free, glm-5-free, grok-code, hy3-free, hy3-preview-free, kimi-k2, kimi-k2-thinking, kimi-k2.5-free, laguna-s-2.1-free, ling-2.6-flash-free, ling-3.0-flash-free, ling-3.0-tiny-free, longcat-2.0-free, mimo-v2-flash-free, mimo-v2-omni-free, mimo-v2-pro-free, minimax-m2.1, minimax-m2.1-free, minimax-m2.5-free, minimax-m3-free
- live-not-catalog (3): grok-4.7, jev-1.13, jev-1.13-free

## Free models live
big-pickle, deepseek-v4-flash-free, ling-3.0-flash-fin-free, mimo-v2.5-free, mimo-v2.6-flash-free, muse-spark-1.2-contributor-free, muse-spark-1.3-contributor-free, nemotron-3-ultra-free, nemotron-3.5-lightning-free

## Cheapest paid (per 1M tokens)

| model | in | out | cache_read |
| --- | --- | --- | --- |
| gpt-5-nano | $0.05 | $0.4 | $0.005 |
| deepseek-v4-flash-vision-exp | $0.14 | $0.28 | $0.028 |
| deepseek-v4-flash | $0.14 | $0.28 | $0.028 |
| glm-5.3-flash | $0.15 | $0.5 | $0.03 |
| qwen3.8-flash | $0.15 | $0.47 | $0.016 |
| gpt-5.6-luna | $0.2 | $1.2 | $0.02 |
| gpt-5.4-nano | $0.2 | $1.25 | $0.02 |
| qwen3.5-plus | $0.2 | $1.2 | $0.02 |
| gpt-5.1-codex-mini | $0.25 | $2 | $0.025 |
| minimax-m2.1 | $0.3 | $1.2 | $0.1 |

## Auth probes

| probe | HTTP | model | type | message |
| --- | --- | --- | --- | --- |
| free-noauth | 429 | big-pickle | FreeUsageLimitError | Rate limit exceeded. Please try again later. |
| free-public | 429 | big-pickle | FreeUsageLimitError | Rate limit exceeded. Please try again later. |
| paid-noauth | 401 | claude-sonnet-4 | AuthError | Missing API key. |
| muse-free-responses-noauth | 429 | muse-spark-1.3-contributor-free | FreeUsageLimitError | Rate limit exceeded. Please try again later. |
| models-noauth | 200 |  |  |  |
| models-public | 200 |  |  |  |


## Per-model serving endpoint (from /docs/zen table)

Models with a docs-table entry: 66. Anything not listed serves /chat/completions.

| model | endpoint | sdk |
| --- | --- | --- |
| claude-fable-5 | /messages | @ai-sdk/anthropic |
| claude-fable-5-1 | /messages | @ai-sdk/anthropic |
| claude-haiku-4-5 | /messages | @ai-sdk/anthropic |
| claude-opus-4-5 | /messages | @ai-sdk/anthropic |
| claude-opus-4-6 | /messages | @ai-sdk/anthropic |
| claude-opus-4-7 | /messages | @ai-sdk/anthropic |
| claude-opus-4-8 | /messages | @ai-sdk/anthropic |
| claude-opus-5 | /messages | @ai-sdk/anthropic |
| claude-sonnet-4-5 | /messages | @ai-sdk/anthropic |
| claude-sonnet-4-6 | /messages | @ai-sdk/anthropic |
| claude-sonnet-5 | /messages | @ai-sdk/anthropic |
| gpt-5 | /responses | @ai-sdk/openai |
| gpt-5-codex | /responses | @ai-sdk/openai |
| gpt-5-nano | /responses | @ai-sdk/openai |
| gpt-5.1 | /responses | @ai-sdk/openai |
| gpt-5.1-codex | /responses | @ai-sdk/openai |
| gpt-5.1-codex-max | /responses | @ai-sdk/openai |
| gpt-5.1-codex-mini | /responses | @ai-sdk/openai |
| gpt-5.2 | /responses | @ai-sdk/openai |
| gpt-5.2-codex | /responses | @ai-sdk/openai |
| gpt-5.3-codex | /responses | @ai-sdk/openai |
| gpt-5.3-codex-spark | /responses | @ai-sdk/openai |
| gpt-5.4 | /responses | @ai-sdk/openai |
| gpt-5.4-mini | /responses | @ai-sdk/openai |
| gpt-5.4-nano | /responses | @ai-sdk/openai |
| gpt-5.4-pro | /responses | @ai-sdk/openai |
| gpt-5.5 | /responses | @ai-sdk/openai |
| gpt-5.5-pro | /responses | @ai-sdk/openai |
| gpt-5.6-luna | /responses | @ai-sdk/openai |
| gpt-5.6-sol | /responses | @ai-sdk/openai |
| gpt-5.6-terra | /responses | @ai-sdk/openai |
| gpt-6-astra | /responses | @ai-sdk/openai |
| grok-4.5 | /responses | @ai-sdk/openai |
| grok-4.6 | /responses | @ai-sdk/openai |
| grok-4.7 | /responses | @ai-sdk/openai |
| grok-build-0.1 | /responses | @ai-sdk/openai |
| muse-spark-1.2 | /responses | @ai-sdk/openai |
| muse-spark-1.3 | /responses | @ai-sdk/openai |
| muse-spark-1.3-contributor-free | /responses | @ai-sdk/openai |
| qwen3.5-plus | /messages | @ai-sdk/anthropic |
| qwen3.6-plus | /messages | @ai-sdk/anthropic |
| qwen3.7-max | /messages | @ai-sdk/anthropic |
| qwen3.7-plus | /messages | @ai-sdk/anthropic |
| qwen3.8-flash | /messages | @ai-sdk/anthropic |


## Full tables

- `opencode-models.json`: every model (prices, free, live flags, endpoint)
- `routes.json`: merged endpoint inventory with drift both ways
