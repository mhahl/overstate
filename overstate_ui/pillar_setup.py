"""One-click NaCl setup for the master trio over the Kubernetes exec API.

Overstate owns the masters, so it can run the runbook itself: generate the
shared NaCl keypair on one master, read the public/secret key, install the
same secret key on the other masters, and store the public key for the
encryption editor. Every step is an ``exec`` call; failures are reported
per step and never raised to the page.

Safety properties:
- A key that already exists on the first master is **never regenerated**
  (silent rotation would orphan every existing ciphertext). The action
  becomes a repair: read the key and copy it to masters that lack it.
- Each target key is written 0600 salt:salt and its SHA-256 compared with
  the source before the step is reported as ok.
"""

from __future__ import annotations

import shlex

from .k8s import K8sClient, K8sError
from .pillar_nacl import key_id
from .settings import set_setting

NACL_DIR = "/home/salt/data/keys/nacl"
KEY_FILE = f"{NACL_DIR}/nacl"
PUB_FILE = f"{NACL_DIR}/nacl.pub"
PK_SETTING = "pillar_nacl_pk"
MASTER_LABEL = "app.kubernetes.io/name=salt-master"
KEYGEN_TIMEOUT = 120.0


def _shell(script: str) -> list[str]:
    return ["/bin/bash", "-c", script]


def _step(steps: list[dict], name: str, ok: bool, detail: str | None = None) -> None:
    steps.append({"step": name, "ok": ok, "detail": (detail or None)})


def _run(client: K8sClient, pod: str, command: list[str], timeout: float = 60.0) -> dict:
    return client.exec_command(pod, command, timeout=timeout)


def setup_nacl(user: str) -> dict:
    """Generate (or reuse) the master keypair and distribute it.

    Returns ``{"ok": bool, "steps": [{"step", "ok", "detail"}]}``.
    """
    steps: list[dict] = []
    try:
        client = K8sClient()
        pods = sorted(
            p["name"] for p in client.list_pods(MASTER_LABEL) if p.get("name")
        )
    except K8sError as exc:
        _step(steps, "discover", False, str(exc)[:200])
        return {"ok": False, "steps": steps}
    if not pods:
        _step(steps, "discover", False, "no salt-master pods found")
        return {"ok": False, "steps": steps}
    _step(steps, "discover", True, f"{len(pods)} master(s)")

    first = pods[0]
    # Reuse an existing key instead of silently rotating it.
    probe = _run(
        client,
        first,
        _shell(f"test -s {KEY_FILE} && echo present || echo absent"),
    )
    exists = probe["ok"] and probe["stdout"].strip() == "present"
    if exists:
        _step(steps, "keygen", True, "existing key reused (not regenerated)")
    else:
        keygen = (
            f"install -d -m 0700 -o salt -g salt {NACL_DIR} && "
            f"runuser -u salt -- salt-call --local nacl.keygen sk_file={KEY_FILE}"
        )
        result = _run(client, first, _shell(keygen), timeout=KEYGEN_TIMEOUT)
        if not result["ok"]:
            _step(steps, "keygen", False, (result["stderr"] or result["stdout"])[:300])
            return {"ok": False, "steps": steps}
        _step(steps, "keygen", True, f"generated on {first}")

    pub = _run(client, first, ["/bin/cat", PUB_FILE])
    secret = _run(client, first, ["/bin/cat", KEY_FILE])
    public_key = pub["stdout"].strip()
    secret_key = secret["stdout"].strip()
    if not pub["ok"] or not secret["ok"] or not public_key or not secret_key:
        _step(steps, "read-keys", False, "could not read the generated key files")
        return {"ok": False, "steps": steps}
    if key_id(public_key) is None:
        _step(steps, "read-keys", False, "the generated public key is not a NaCl key")
        return {"ok": False, "steps": steps}
    _step(steps, "read-keys", True, f"key id {key_id(public_key)}")

    source_hash = _run(client, first, _shell(f"sha256sum {KEY_FILE} | awk '{{print $1}}'"))
    digest = source_hash["stdout"].strip()

    for pod in pods[1:]:
        script = (
            f"install -d -m 0700 -o salt -g salt {NACL_DIR} && "
            f"printf '%s' {shlex.quote(secret_key)} > {KEY_FILE} && "
            f"chown salt:salt {KEY_FILE} && chmod 0600 {KEY_FILE} && "
            f"sha256sum {KEY_FILE} | awk '{{print $1}}'"
        )
        copied = _run(client, pod, _shell(script))
        ok = copied["ok"] and copied["stdout"].strip() == digest and bool(digest)
        _step(
            steps,
            f"install:{pod}",
            ok,
            None if ok else (copied["stderr"] or "key digest mismatch")[:300],
        )
        if not ok:
            return {"ok": False, "steps": steps}

    set_setting(PK_SETTING, public_key)
    _step(steps, "store-public-key", True, "stored in Encrypted pillar key settings")
    return {"ok": True, "steps": steps}
