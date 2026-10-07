# Scoped RBAC

Two authorization models exist. **Legacy** is the ladder: viewer,
operator, admin, higher implies lower. **Scoped** replaces the ladder
with grants: a built-in role on a scope. Fresh installs run legacy;
nothing about scoped mode activates until you select it on Settings.
The `docs/plans/scoped-rbac.md` file is the build plan, not the
manual; this page is the manual.

## How it works

A **grant** is one row: a subject (a user, or a local group whose
members all inherit it), a role, and a scope. An **IdP mapping** is
the same row keyed by provider group instead of a local subject: every
SSO user in that provider group gains it. Grants and mappings union.
There is no deny row; a second row can only add access, never remove
it. To narrow access, change or delete the row that grants it.

**Scopes** name a set of minions: the whole `fleet`, a minion `group`
(by id, so renames keep working), an id `list`, an id `glob`, or an
inventory `grain` match. File roles use a `prefix` scope over a
file-root path instead. A scope is evaluated against the inventory
snapshot at request time, never live against Salt on the auth path.

**Roles** are fourteen fixed names; custom roles do not exist. Each
role is a set of permissions:

| Role | Scopes | Permissions |
|---|---|---|
| `viewer` | fleet only | minion.read, minion.onboard, pillar.read, mine.read, job.read, state.read, schedule.read, beacon.read, group.read, file.read, reactor.read, event.read, audit.read, key.read, settings.read |
| `operator` | fleet only | viewer plus minion.refresh, minion.remove, pillar.capture, job.run.read, job.run.change, job.run.state, job.run.orchestrate, job.kill, job.save, job.batch, key.accept, key.delete, schedule.write, beacon.write, group.write, file.write, file.sync, reactor.write, state.watch, console.runner, dashboard.probe |
| `admin` | fleet only | operator plus file.git, reactor.persist, master.read, master.write, master.rollout, settings.write, settings.rotate_eauth, user.read, user.admin, grant.admin |
| `team-viewer` | minion scope or fleet | minion.read, job.read, state.read, schedule.read, beacon.read, group.read, key.read |
| `team-operator` | minion scope or fleet | team-viewer plus minion.refresh, job.run.read, job.run.change, job.kill, job.save, job.batch |
| `team-state` | minion scope or fleet | team-operator plus job.run.state |
| `secrets-reader` | minion scope or fleet | minion.read, pillar.read, mine.read |
| `scheduler` | minion scope or fleet | minion.read, schedule.read, schedule.write, schedule.allow.read, schedule.allow.change, schedule.allow.state, beacon.read, beacon.write |
| `team-lead` | minion scope or fleet | team-state plus grant.delegate, group.write, user.read, audit.read |
| `security-reader` | fleet only | minion.read, job.read, state.read, schedule.read, beacon.read, key.read, audit.read, group.read, file.read, reactor.read |
| `auditor` | fleet only | audit.read, minion.read, job.read, state.read, schedule.read, key.read, group.read, settings.read |
| `key-custodian` | fleet only | key.read, key.accept, key.delete, minion.read, minion.remove, minion.onboard |
| `file-reader` | prefix (or fleet) | file.read |
| `file-editor` | prefix (or fleet) | file.read, file.write |

Three scope rules keep the table honest. The ladder names plus
`security-reader`, `auditor`, and `key-custodian` grant **only on the
whole fleet**; the same row on a minion scope is ignored. Some
permissions are **fleet-domain** (`key.accept`, `job.run.orchestrate`,
reactor/master/settings/user/grant/console/dashboard and a few more):
a team role on the fleet does not confer them, and a minion scope
never does. `file.read`/`file.write` are **prefix-domain**: they need
a `prefix` scope (or fleet), and a minion scope does not carry them.
`user.read` and `audit.read` on a team-lead grant are the **delegate
exception**: they render the filtered Users and Audit views instead of
the full ones, and they are never dropped.

**Evaluation** is union over every applicable row: manual grants, group
grants, IdP mappings, and — only while the fallback flag is on — the
legacy role column as a fleet grant for grant-less ladder accounts.
One row covering the permission on the minion (or prefix) is enough.
Results memoize per request. The role badge on the Users page is a
cache recomputed on login, grant change, and mode switch; it is a
label, not the check — every request re-evaluates.

**Constrain before publish.** Every Salt publish intersects the
requested target with the caller's scope first, using the snapshot,
and publishes only the intersection. A scoped `*` reaches exactly the
minions in scope. An empty intersection refuses with 403 and an audit
`deny` row; a narrowed one publishes and records which ids were
dropped. This applies to job runs, mine reads, batch waves, saved-job
pins, and kills: a stored `*` job still needs fleet `job.kill` to
stop. Schedules and reactors check at define time (`schedule.write`
on the minion plus the function class allowed), because Salt fires
them later with no user on the path.

**Reads filter, then render.** Lists, job returns, raw minion tabs,
audit rows, and the Users page show only what the caller's grants
cover. State results and pillar documents are separate permissions: a
`team-state` user fires `state.apply` and reads its return but gets
403 on the pillar browser, the mine, and highstate bodies. Single-id
runs and console key/minion rows stamp the audit row with that id so
a lead with `audit.read` on the scope can see them.

