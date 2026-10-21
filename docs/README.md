# Overstate documentation index

Entry point for all documentation. Paths are repo-root-relative.

## Guides — how to operate and develop Overstate

| Document | Purpose |
|---|---|
| [`docs/guides/user.md`](guides/user.md) | Operator / end-user guide: every page, workflow, and role capability |
| [`docs/guides/admin.md`](guides/admin.md) | Administrator guide: settings, users, roles, operations |
| [`docs/guides/developer.md`](guides/developer.md) | Contributor guide: conventions, module layout, tests, lint, coverage, deps |
| [`docs/guides/install-kubernetes.md`](guides/install-kubernetes.md) | Production install runbook: secrets → apply → verify → restore |
| [`docs/guides/cloud-init-salt-minion.md`](guides/cloud-init-salt-minion.md) | cloud-init user-data for a Salt minion on Fedora 44 / openSUSE Tumbleweed |
| [`docs/guides/pillar-encryption.md`](guides/pillar-encryption.md) | NaCl-encrypted pillar values (NetBird, IPA): keypair, renderer, rotation |

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
| [`docs/reference/ui-style-guide.md`](reference/ui-style-guide.md) | Shared UI styling conventions for navigation, settings, onboarding, and console |

## Related top-level documents

- [`README.md`](../README.md) — project overview and quickstart
- [`PRODUCT.md`](../PRODUCT.md) — machine-readable product spec
- [`THIRD-PARTY-LICENSES.md`](../THIRD-PARTY-LICENSES.md) — dependency licences
