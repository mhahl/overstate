# Overstate documentation index

Entry point for all documentation. Paths are repo-root-relative.

## Guides — how to operate and develop Overstate

| Document | Purpose |
|---|---|
| [`docs/guides/user.md`](guides/user.md) | Operator / end-user guide: every page, workflow, and role capability |
| [`docs/guides/admin.md`](guides/admin.md) | Administrator guide: settings, users, roles, operations |
| [`docs/guides/developer.md`](guides/developer.md) | Contributor guide: conventions, module layout, tests, lint, coverage, deps |
| [`docs/guides/install-kubernetes.md`](guides/install-kubernetes.md) | Production install runbook: secrets → apply → verify → restore |

## Reference — design and feature specifics

| Document | Purpose |
|---|---|
| [`docs/reference/architecture-kubernetes.md`](reference/architecture-kubernetes.md) | Kubernetes deployment design, topology, data flows, assumptions, risks |
| [`docs/reference/rbac.md`](reference/rbac.md) | Scoped-RBAC manual: roles, permissions, grants, delegation |
| [`docs/reference/sso.md`](reference/sso.md) | OIDC provider setup (Authlib, group-to-role mapping) |
| [`docs/reference/tls.md`](reference/tls.md) | Browser ↔ app ↔ salt-api TLS chain |
| [`docs/reference/returner.md`](reference/returner.md) | Job-return path (master-side returner config) |
| [`docs/reference/topology.md`](reference/topology.md) | Proxy minions, syndic, salt-ssh coverage |
| [`docs/reference/upstream-salt-patches.md`](reference/upstream-salt-patches.md) | Record of patches carried against upstream Salt |

## Decision records and history

| Location | Contents |
|---|---|
| [`docs/plans/`](plans/) | Feature design / decision records for current and shipped work. `docs/plans/scoped-rbac.md` is the build plan behind [`docs/reference/rbac.md`](reference/rbac.md). |
| [`docs/history/`](history/) | Historical documents kept for context only: the original v1 product plan (`docs/history/PLAN.md`) and version decision records `docs/history/v3.md`–`docs/history/v3.9.md`. Not current scope. |
| [`docs/reviews/`](reviews/) | Point-in-time codebase review snapshots. |

## Related top-level documents

- [`README.md`](../README.md) — project overview and quickstart
- [`PRODUCT.md`](../PRODUCT.md) — machine-readable product spec
- [`THIRD-PARTY-LICENSES.md`](../THIRD-PARTY-LICENSES.md) — dependency licences
