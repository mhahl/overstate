# Overstate administrator guide

You own users, roles, single sign-on, and server settings. You also own
the consequences: every admin click writes an audit row, and settings
changes apply immediately. No staging, no preview.

## Roles

Two models exist. Legacy is the ladder: viewer, operator, admin.
Higher implies lower. The server enforces the floor on every request,
so hiding a button never grants safety and showing one never grants
access. Scoped RBAC replaces the ladder with grants: a built-in role
on a scope (whole fleet, minion group, id list, id glob, or inventory
grain). Grants union; there is no deny row. A job target, a mine read,
and a kill are intersected with the caller's scope before anything
publishes, so a scoped `*` only ever reaches the minions in scope.
Which model applies is the `rbac_mode` setting below; flipping it back
to `legacy` restores the ladder on the next request. The full model,
recipes, and defaults live in `docs/rbac.md`.

Three guards protect you from yourself on the Users page. You cannot
demote your own account, and you cannot delete it. To leave the admin
role, promote someone else first and let them demote you. In scoped
mode you also cannot delete your own last fleet admin grant while no
other fleet admin (grant or IdP mapping) still applies to you.

## Users

The Users page lists every account, local and SSO, with its role. Only
admins see the full directory. A team lead holding `user.read` on a
minion scope sees a filtered page instead: users and local groups
with a grant inside that scope, and only those grant rows. No fleet
grants, no tokens, no rotation card.

**Change a role** with the role control on the row. The change applies
at once. For SSO accounts the next login recalculates the role from
group membership and overwrites your edit. Read the SSO section before
you hand-tune SSO roles; the provider wins.

**Delete** removes the account. You cannot delete yourself. Deleting a
local account does not touch anything on the Salt master; Overstate
accounts and Salt keys are separate systems. Deleting an SSO account
blocks that person until their next login recreates them (as a viewer
in legacy, with no access in scoped mode unless a mapping or a manual
grant applies).

### Scoped grants

Fourteen built-in roles, no custom roles. The ladder names (`viewer`,
`operator`, `admin`) plus `security-reader`, `auditor`, and
`key-custodian` grant only on the whole fleet. The team roles grant on
a minion scope or the fleet: `team-viewer` (reads, no secrets, no
execute), `team-operator` (runs reads and changes, no states, no
pillar), `team-state` (adds `state.apply` and its return bodies, still
no pillar browser), `secrets-reader` (pillar and mine, no execute),
`scheduler` (defines schedules, cannot run jobs), and `team-lead`
(team-state plus delegated grant-writing, filtered user and audit
views). `file-reader` and `file-editor` grant on a file-root prefix.

State results and pillar documents are different permissions. A
`team-state` user can fire `state.apply` and read its return, but gets
403 on the pillar browser, the mine, `state.show_highstate`, and the
confirm-time SLS render. Pillar, mine, live full grains, stored grain
and beacon job returns, and highstate bodies need `pillar.read` or
`mine.read`.

The **Grants** button on a row opens the grant editor: role, scope
kind, scope value. Group scopes store the minion group id, so renaming
the group keeps the grant working. A group named by any grant or IdP
mapping is frozen: only a fleet admin can change its members, rename,
or delete it, and a referenced group cannot be deleted at all.

**Local groups** are membership lists for local accounts, managed
on the Groups page next to Users. Grants on a group apply to every
member. The same page lists IdP groups read-only: provider
membership recorded automatically at each login, with whatever access
the Settings mappings confer.

### Delegated admin

`team-lead` carries `grant.delegate`: the lead may create and delete
grants whose minion set sits inside their own scope and whose
permissions they already hold there. No fleet scopes, no `grant.admin`,
no IdP mappings, no editing their own grants, no touching backfilled
fleet ladder rows. A lead cannot appoint a `secrets-reader` without
holding `pillar.read` and `mine.read` on that scope themselves. That
is the escalation brake; to lift it for one scope, grant the lead a
`secrets-reader` row there and the subset check passes on its own.

### Service accounts

CI identities live on the Users page under Service accounts
(`user.admin`). Name only: no password, no login, no ladder role. A
**token** (name, optional saved-job pin, optional expiry) shows its
Bearer [REDACTED] once at creation and is never stored or shown again;
only the hash and the prefix persist. Give the token the smallest role
that matches the job (`team-operator` or `team-state`) and pin it to
the saved job a human already confirmed. An unpinned token may only
run read-class functions (`test.ping`, `service.status`,
`schedule.list`, `beacons.list`, `sys.doc`, `sys.list_functions`).
Fires run as the account's grants with the target re-constrained, so a
saved `*` cannot escape the account's scope. Revoke ends future use;
fires already running are unaffected. Deleting a saved job pinned by a
token refuses with 409 until you unpin it. A lead cannot mint service
accounts.

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

