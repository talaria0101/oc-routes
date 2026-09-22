/* oc-routes fetch tap: bun/node --preload module logging plaintext URLs.
 *
 * Sees method + full URL + auth presence BEFORE TLS, for every request made
 * through global fetch or node http/https. Complements preload.c (which sees
 * hosts for any binary): run the CLI from source with this preloaded and you
 * get exact paths, including any the static inventory missed.
 *
 * Use:
 *   bun --preload /workspace/oc-routes/harness/mitm/tap_fetch.js \
 *     ./src/index.ts models --refresh
 *   OC_ROUTES_FETCHLOG=/tmp/fetch.jsonl node --import ... (same file works)
 *
 * Log: JSONL {ts, pid, method, url, host, path, authed}. Bodies never logged.
 */
const fs = require("fs");
const path = require("path");

const LOG = process.env.OC_ROUTES_FETCHLOG || "/tmp/oc-routes-fetch.jsonl";

function append(obj) {
  try {
    obj.ts = new Date().toISOString();
    obj.pid = process.pid;
    fs.appendFileSync(LOG, JSON.stringify(obj) + "\n");
  } catch { /* never break the target */ }
}

function describeUrl(u) {
  try {
    const p = new URL(u);
    return { host: p.hostname, path: p.pathname || "/" };
  } catch {
    return { host: "", path: String(u).slice(0, 200) };
  }
}

function logFetch(method, url, headers) {
  const { host, path } = describeUrl(url);
  let authed = false;
  try {
    if (headers) {
      if (typeof headers.get === "function") authed = !!headers.get("authorization");
      else if (typeof headers === "object")
        authed = !!headers.authorization || !!headers.Authorization;
    }
  } catch { /* ignore */ }
  append({ ev: "fetch", method: String(method || "GET").toUpperCase(), url: String(url).slice(0, 2000), host, path, authed });
}

try {
  const origFetch = globalThis.fetch;
  if (typeof origFetch === "function") {
    globalThis.fetch = function (input, init) {
      try {
        const url = typeof input === "string" ? input : input?.url || String(input);
        logFetch(init?.method || input?.method || "GET", url, init?.headers || input?.headers);
      } catch { /* ignore */ }
      return origFetch.apply(this, arguments);
    };
  }
} catch { /* ignore */ }

try {
  const http = require("http");
  const https = require("https");
  for (const mod of [http, https]) {
    const orig = mod.request;
    mod.request = function (a, b, c) {
      try {
        let url = "";
        if (typeof a === "string") url = a;
        else if (a instanceof URL) url = a.href;
        else if (a && typeof a === "object")
          url = `${mod === https ? "https" : "http"}://${a.hostname || a.host || "?"}${a.path || a.pathname || "/"}`;
        logFetch((a?.method || b?.method || "GET"), url, a?.headers || b?.headers);
      } catch { /* ignore */ }
      return orig.apply(this, arguments);
    };
    const origGet = mod.get;
    mod.get = function () {
      try {
        return mod.request.apply(this, arguments).end();
      } catch { return origGet.apply(this, arguments); }
    };
  }
} catch { /* ignore */ }

try {
  append({ ev: "tap_ready", argv: process.argv.slice(0, 8).join(" ").slice(0, 300) });
} catch { /* ignore */ }
