# Before you make it public

grumpy's `/sessions` and `/verdict` endpoints are bearer-gated, but the answer page (`/s/{token}`) is intentionally open — the token in the URL is the only credential, so anyone with the link can answer it. Before pointing a real `GRUMPY_BASE_URL` at the public internet:

- **Put a reverse proxy or CDN in front of it that rate-limits and caps request body size.** grumpy has no built-in rate limiting — an in-process limiter would be false security the moment you run more than one replica.
- **Redact `/s/{token}` from your proxy's access logs.** That path *is* a bearer-equivalent secret. grumpy keeps it out of its own structured logs and disables uvicorn's access log for this reason, but a default nginx/Caddy line in front of it will happily write the token to disk.
- **Set spend limits on your Anthropic API key.** Real grading is one synchronous Claude call per screen submitted, plus two calls when the session is created (one to read the diff, one to write the questions), with no built-in per-deployment budget.
- **Treat `GRUMPY_TOKEN` as a real secret**, and set `GRUMPY_ALLOWED_REPOS` if you want a leaked token to not be usable against arbitrary repos.
- **Diffs are stored in Postgres in plaintext** — scope database access like it holds source code, because it does.

grumpy does ship a few defaults out of the box: interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are disabled; `Referrer-Policy`, `X-Content-Type-Options`, `X-Frame-Options` and a `Content-Security-Policy` of `default-src 'none'; style-src 'unsafe-inline'; script-src 'self'` are sent on every response (the only script allowed is grumpy's own same-origin paste guard — no inline script, no `img-src`, no `connect-src` — so the pages make no outbound requests at all, and there is nothing a session URL can leak to); request bodies are capped against bytes actually received rather than a client-supplied `Content-Length`; transient model-API failures are retried with backoff instead of surfacing to the developer; and session tokens are kept out of the logs.
