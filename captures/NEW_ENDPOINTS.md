# Runtime-observed endpoints missing from static tables

Observed 7 unique endpoints (204 events); 0 not covered by static/spec tables.

None: every runtime endpoint is already in the static tables.

## Tap host allowlist verdict

- [ok] `127.0.0.1` hits=1 via dns (local serve target (bind fails in sandbox; ServeError))
- [ok] `models.opencode.ai` hits=8 via fetch,proxy_connect,tls_sni (S4 pricing catalog; ModelsDev fetch ${source}/api.json)
- [ok] `opencode.ai` hits=96 via dns,fetch,proxy_connect,tls_sni (S5/S6 zen inference + console device flow + docs site)
- [ok] `registry.npmjs.org` hits=59 via fetch,proxy_connect,tls_sni (npm-config.ts default registry; Installation.latest + Npm metadata)
