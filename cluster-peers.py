"""Report Salt master cluster members from the Kubernetes API.

Stdlib only. Two modes:

  cluster-peers.py           print one endpoint DNS name per line (sorted, unique)
  cluster-peers.py replicas  print the StatefulSet's spec.replicas

Names (``<pod-name>.<headless-service>``) are the cluster's stable
node identities: pod IPs change on every recreate, which orphans
Raft membership, while StatefulSet pod names survive. Names come
from each endpoint's targetRef, so an endpoint without one is
skipped; when none resolve the mode fails.

Reads the headless Service's EndpointSlices (publishNotReadyAddresses
keeps unready pods listed, so the set is the true member list, not a
DNS race). Any failure exits non-zero with stdout untouched: the
caller falls back to constructed names, never worse off than today.

Environment:
  MASTER_HEADLESS_SERVICE  Service name (default: salt-master)
  MASTER_STATEFULSET       StatefulSet name, replicas mode (default: salt-master)
  K8S_NAMESPACE            Namespace (default: in-cluster namespace file)
  K8S_API_HOST / K8S_API_PORT / K8S_API_SCHEME (defaults: in-cluster, https)
  SA_TOKEN_FILE (default: in-cluster token path)
  SA_CA_FILE (default: in-cluster CA path)
  SA_NAMESPACE_FILE (default: in-cluster namespace path)
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.parse
import urllib.request


class ApiError(Exception):
    """Anything that makes the API answer unusable."""


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError as exc:
        raise ApiError(f"cannot read {path}: {exc}") from exc


def client_config(env):
    """Return (base_url, token, ca_file_or_None); raise ApiError."""
    scheme = env.get("K8S_API_SCHEME", "https")
    host = env.get("K8S_API_HOST") or env.get("KUBERNETES_SERVICE_HOST")
    port = env.get("K8S_API_PORT") or env.get("KUBERNETES_SERVICE_PORT")
    if not host or not port:
        raise ApiError("no API server address")
    token_file = env.get(
        "SA_TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token"
    )
    token = _read_text(token_file)
    if not token:
        raise ApiError("empty API token")
    ca = None
    if scheme == "https":
        ca = env.get(
            "SA_CA_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        )
        if not os.path.isfile(ca):
            raise ApiError("no API CA file")
    return f"{scheme}://{host}:{port}", token, ca


def api_get_json(base_url, token, ca, path):
    """GET path, return the decoded JSON body; raise ApiError."""
    req = urllib.request.Request(
        base_url + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    context = ssl.create_default_context(cafile=ca) if ca else None
    try:
        with urllib.request.urlopen(req, timeout=10, context=context) as resp:
            if resp.status != 200:
                raise ApiError(f"API {path} -> HTTP {resp.status}")
            try:
                return json.load(resp)
            except ValueError as exc:
                raise ApiError(f"API {path} not JSON: {exc}") from exc
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(f"API {path} failed: {exc}") from exc


def namespace(env):
    """Namespace from env, else the in-cluster namespace file."""
    if env.get("K8S_NAMESPACE"):
        return env["K8S_NAMESPACE"]
    return _read_text(
        env.get(
            "SA_NAMESPACE_FILE",
            "/var/run/secrets/kubernetes.io/serviceaccount/namespace",
        )
    )


def slice_names(doc, service):
    """Stable DNS names for one EndpointSlice doc (may be empty).

    ``<pod-name>.<service>`` per endpoint targetRef; endpoints
    without a targetRef name carry no stable identity and are skipped.
    """
    names = []
    for endpoint in doc.get("endpoints") or []:
        target = endpoint.get("targetRef") or {}
        pod = target.get("name")
        if pod:
            names.append(f"{pod}.{service}")
    return names


def member_names(slices_doc, service):
    """Sorted unique DNS names across an EndpointSliceList doc; never empty."""
    names = set()
    for item in slices_doc.get("items") or []:
        names.update(slice_names(item, service))
    if not names:
        raise ApiError("no endpoint pod names")
    return sorted(names)


def peers_mode(env):
    """Live member DNS names for the headless Service."""
    base_url, token, ca = client_config(env)
    ns = namespace(env)
    service = env.get("MASTER_HEADLESS_SERVICE", "salt-master")
    selector = urllib.parse.quote(f"kubernetes.io/service-name={service}", safe="")
    doc = api_get_json(
        base_url,
        token,
        ca,
        f"/apis/discovery.k8s.io/v1/namespaces/{ns}/endpointslices"
        f"?labelSelector={selector}",
    )
    return member_names(doc, service)


def replicas_mode(env):
    """Desired replicas of the StatefulSet (the full-view guard)."""
    base_url, token, ca = client_config(env)
    ns = namespace(env)
    sts = env.get("MASTER_STATEFULSET", "salt-master")
    doc = api_get_json(
        base_url, token, ca, f"/apis/apps/v1/namespaces/{ns}/statefulsets/{sts}"
    )
    try:
        replicas = doc["spec"]["replicas"]
    except (KeyError, TypeError) as exc:
        raise ApiError("no spec.replicas") from exc
    if not isinstance(replicas, int):
        raise ApiError("spec.replicas not an integer")
    return replicas


def main(argv, env):
    mode = argv[1] if len(argv) > 1 else "peers"
    if mode == "peers":
        for name in peers_mode(env):
            print(name)
    elif mode == "replicas":
        print(replicas_mode(env))
    else:
        raise ApiError(f"unknown mode {mode!r}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv, os.environ))
    except ApiError as exc:
        print(f"cluster-peers: {exc}", file=sys.stderr)
        sys.exit(1)
