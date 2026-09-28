# Upstream Salt patches

Overstate runs stock Salt on the masters plus a small set of surgical
changes to Salt's experimental cluster code. This file lists every
change made to upstream Salt sources, where it lives, and why.

## Vehicle

All changes ship through `salt-cluster-identity-patch.py` (repo root),
applied once at image build time by `Containerfile.salt-master` against
the pinned base image
(`ghcr.io/cdalvaro/docker-salt-master:lts`, pinned by digest).
The patcher uses exact-string anchors that must each occur exactly
once: if upstream drifts, the build fails loudly (`PatchError`)
instead of shipping a half-patched tree. That anchor check is the
safety net — there is no runtime patching.

Tests: `tests/test_salt_cluster_patch.py` exercises the patcher
against synthetic trees (exact-once application, hard errors on
missing/duplicate anchors, table well-formedness).

## Changes

### 1. Stable cluster identity (`cluster_node_id`)

Upstream keys Raft membership, join sentinels, founder election, and
ring ownership on `opts["interface"]` — the pod IP, which Kubernetes
changes on every recreate. Dead IPs accumulate in the voter set and
quorum is eventually lost for good.

- `salt/cluster/consensus/service.py`: Raft `node_id` and the
  `SaltPeer` local id prefer the new free-form `cluster_node_id` opt,
  falling back to `interface` when unset (unpatched behavior
  byte-for-byte identical without the opt).
- `salt/cluster/ring_membership.py` (2 sites): same preference for
  ring ownership checks.
- `salt/master.py`, `salt/channel/server.py`: founder election,
  bootstrap pool, and join-sentinel paths compare against
  `cluster_node_id` instead of `interface`.

`cluster-entrypoint.sh` stamps `cluster_node_id` as
`<pod-name>.<headless-service>` (a DNS name that survives recreates)
into a drop-in on the keys PVC, included from the shared ConfigMap.

### 2. Founder joins instead of forking

When the designated founder boots while peers are already live (the
normal rolling-restart case), it joins the existing cluster instead of
founding a second one. Probes each peer's cluster port with a 2s TCP
connect before deciding.

- `salt/master.py`, `salt/channel/server.py`: founder path checks
  peer liveness first (`_join_existing`).

### 3. Forkserver-safe cluster readiness

Python 3.14 defaults to forkserver, so forked workers don't inherit
`SMaster.secrets` and the in-memory `cluster_ready` event is invisible
to them. Request handling would defer forever (`cluster_retry` on
every call, minions logging "Master did not return a session key").

- `salt/channel/server.py` (`_cluster_is_ready`): after checking the
  in-memory event, also honors the `<cachedir>/health/ready` sentinel
  file the publisher writes once this node is a committed voter.

### 4. Transport recovery: drop dead cached raft clients (fix 1)

`salt/cluster/consensus/peer.py::_publish` caches one
`_TCPPubServerPublisher` per peer pusher for the life of the process.
When the cached stream dies (peer pod restarted, RST mid-handshake),
every subsequent send through that pusher fails the same way — observed
live as 124 consecutive `StreamClosedError`s blacking out one peer
direction, which starved elections of that peer's replies and helped
wedge the cluster (no committed voters → `cluster_retry` → salt-api
401s on every node).

- `salt/cluster/consensus/peer.py`: a failed `send` now closes and
  drops the cached client so the next send reconnects fresh
  (re-resolving the peer), then re-raises so the caller still logs
  the dropped RPC. Deliberately **no retry**: retrying a
  possibly-delivered fire-and-forget publish could duplicate Raft
  RPCs, and duplicate pre-vote replies poison candidacies
  (`CandidacyError`) and stall elections.

## Deliberately not patched

- `salt/cluster/consensus/raft/node.py` (`Candidacy.handle_reply`
  raising on same-term duplicates) and the pre-vote overlap behavior
  were investigated as the "double delivery" suspect and left alone:
  duplicates observed live are consistent with overlapping pre-vote
  rounds in an all-deny deadlock, not systematic double-sends, and a
  single granted reply elects regardless — so tolerating duplicates
  would silence logs without unblocking elections. Revisit if a
  systematic double-send is ever demonstrated.
- Salt's `pam` auth, `rest_cherrypy` login, and token paths were
  investigated during the same incident and exonerated (credentials,
  PAM, and `external_auth` all verified working; the 401s were the
  `cluster_retry` gate above).
