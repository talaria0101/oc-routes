# CLI drive report (opencode, LD_PRELOAD tap + fetch hook runner)

17 runs, 204 captured events, 7 unique endpoints, 0 new surface. Window (UTC): 2026-09-22T09:28:12.210Z .. 2026-09-22T09:35:52.256Z.

## Runs

| Run | Exit | Time | Events |
| --- | --- | --- | --- |
| auth-login-opencode | ? | ?s | 87 |
| console-login | ? | ?s | 9 |
| login-device | ? | ?s | 0 |
| models-cached | ? | ?s | 0 |
| models-opencode | ? | ?s | 0 |
| models-opencode-go | ? | ?s | 0 |
| models-refresh | ? | ?s | 3 |
| models-zen-verbose | ? | ?s | 0 |
| orgs | ? | ?s | 0 |
| providers-list | ? | ?s | 0 |
| serve | ? | ?s | 1 |
| src-login | ? | ?s | 9 |
| src-models | ? | ?s | 6 |
| startup-check | ? | ?s | 0 |
| stats | ? | ?s | 0 |
| upgrade | ? | ?s | 0 |
| upgrade-check | ? | ?s | 0 |


## Endpoints observed

| Endpoint | Hits | Statuses | Seen in |
| --- | --- | --- | --- |
| GET models.opencode.ai/api.json | 2 |  | src-models |
| GET registry.npmjs.org/@opencode-ai%2fplugin | 1 |  | src-models |
| POST opencode.ai/console/auth/device/code | 1 |  | src-login |
| POST opencode.ai/console/auth/device/token | 83 |  | src-login |
| TLS models.opencode.ai:443 | 3 |  | auth-login-opencode, models-refresh, src-models |
| TLS opencode.ai:443 | 4 |  | console-login, src-login |
| TLS registry.npmjs.org:443 | 29 |  | auth-login-opencode, src-models |


## New surface (not in static tables)

none: every captured endpoint matches the modeled surface

## Tap host allowlist verdict

- [ok] `127.0.0.1` hits=1 via dns
- [ok] `models.opencode.ai` hits=8 via fetch,proxy_connect,tls_sni
- [ok] `opencode.ai` hits=96 via dns,fetch,proxy_connect,tls_sni
- [ok] `registry.npmjs.org` hits=59 via fetch,proxy_connect,tls_sni

Verdict: PASS - no unknown hosts.

## Limits

- No TCP bind in the sandbox: capture is in-process (LD_PRELOAD dns/connect/SNI tap + fetch hook), same host visibility as a TLS proxy.
- Raw logs stay in this directory; curated tables above are what to cite.

## Reproduce

```sh
./harness/mitm/drive.sh captures/  # live CLI battery (needs binary + network)
python3 harness/mitm/analyze.py --caps captures  # offline re-curation
```
