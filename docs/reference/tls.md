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
for nested keys) can take effect declaratively.

- **Dev** (`scripts/enable-api-tls.sh`): copies the CA-signed cert and
key over `/etc/pki/tls/certs/localhost.crt|.key` and runs
`supervisorctl restart salt-api`. `dev-up.sh` and `dev-rebuild.sh` invoke
it automatically. Re-run it after any master recreate.
- **Kubernetes** (`deploy/kubernetes/salt-master.yaml`): a `postStart`
lifecycle hook on the salt-master container does the same swap. The leaf
cert is mounted at `/run/secrets/overstate-salt-api` (a *different* path
from the image's regen path — mounting over the image's path would
make startup delete-and-exit). The hook polls up to 120s for the
image's regenerated cert to appear (proving the one-shot entrypoint
regen has finished), copies the leaf over the image's path, waits for
the supervisord socket, then `supervisorctl restart salt-api` so the
running process picks up the new cert. The hook always exits 0 (a
transient failure logs to `/tmp/tls-swap.log` on the pod and leaves
the master on the image's self-signed cert — the same state as before
this hook existed), so a hook failure never crash-loops the pod. The
certs themselves are issued by cert-manager from
`deploy/kubernetes/salt-api-tls.yaml` (self-signed bootstrap Issuer →
10-year CA → 90-day leaf with SAN `salt-master-api` and the in-cluster
FQDNs; the leaf Secret `salt-master-api-cert` is mounted into the
master, the CA Secret `overstate-internal-ca` into the app / worker /
reconcile-cronjob / file-sync-cronjob).

## Verification behavior

`SaltClient` verifies TLS by default (`verify=True`, system CAs).
`SALT_API_VERIFY_CA` overrides it: a path uses that CA bundle, the literal
`false` disables verification (local dev only, against self-signed masters).

## Production (Kubernetes)

- Browser traffic terminates at Traefik; the UI Certificate
  (`deploy/kubernetes/ingress.yaml`) is cert-manager-issued.
- The salt-api hop is now pinned: every master pod serves a cert signed
  by the cert-manager-managed `overstate-internal-ca` (mounted into the
  app / worker / both cronjobs as `/etc/ssl/overstate/ca.crt` via
  `SALT_API_VERIFY_CA`). The cert-manager resources and the master
  cert-swap hook are the two pieces — both are required to land before
  the app env flips, which is why this release deploys in two steps
  (see `docs/guides/install-kubernetes.md` §2.1, the "Salt-api TLS pinning:
  two-step apply").
- Never set `SALT_API_VERIFY_CA=false` outside the cluster. The
  in-cluster default is the mounted CA path; `false` is dev-only.