**Delegation is a subset rule.** A `team-lead` may create and delete
grants whose minion set sits inside their own scope and whose
permissions they already hold there — no fleet scopes, no
`grant.admin`, no IdP mappings, no editing their own grants, no
touching backfilled ladder rows. The check compares permission sets,
not role names, so appointing a `secrets-reader` requires holding
`pillar.read` and `mine.read` on that scope first.

**Service accounts** are CI identities: name only, no password, no
login. A token shows its Bearer [REDACTED] once; only a hash and prefix
persist. A token pinned to a saved job runs exactly that job with the
target re-constrained to the account's scope; an unpinned token runs
only the read class (`test.ping`, `service.status`, `schedule.list`,
`beacons.list`, `sys.doc`, `sys.list_functions`). Revoke ends future
use; running fires are unaffected. Deleting a pinned saved job refuses
with 409 until unpinned.

## How to use it

### Turn scoped mode on

1. On Settings, fill `oidc_admin_groups` and `oidc_operator_groups`
   with your provider groups (comma-separated). These become the
   initial backfilled IdP mappings.
2. Set `rbac_mode` to `scoped`. Entering scoped rebuilds the
   backfilled mappings from those lists and keeps manual rows; every
   role cache recomputes.
3. Keep `rbac_role_fallback` **on** until you have checked the grants:
   grant-less ladder accounts keep their fleet role meanwhile. The
   page warns while it is on. Turn it **off** when the grants are
   right; that is the point where scoped mode is real.
4. Replace the backfilled fleet mappings with narrower rows over
   time; deleting a mapping row drops that access at once.

Rollback is the same control in reverse: set `rbac_mode` back to
`legacy`. Grants sit idle, the ladder returns on the next request,
and every account that can log in regains the login-only reads. Never
downgrade the schema in an incident; the grant tables are simply
unused in legacy mode.

### Recipes

**A team owns `web-*`.** Create a minion group for the web minions
(or use a glob scope `web-*` directly). Grant `team-operator` on that
scope to the team's local group, `team-state` if they apply states,
`secrets-reader` if they debug pillar. Their `*` targets run on team
minions only; the confirm page says so before anything publishes.

**A scheduler, not an executor.** Grant `scheduler` on the scope. The
account defines schedules whose functions pass the define-time check
but cannot run jobs: `scheduler` carries no `job.run.*`.

**A team lead.** Grant `team-lead` on the team's scope. The lead opens
the grant editor from the Users page and appoints readers and
operators inside that scope. To let the lead appoint a
`secrets-reader`, grant the lead `secrets-reader` on the same scope
first — the subset check then passes on its own. The lead sees the
filtered Users and Audit views: only subjects and rows inside their
scope.

**Files.** Grant `file-editor` with a `prefix` scope of the root the
team owns (for example `srv/salt/web`). Prefix scopes compare by path
segments, so `srv/salt/web` never covers `srv/salt/webshop`.

**CI.** Create a service account on the Users page, grant it the
smallest role that matches the job, save the job once as a human with
a confirmed target, and pin the token to that saved job. The token
then runs exactly that job, re-constrained to the account's scope.
Use unpinned tokens only for read-class probes.

**SSO.** One IdP mapping row per provider group: group name, role,
scope. Every matching row applies, so a user in two mapped groups
gains both. Removing a group from the claim drops that access at the
next login. A new SSO user with no matching mapping and no manual
grant gets nothing — not viewer. Manual grants on SSO users are
additive and survive login; they cannot subtract a mapped fleet role.

### When something 403s

The deny reason is in the audit row detail: `needs-fleet` (that
target type or function class needs a fleet grant),
`empty-intersection` (the scope holds none of the requested minions),
`out-of-scope` (a saved, pinned, or batch target escapes the
grants), `no-grant` (no row covers the permission at all). The
response and the flash never name minions the caller did not supply;
the full dropped-id list is audit-visible to fleet `audit.read` only.

## Defaults

Every default ships in code; no setup step invents one:

| Default | Value | Source |
|---|---|---|
| `rbac_mode` | `legacy` | flag default (DB row, else `RBAC_MODE`, else `legacy`) |
| `rbac_role_fallback` | `on` | flag default (DB row, else `RBAC_ROLE_FALLBACK`, else `on`) |
| `oidc_groups_claim` | `groups` | empty claim falls back to `groups` |
| `default_target` | `*` | Run defaults panel |
| `page_size` | `25` | Display panel |
| `theme` | `light` | Display panel |
| First account | local `admin` plus a fleet `admin` grant in the same transaction | `seed_admin` |
| Entering scoped | backfilled IdP mappings rebuilt from the group lists; manual rows and all `Grant` rows (including the seed grant) kept | mode control save |

No further defaults are required or created. Provider group names
cannot be defaulted — they name your IdP groups, so the backfilled
mappings start empty until you list them, and fallback-on plus the
seed grant keep the installer working meanwhile. New SSO users
default to nothing rather than viewer, deliberately: silent access is
worse than a loud empty page.
