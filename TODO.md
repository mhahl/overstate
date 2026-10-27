# TODO

## Standing notes

- Local machine: FreeBSD podman (ocijail) — no local Linux builds, and the
  compose dev stack's Linux images cannot run either. Use the kaniko flow
  for anything image-related.
- No python3 on PATH; the test venv lives at `/tmp/opencode/ovst-venv`
  (FreeBSD `pkg` py312-* packages + `--system-site-packages` venv +
  `pip install flask-login pynacl`; run `python -m pytest` from the repo
  root). Otherwise use the dev container if one is set up.
- GitHub pushes go through `gh` (https remote + `gh auth setup-git`);
  SSH needs the FIDO key attached (`id_ed25519_sk`).
- Release flow: image tag = the CODE commit sha (capture `TAG` before
  the pin commit); the kaniko flow in `kaniko-build.yaml` header /
  `docs/guides/install-kubernetes.md` §2 is a SINGLE sed'd apply —
  double-applying it fails on the immutable Job spec and re-creates the
  RWO context-loader pod, which blocks the kaniko pod's attach.
- `ruff` from pkg (0.17.x) flags many pre-existing findings (new default
  rules) and wants to reformat old files: compare against baseline rather
  than chasing a zero-diff on the whole tree.
- pytest env note: 12 pre-existing failures on this box
  (test_cluster_entrypoint, test_rbac_schema, test_v3, one
  test_minions_helpers) caused by pkg alembic 1.18.2 vs locked 1.20.0 —
  same set fails on pristine HEAD.