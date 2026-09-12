# Overstate SSO (OIDC)

Status: per `docs/v3.9.md` (Final).

Local password login always works. Setting the three OIDC variables below
adds a "Log in with SSO" button on the login page.

## Provider setup

Register Overstate as an OIDC client (any conforming provider works) with:

- Redirect URI: `https://<app-host>/login/oidc/callback`
- Scopes: `openid email profile` (plus groups, see below)

Configure it on the server Settings page (admin only): issuer, client
ID, groups claim, and the admin/operator group lists. Values set there
override the environment; clearing a field defers back to it:

```sh
OIDC_ISSUER=https://login.example.com/realms/ops
OIDC_CLIENT_ID=overstate
OIDC_CLIENT_SECRET=change-me-in-production
```

The client secret is the one credential allowed in the database (a
declared exception to the env-only rule): set it on the Settings page
— shown as a masked password field — or via `OIDC_CLIENT_SECRET`, which
remains as fallback. The SSO button appears only when issuer, client
ID, and secret are all set from either source; otherwise the
`/login/oidc*` routes return 404 and local login is unaffected.

## Identity and roles

- Accounts are keyed on the issuer + subject pair. An SSO login never
  merges into a same-named local account: on a username collision the
  SSO account gets a short-subject suffix instead.
- Everyone arrives as viewer. Group membership promotes at each login:

```sh
OIDC_GROUPS_CLAIM=groups            # claim carrying group names
OIDC_ADMIN_GROUPS=overstate-admins
OIDC_OPERATOR_GROUPS=overstate-operators
```

No group match means viewer. The mapping is re-evaluated at every login
and wins over manual edits on the Users page — remove someone from the
group to demote them. Changing `OIDC_ISSUER` orphans existing SSO rows
(they keep their old issuer key); delete the stale rows on Users.

## Notes

- SSO accounts have no local password and cannot use password login.
- Logout ends the Overstate session only; the provider session stays.
- Pre-change SSO rows (matched by bare username) are orphaned by the
  identity key: users log in fresh and an admin deletes the stale rows.
