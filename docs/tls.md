# TLS / reverse proxy

MediaBridge is designed for internal hosting, but the web UI still handles a login
password and session cookies, so traffic between a browser and the server is
encrypted rather than sent in the clear.

## How it works

An `nginx` service (`docker-compose.yml`, config in `nginx/`) is the only container
that publishes a port for web traffic. It:

- Redirects plain HTTP (port 80) to HTTPS (port 443).
- Terminates TLS using a self-signed cert (`./certs/server.crt` / `server.key`).
- Proxies everything to the `web` container over plain HTTP on the internal Docker
  network — that hop is trusted (private compose network, not exposed to the host
  or internet), so it isn't re-encrypted.
- Sets standard hardening headers (HSTS, `X-Frame-Options`, `X-Content-Type-Options`,
  `Referrer-Policy`), hides the Nginx version, rate-limits `/login`, and blocks
  dotfile paths.

`web` itself trusts `X-Forwarded-Proto` from the proxy (via uvicorn's
`ProxyHeadersMiddleware`) and marks the session cookie `Secure`, since it's only
ever reached through nginx.

## Generating the cert

```bash
./setup.sh --configure-tls
```

Prompts for the hostname or IP you'll use to reach the server (default `localhost`)
and writes a self-signed cert valid for ~825 days into `./certs/` (gitignored), along
with a backup copy at `certs/self-signed.crt` / `self-signed.key` used by Settings'
"revert to self-signed" action. It won't overwrite an existing cert — delete both
`certs/server.*` files first to regenerate (e.g. after the hostname/IP changes, or
the cert expires).

Because the cert is self-signed, browsers will show a trust warning on first
visit — expected for an internally-hosted server. If you want to avoid the
warning, add `certs/server.crt` to your OS/browser's trusted store, or replace it
with a cert from your own internal CA (same filenames, same mount point).

## Custom domains & user-pasted certs

Settings > TLS / Domain has two sections: **Domains** (which hostnames nginx
responds to, plus an external-address label) and **Certificate** (the cert/key
nginx presents, shared across all domains, plus an HTTP-redirect toggle):

- **Domains** is a textarea, one hostname or IP per line — e.g.
  `mediabridge.example.com`. This is the standard "which hosts should this server
  actually answer for" control: `web/app/tls.py` parses it into an nginx
  `server_name` entry, written to `./nginx/generated/domains.conf` (which
  `nginx/nginx.conf` `include`s), so nginx only responds to the hostnames
  listed here rather than blindly matching everything — most useful when the
  installed cert is a wildcard (e.g. `*.example.com`) covering more than the
  one host you actually intend to serve. An empty list falls back to the
  original `server_name _` catch-all (answer to anything). nginx always
  listens on the standard 80/443 *inside* its container regardless of what's
  listed here — mapping those to whatever host port(s) you actually want
  reachable (e.g. `9443:443` in `docker-compose.yml`) is entirely a Docker
  port-mapping / external-proxy concern, out of scope for this app to
  configure.
- Also in that section, **External hosting address** is a free-text,
  purely-informational field — not a domain-matching control, and nothing
  reads it programmatically. It exists so the actual, currently-true address
  this instance is reached at is recorded somewhere explicit rather than
  needing to be reverse-engineered from network/proxy setup later — e.g.
  `https://192.0.2.10:9443` for a direct mapped-port address, or a public
  domain if it's fronted by an external reverse proxy you manage separately.
- **Certificate**: nginx always loads `certs/server.crt` / `certs/server.key`
  — fixed paths, shared by every domain listed above (one cert/key pair, not
  per-domain). `web` validates the pair (matching public key, not expired)
  and atomically overwrites those same two files.
- The **redirect HTTP to HTTPS** checkbox controls whether the generated
  config includes a `listen 80` server that 301s to HTTPS; unchecked, nginx
  doesn't listen on port 80 at all.
- The cert's metadata (upload time, `is_custom`) plus the domains list and
  redirect flag are tracked in a `TlsConfig` singleton row (plaintext in
  Postgres, consistent with `CloudStorageConfig` etc.); the cert/key bytes
  themselves stay on disk under `./certs`, never in the database.
- `./setup.sh --configure-tls` also copies the generated self-signed pair to
  `certs/self-signed.crt` / `self-signed.key` as a backup. "Revert to
  self-signed" in Settings copies those back over the active pair — no need to
  regenerate.
- nginx doesn't hot-reload a changed cert file or config on its own — saving
  domains or a cert triggers a full container restart of `nginx` through
  `terminator` (`terminator`'s service allowlist now includes `nginx`
  alongside `worker`), gated by the same `TERMINATOR_API_KEY`. A few seconds
  of reconnect is an acceptable cost for a manual, occasional action. `web`
  also regenerates `domains.conf` from the DB on every startup, so it can't
  drift if the generated file's volume is ever reset.
- Still internal-hosting scoped: no ACME/Let's Encrypt auto-renewal for custom
  domains — you supply and rotate your own cert.

## Known limitations

- `client_max_body_size` is currently unlimited (`0`) in `nginx/nginx.conf` because
  it's not yet confirmed whether any web-facing route streams large file bodies
  through the `web` container directly (as opposed to local-filesystem/GCS access
  from the worker). Tighten this once that's measured — see `docs/TODO.md`.
- This setup assumes an internal, trusted network. It is not hardened for direct
  internet exposure (no WAF, no automatic cert renewal/ACME, no fail2ban-style
  banning beyond the login rate limit).
