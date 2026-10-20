"""GPG encrypt-only service for encrypted pillar values.

The app holds only the master's **public** key (a Setting row). Every
encrypt builds a scratch GNUPGHOME, imports that pub key, and produces
an armored block the master's ``gpg`` renderer decrypts at pillar render
time. The private key never enters this process.

Salt's gpg renderer unwraps any ``-----BEGIN PGP MESSAGE-----`` value it
finds in a pillar file rendered with the gpg renderer (``renderer:
jinja | yaml | gpg`` on the master, or a ``#!yaml|gpg`` shebang per
file). We only ever produce ciphertext; decryption and display of
plaintext stay on the master side, exactly as the pillar-encryption
guide documents.
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager

import gnupg

ARMOR_HEADER = "-----BEGIN PGP MESSAGE-----"


class GpgSetupError(RuntimeError):
    """The stored public key is not usable (empty, unparsable, or no
    encryption subkey). Carries a message safe to show in the UI."""


@contextmanager
def _gpg_home():
    """A scratch GNUPGHOME for one operation. Never persists keys."""
    with tempfile.TemporaryDirectory(prefix="overstate-gpg-") as home:
        gpg = gnupg.GPG(gnupghome=home)
        gpg.encoding = "utf-8"
        yield gpg


def key_id(pk_armored: str) -> str | None:
    """Fingerprint/keyid of the master public key, or None when it
    cannot be imported (the fingerprint is the import's canonical id).

    Runs in a scratch homedir: importing a public key is harmless, but
    nothing should linger between requests.
    """
    armored = (pk_armored or "").strip()
    if not armored:
        return None
    try:
        with _gpg_home() as gpg:
            imported = gpg.import_keys(armored)
            if not imported.fingerprints:
                return None
            fp = imported.fingerprints[0]
            # Short form: the last 16 hex chars, like gpg's output.
            return fp[-16:] if len(fp) >= 16 else fp
    except Exception:  # noqa: BLE001 — any gpg/import failure means unusable
        return None


def encrypt_value(plaintext: str, pk_armored: str) -> str:
    """Encrypt ``plaintext`` with the master public key (armored block).

    Raises :class:`GpgSetupError` when the key is missing or unusable —
    the caller turns that into a setup warning, never a crash.
    """
    armored = (pk_armored or "").strip()
    if not armored:
        raise GpgSetupError("no master public key stored yet")
    try:
        with _gpg_home() as gpg:
            imported = gpg.import_keys(armored)
            if not imported.fingerprints:
                raise GpgSetupError("the stored master public key is unusable")
            encrypted = gpg.encrypt(
                plaintext,
                recipients=imported.fingerprints,
                always_trust=True,
                armor=True,
            )
            if not encrypted.ok:
                raise GpgSetupError(
                    "encryption failed — check the stored master public key"
                )
            out = str(encrypted)
    except (OSError, ValueError) as exc:
        raise GpgSetupError("encryption failed (gpg unavailable)") from exc
    if not out.strip().startswith(ARMOR_HEADER):
        raise GpgSetupError("encryption produced no armored block")
    return out


def is_encrypted(value) -> bool:
    """True when ``value`` is an armored PGP block (the shape the gpg
    renderer decrypts). Anything else is plaintext data."""
    if not isinstance(value, str):
        return False
    return value.strip().startswith(ARMOR_HEADER)
