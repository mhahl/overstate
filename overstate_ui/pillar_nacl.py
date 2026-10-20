"""NaCl (sealedbox) encrypt-only service for encrypted pillar values.

The app holds only the master's **public** key (a Setting row). Values
are encrypted with ``nacl.public.SealedBox`` and stored as
``NACL[base64]`` strings; Salt's ``nacl`` renderer decrypts them
master-side at pillar-render time using the private key on the master.
The private key never enters this process, and sealedbox is one-way, so
the app can add secrets but can never read them back.

Keys are 32 raw bytes, base64-encoded (Salt's ``nacl.keygen`` writes a
``.pub`` file with exactly this value). No trust model, subkeys, or
expiry — rotation is generating a new pair and re-entering values.
"""

from __future__ import annotations

import base64
import binascii
import hashlib

from nacl.public import PublicKey, SealedBox

ENVELOPE_PREFIX = "NACL["
ENVELOPE_SUFFIX = "]"
KEY_BYTES = 32


class NaclSetupError(RuntimeError):
    """The stored public key is missing or unusable. Its message is safe
    to show in the UI."""


def _decode_key(pk_b64: str) -> bytes:
    raw = (pk_b64 or "").strip()
    if not raw:
        raise NaclSetupError("no master public key stored yet")
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise NaclSetupError("the stored public key is not valid base64") from exc
    if len(key) != KEY_BYTES:
        raise NaclSetupError(
            f"the stored public key is not a {KEY_BYTES}-byte NaCl key"
        )
    return key


def key_id(pk_b64: str) -> str | None:
    """Short, stable display id for a public key (SHA-256 prefix).

    Display only — it is not part of the key or the envelope, so nothing
    depends on its exact format.
    """
    try:
        key = _decode_key(pk_b64)
    except NaclSetupError:
        return None
    return hashlib.sha256(key).hexdigest()[:16]


def encrypt_value(plaintext: str, pk_b64: str) -> str:
    """Encrypt ``plaintext`` with the master public key.

    Returns the ``NACL[...]`` envelope Salt's renderer decrypts.
    Raises :class:`NaclSetupError` when the key is missing or unusable.
    """
    key = _decode_key(pk_b64)
    try:
        sealed = SealedBox(PublicKey(key)).encrypt(plaintext.encode("utf-8"))
    except (ValueError, TypeError) as exc:  # pragma: no cover — libsodium
        raise NaclSetupError("encryption failed") from exc
    return f"{ENVELOPE_PREFIX}{base64.b64encode(sealed).decode('ascii')}{ENVELOPE_SUFFIX}"


def is_encrypted(value) -> bool:
    """True when ``value`` is a NACL[...] envelope (the shape the nacl
    renderer decrypts). Anything else is plaintext data."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    return text.startswith(ENVELOPE_PREFIX) and text.endswith(ENVELOPE_SUFFIX)
