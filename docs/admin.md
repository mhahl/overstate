# Overstate administrator guide

You own users, roles, single sign-on, and server settings. You also own
the consequences: every admin click writes an audit row, and settings
changes apply immediately. No staging, no preview.

## Roles

Viewer, operator, admin. Higher implies lower. The server enforces the
floor on every request, so hiding a button never grants safety and
showing one never grants access.

Two guards protect you from yourself on the Users page. You cannot
demote your own account, and you cannot delete it. To leave the admin
role, promote someone else first and let them demote you.

## Users

The Users page lists every account, local and SSO, with its role. Only
admins see it.

**Change a role** with the role control on the row. The change applies
at once. For SSO accounts the next login recalculates the role from
group membership and overwrites your edit. Read the SSO section before
you hand-tune SSO roles; the provider wins.

**Delete** removes the account. You cannot delete yourself. Deleting a
local account does not touch anything on the Salt master; Overstate
accounts and Salt keys are separate systems. Deleting an SSO account
blocks that person until their next login recreates them as a viewer.

No password-change screen exists. Local users cannot rotate their own
passwords through the UI. If a local credential leaks, delete the
account and create a replacement, or rotate it directly in the
database. SSO users authenticate at the provider, so provider-side
rotation covers them.

## Single sign-on

SSO is optional. Unset everywhere means local login only. When the
issuer, client ID, and client secret are all set, from either source,
the login page gains the SSO button and the `/login/oidc` routes
activate. Missing any one of the three disables SSO silently and local
login keeps working.

### Where each value lives

Set non-secret values on the Settings page under Single sign-on. A DB
value overrides the environment variable of the same name. Clearing a
field deletes the override and the environment value applies again.
This lets you test a new issuer in the UI and roll back by clearing
the field.

The client secret is the one credential allowed in the database. Type
it into the masked field on the Settings page, or keep it in the
`OIDC_CLIENT_SECRET` environment variable as fallback. Clearing the DB
field falls back to the variable. Salt-api, Postgres, and Redis
credentials stay env-only with no exception.

### Role mapping

First login creates the account as a viewer. No local password is
stored. The account key is the issuer plus subject pair, so renaming a
user at the provider keeps the same Overstate account, and a local
account with the same username never merges into it. On collision the
SSO account takes a short-subject suffix.

Each login recomputes the role from group membership:

- Membership in an admin-listed group makes admin.
- Otherwise membership in an operator-listed group makes operator.
- Otherwise viewer.

Admin wins over operator. Configure the groups claim name (default
`groups`), the admin list, and the operator list on the Settings page
or via `OIDC_GROUPS_CLAIM`, `OIDC_ADMIN_GROUPS`, and
`OIDC_OPERATOR_GROUPS`. Values are comma-separated provider group
names. Because login overwrites the role, manage SSO authorization in
the provider groups, not on the Users page.

See `docs/sso.md` for the provider-side checklist: redirect URIs, the
discovery URL, and claim names.

## Settings reference

Four panels, ten values. Admins edit; everyone else reads.

**Server.**

- `master_host`. The Salt master hostname shown in the onboarding
  wizard. Empty resets to the detected default: the app host's FQDN,
  else the host part of the salt-api URL. Verify it. New minions point
  at whatever this says.

**Single sign-on (OIDC).**

- `oidc_issuer`. Provider base URL. Discovery appends
  `/.well-known/openid-configuration`. Empty plus no env disables SSO.
- `oidc_client_id`. The client ID you registered at the provider.
- `oidc_client_secret`. Masked input. Empty defers to the env var.
- `oidc_groups_claim`. Claim carrying group names. Empty uses
  `groups`.
- `oidc_admin_groups`. Comma-separated provider groups mapped to
  admin.
- `oidc_operator_groups`. Comma-separated provider groups mapped to
  operator.

**Run defaults.**

- `default_target`. Prefills the job form target. Default `*`. Set it
  to your safest broad matcher so an empty form aims somewhere boring.

**Display.**

- `page_size`. Minion list rows per page: 10, 25, or 50.
- `theme`. `light`, `dark`, or `wireframe` (the default). Applies server-side to every page, including the login screen.

Saving writes one row per value and flashes confirmation. Invalid
option values reset to the default instead of erroring.

## Audit

Every mutating action records who did it, what it was, and the Salt
job ID when one exists: key accept, highstate, schedule delete, role
change, batch waves, and the rest. The Audit page (Observe menu)
lists rows newest-first with user and action filters, and each JID
links to its job. For deeper queries go to Postgres directly:

```sql
SELECT created_at, "user", action, jid
FROM audit_events ORDER BY id DESC LIMIT 50;
```

Join `jid` against the `jobs` table to move from "who clicked" to
"what Salt did". Back up this table with the rest of the database;
it is your compliance story.

## Rotate the salt-api password

The Users page links to a rotation helper. It generates a fresh
password on every visit and shows the two-sided steps: set it for
the eauth system user on the master and restart salt-api, then set
`SALT_EAUTH_PASSWORD` to the same value and restart the app and
worker containers. The verify box tries a salt-api login with the
pasted password and audit-logs success. The password is never
stored anywhere; if verification fails, the two sides disagree.

## Login rate limit

Ten attempts per address per minute. The eleventh sees 429. The limit
is in-memory per worker process, so a multi-worker deployment allows
roughly ten times the worker count. Put the app behind a reverse proxy
that forwards the real client IP, or one attacker shares a budget with
everyone behind the same NAT address.

## Seed admin

First boot creates an `admin` account only when the users table is
empty. The generated password prints once in the container log. Copy
it into your vault, log in, and consider replacing the account: create
your named admin, verify it works, delete `admin`. If the users table
already holds anyone, boot never seeds, so a restart cannot lock you
out or reset credentials.
