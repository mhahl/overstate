"""Overstate configuration — env only, per PLAN.md. Never store secrets in the DB."""

import os


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required env var: {name}")
    return value


def _verify_setting(value: str) -> bool | str:
    """Map SALT_API_VERIFY_CA to an httpx verify value. Default True
    (system CAs); the literal "false" disables verification and is only
    acceptable in local dev against self-signed masters."""
    if not value:
        return True
    if value.strip().lower() == "false":
        return False
    return value


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://overstate:overstate@localhost:5432/overstate",
    )
    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    SALT_API_URL = os.environ.get("SALT_API_URL", "https://salt-master:8000")
    SALT_API_VERIFY = _verify_setting(os.environ.get("SALT_API_VERIFY_CA", ""))
    SALT_EAUTH_USER = os.environ.get("SALT_EAUTH_USER", "overstate")
    SALT_EAUTH_PASSWORD = os.environ.get("SALT_EAUTH_PASSWORD", "")
    SALT_EAUTH_TYPE = os.environ.get("SALT_EAUTH_TYPE", "pam")
    OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "")
    OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
    OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
    OIDC_GROUPS_CLAIM = os.environ.get("OIDC_GROUPS_CLAIM", "groups")
    OIDC_ADMIN_GROUPS = os.environ.get("OIDC_ADMIN_GROUPS", "")
    OIDC_OPERATOR_GROUPS = os.environ.get("OIDC_OPERATOR_GROUPS", "")
    FILE_ROOTS = os.environ.get("FILE_ROOTS", "salt-srv/salt")
    SYNDIC_MASTERS = os.environ.get("SYNDIC_MASTERS", "")


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    WTF_CSRF_ENABLED = False
