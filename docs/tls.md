# TLS channel: browser ↔ Overstate ↔ salt-api

Both hops run TLS with certificates. There is no mTLS (grill decision):
authentication is tokens (salt-api eauth) plus network policy.

## Dev PKI

`scripts/gen-dev-certs.sh` creates a self-signed CA and server certs in
`salt-config/tls/` (gitignored, never commit). `scripts/dev-up.sh` runs it
automatically when certs are missing.

- `ca.crt` — dev CA. Mounted into the app as the salt-api trust anchor
  (`SALT_API_VERIFY_CA=/srv/tls/ca.crt`).
- `api.crt`/`api.key` — installed as the salt-api server cert by
  `scripts/enable-api-tls.sh` (SAN: `salt-master`, `localhost`, `127.0.0.1`).
- `app.crt`/`app.key` — served by gunicorn via `TLS_CERT`/`TLS_KEY`.

## Why salt-api needs a post-boot install step

The master image regenerates its own self-signed cert and appends its
`rest_cherrypy` block to the main config file at every boot. So neither a
bind-mount over its cert paths (its startup deletes them and exits) nor
`ssl_crt`/`ssl_key` in our mounted `api.conf` (the main file wins the merge
for nested keys) can take effect declaratively. `enable-api-tls.sh` copies
our certs over the image's paths and restarts salt-api; `dev-up.sh` and
`dev-rebuild.sh` run it automatically. Re-run it after any master recreate.

## Verification behavior

`SaltClient` verifies TLS by default (`verify=True`, system CAs).
`SALT_API_VERIFY_CA` overrides it: a path uses that CA bundle, the literal
`false` disables verification (local dev only, against self-signed masters).

## Production checklist

- Use a real master setup where your `ssl_crt`/`ssl_key` actually win, or
  keep an equivalent post-boot install; verify with
  `curl --cacert <ca> https://<master>:8000/login`.
- Never set `SALT_API_VERIFY_CA=false` outside local dev.
- The app serves 8000/TLS when `TLS_CERT`/`TLS_KEY` are mounted, otherwise
  plain HTTP with a warning.