First login creates the account. No local password is stored. The
account key is the issuer plus subject pair, so renaming a user at the
provider keeps the same Overstate account, and a local account with
the same username never merges into it. On collision the SSO account
takes a short-subject suffix.

In legacy mode each login recomputes the role from group membership:

- Membership in an admin-listed group makes admin.
- Otherwise membership in an operator-listed group makes operator.
- Otherwise viewer.

Admin wins over operator. Configure the groups claim name (default
`groups`), the admin list, and the operator list on the Settings page
or via `OIDC_GROUPS_CLAIM`, `OIDC_ADMIN_GROUPS`, and
`OIDC_OPERATOR_GROUPS`. Values are comma-separated provider group
names. Because login overwrites the role, manage SSO authorization in
the provider groups, not on the Users page.

In scoped mode the two lists become the initial IdP mapping rows, and
the editor on Settings replaces them: one row per provider group with
a role and a scope. Every matching row applies (union, not
first-match), so a user in two mapped groups gains both. Removing a
group from the claim drops that access at the next login; deleting a
mapping row drops it at once. A new SSO user with no matching mapping
and no manual grant gets nothing, not viewer. Manual grants on an SSO
user are additive and survive login; they cannot subtract a mapped
fleet role. To narrow mapped access, change the mapping, not the
user row.

See `docs/sso.md` for the provider-side checklist: redirect URIs, the
discovery URL, and claim names.

## Settings reference

Four sub-tabs: General, Single sign-on, Access control, and
Rotation. Each form saves only its own values. Admins edit; everyone
else reads. In scoped mode a holder of `settings.read` sees only
display values (`page_size`, `theme`, `master_host`); OIDC fields
and the rotation card need `grant.admin` and `settings.rotate_eauth`
respectively.

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
- `theme`. `light` (the default), `dark`, or `wireframe`. Applies server-side to every page, including the login screen.

Saving writes one row per value and flashes confirmation. Invalid
option values reset to the default instead of erroring.

**Single sign-on (IdP mappings).** In scoped mode the mapping editor
replaces the two group-list boxes above: group name, role, scope, one
row per provider group. The old boxes stay visible as a read-only
mirror while scoped. Only a fleet `grant.admin` may add or delete
rows.

**Scoped RBAC.** `rbac_mode` (`legacy` or `scoped`) selects the
authorization model; `rbac_role_fallback` (`on` or `off`) decides
whether a grant-less account keeps its role column as fleet access.
Keep fallback on until backfill is checked — it is what keeps the
seeded admin working — then turn it off; the page warns while it is
on. Entering scoped mode rebuilds the backfilled ladder mappings from
the current group lists and keeps manual rows. Rollback is setting
`rbac_mode` back to `legacy`: grants sit idle, the ladder returns on
the next request, and every account that can log in regains the
login-only reads (pillar, mine, files, events), including team-only
accounts. Team-only users lose operator mutations there, because
`scoped` and `none` are not ladder rungs. Do not downgrade the schema
in an incident; the extra tables are unused in legacy mode.

## Audit

Every mutating action records who did it, what it was, and the Salt
job ID when one exists: key accept, highstate, schedule delete, role
change, batch waves, and the rest. Refused attempts record a `deny`
row with the permission and the reason instead. The Audit page
(Observe menu) lists rows newest-first with user, action, outcome,
and permission filters, and each JID links to its job. For deeper
queries go to Postgres directly:

```sql
SELECT created_at, "user", action, jid
FROM audit_events ORDER BY id DESC LIMIT 50;
```

Join `jid` against the `jobs` table to move from "who clicked" to
"what Salt did". Back up this table with the rest of the database;
it is your compliance story.

In scoped mode a fleet `audit.read` holder (auditor, security-reader,
fleet viewer/operator/admin) sees every row. A team lead sees rows
naming an in-scope minion plus their own rows, with out-of-scope ids
in the detail redacted to a count — including on their own
`job-constrained` row, which names the minions a `*` did not touch.
Rows from before the scoped change carry no minion id and stay
fleet-only. Nobody without `audit.read` opens the page; their denial
is the 403 itself.

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
