"""DB-backed settings. Only the OIDC client secret may live here (declared exception); all other credentials stay env-only."""

import secrets
import socket
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from .audit import log_event
from .auth import roles_required
from .db import get_session
from .models import Setting

bp = Blueprint("settings", __name__, url_prefix="/settings")

OIDC_ENV_FALLBACK = {
    "oidc_issuer": "OIDC_ISSUER",
    "oidc_client_id": "OIDC_CLIENT_ID",
    "oidc_client_secret": "OIDC_CLIENT_SECRET",
    "oidc_groups_claim": "OIDC_GROUPS_CLAIM",
    "oidc_admin_groups": "OIDC_ADMIN_GROUPS",
    "oidc_operator_groups": "OIDC_OPERATOR_GROUPS",
}

DEFS = {
    "master_host": {
        "label": "Master hostname",
        "help": "Minion-facing Salt master hostname shown in the onboarding wizard. An empty value resets to the detected default.",
        "default": "",
    },
    "oidc_issuer": {
        "label": "OIDC issuer",
        "help": "Single sign-on provider URL. An empty value with no environment variable disables SSO.",
        "default": "",
    },
    "oidc_client_id": {
        "label": "OIDC client ID",
        "help": "Client ID registered at the provider.",
        "default": "",
    },
    "oidc_client_secret": {
        "label": "OIDC client secret",
        "help": "An empty value defers to OIDC_CLIENT_SECRET.",
        "default": "",
    },
    "oidc_groups_claim": {
        "label": "OIDC groups claim",
        "help": "Claim carrying group names for role mapping. An empty value uses the default.",
        "default": "",
    },
    "oidc_admin_groups": {
        "label": "OIDC admin groups",
        "help": "Enter comma-separated provider groups to map to the admin role.",
        "default": "",
    },
    "oidc_operator_groups": {
        "label": "OIDC operator groups",
        "help": "Enter comma-separated provider groups to map to the operator role.",
        "default": "",
    },
    "default_target": {
        "label": "Default target",
        "help": "The run form prefills this value when no preset or bulk selection sets one.",
        "default": "*",
    },
    "page_size": {
        "label": "Page size",
        "help": "Rows per page in the minion list.",
        "options": ["10", "25", "50"],
        "default": "25",
    },
    "theme": {
        "label": "Theme",
        "help": "Interface color scheme.",
        "options": ["light", "dark", "wireframe"],
        "default": "light",
    },
}


SECTIONS = [
    {
        "title": "Server",
        "desc": "How minions and browsers reach this installation.",
        "keys": ["master_host"],
    },
    {
        "title": "Single sign-on (OIDC)",
        "desc": "Provider connection and role mapping. Values here override the environment. Clearing a value defers to the environment. You can store the client secret here or in OIDC_CLIENT_SECRET.",
        "keys": [
            "oidc_issuer",
            "oidc_client_id",
            "oidc_client_secret",
            "oidc_groups_claim",
            "oidc_admin_groups",
            "oidc_operator_groups",
        ],
    },
    {
        "title": "Run defaults",
        "desc": "Values the command forms prefill.",
        "keys": ["default_target"],
    },
    {
        "title": "Display",
        "desc": "List density and color scheme.",
        "keys": ["page_size", "theme"],
    },
]


def default_master_host() -> str:
    """Detected default for the minion-facing master hostname: this
    host's FQDN, falling back to the salt-api URL host (this app often
    runs next to the master, but an admin should verify in Settings)."""
    try:
        fqdn = socket.getfqdn()
    except OSError:
        fqdn = ""
    if fqdn and fqdn not in ("localhost",):
        return fqdn
    try:
        return urlsplit(current_app.config["SALT_API_URL"]).hostname or ""
    except (ValueError, KeyError):
        return ""


def get_setting(key: str) -> str:
    row = get_session().get(Setting, key)
    if row is not None:
        return row.value
    if key in OIDC_ENV_FALLBACK:
        return current_app.config.get(OIDC_ENV_FALLBACK[key], "") or ""
    if key == "master_host":
        return default_master_host()
    return DEFS.get(key, {}).get("default", "")


#: Keys a scoped settings.read holder may see. OIDC issuer, client id,
#: group lists, and the client secret are grant.admin; the rotation card
#: is settings.rotate_eauth.
READ_DISPLAY_KEYS = ("page_size", "theme", "master_host")


def _settings_context():
    from .authz import authorize, rbac_mode

    scoped = rbac_mode() == "scoped"
    if scoped:
        # Server checks are authorize, not the role string: writers edit,
        # grant.admin sees OIDC fields, settings.read sees display keys.
        is_admin = authorize(current_user, "settings.write")
        show_oidc = authorize(current_user, "grant.admin")
    else:
        is_admin = show_oidc = current_user.role == "admin"
    values = {key: get_setting(key) for key in DEFS}
    # The stored secret is never echoed: the form posts it back only
    # when the admin types a new one, and viewers see no OIDC values.
    values["oidc_client_secret"] = ""
    if not show_oidc:
        for key in OIDC_ENV_FALLBACK:
            values[key] = ""
        sections = [
            {**s, "fields": [(k, DEFS[k]) for k in s["keys"]]}
            for s in SECTIONS
            if s["title"] != "Single sign-on (OIDC)"
        ]
    else:
        sections = [
            {**s, "fields": [(k, DEFS[k]) for k in s["keys"]]} for s in SECTIONS
        ]
    if scoped and not is_admin:
        values = {k: (v if k in READ_DISPLAY_KEYS else "") for k, v in values.items()}
        sections = [
            {**s, "fields": [(k, DEFS[k]) for k in s["keys"] if k in READ_DISPLAY_KEYS]}
            for s in sections
        ]
        sections = [s for s in sections if s["fields"]]
    return sections, values, is_admin, show_oidc


SECTION_PAGES = ("general", "sso", "access", "rotation")


def _render_section(section: str, rotation_password=None):
    """Render one settings subsection. Sections share one context; each
    form posts only its own keys and ``save`` touches nothing else, so
    sub-pages stay independent."""
    sections, values, is_admin, show_oidc = _settings_context()
    general = [s for s in sections if "oidc_issuer" not in s["keys"]]
    sso = [s for s in sections if "oidc_issuer" in s["keys"]]
    return render_template(
        "settings.html",
        section=section,
        general_sections=general,
        sso_sections=sso,
        values=values,
        is_admin=is_admin,
        show_oidc=show_oidc,
        idp_mappings=_idp_mapping_rows() if show_oidc else [],
        minion_groups=_minion_group_options() if show_oidc else [],
        role_groups=_role_group_options() if show_oidc else [],
        grain_keys=_grain_key_options(),
        rotation_user=current_app.config["SALT_EAUTH_USER"],
        rotation_password=rotation_password,
        can_rotate=_rotation_allowed(),
        **_rbac_card_values(),
    )


@bp.route("/")
@login_required
def index():
    from .authz import require

    require("settings.read")
    return _render_section("general")


@bp.route("/sso")
@login_required
def sso():
    from .authz import require

    require("settings.read")
    return _render_section("sso")


@bp.route("/access")
@login_required
def access():
    from .authz import require

    require("settings.read")
    return _render_section("access")


@bp.route("/rotation")
@login_required
def rotation():
    from .authz import require

    require("settings.read")
    return _render_section("rotation")


def _idp_mapping_rows():
    from .models import IdpRoleMapping

    return get_session().query(IdpRoleMapping).order_by(IdpRoleMapping.idp_group).all()


def _minion_group_options():
    from .models import MinionGroup

    return get_session().query(MinionGroup).order_by(MinionGroup.name).all()


def _role_group_options():
    from .users import ROLE_GROUPS

    return ROLE_GROUPS


def _grain_key_options():
    from .inventory import SNAPSHOT_GRAINS

    return list(SNAPSHOT_GRAINS)


def _rbac_card_values() -> dict:
    from .authz import rbac_flag

    return {
        "rbac_mode_value": rbac_flag("rbac_mode", "RBAC_MODE", "legacy"),
        "rbac_fallback_value": rbac_flag(
            "rbac_role_fallback", "RBAC_ROLE_FALLBACK", "on"
        ),
    }


def _rotation_allowed() -> bool:
    from .authz import authorize, rbac_mode

    if rbac_mode() == "scoped":
        return authorize(current_user, "settings.rotate_eauth")
    return current_user.role == "admin"


@bp.post("/idp-mappings")
@roles_required("admin")
def create_idp_mapping():
    """IdP mapping editor. Fleet grant.admin in scoped mode."""
    from .audit import log_event
    from .models import IdpRoleMapping

    _require_grant_admin()
    session = get_session()
    idp_group = request.form.get("idp_group", "").strip()
    role = request.form.get("role", "").strip()
    scope_kind = request.form.get("scope_kind", "").strip()
    if not idp_group:
        flash("Group name is required.", "error")
        return redirect(url_for("settings.access"))
    from .users import _validate_grant_scope

    error, scope_value = _validate_grant_scope(role, scope_kind, request.form)
    if error is not None:
        flash(error, "error")
        return redirect(url_for("settings.access"))
    existing = (
        session.query(IdpRoleMapping)
        .filter_by(
            idp_group=idp_group,
            role=role,
            scope_kind=scope_kind,
            scope_value=scope_value,
        )
        .first()
    )
    if existing is not None:
        flash("That mapping already exists.", "error")
        return redirect(url_for("settings.access"))
    session.add(
        IdpRoleMapping(
            idp_group=idp_group,
            role=role,
            scope_kind=scope_kind,
            scope_value=scope_value,
            origin="manual",
        )
    )
    session.commit()
    from .authz import refresh_role_cache

    for user in _users_in_idp_group(session, idp_group):
        refresh_role_cache(user)
    log_event(
        current_user.username,
        "mapping-create",
        permission="grant.admin",
        detail=f"{idp_group} {role} {scope_kind} {scope_value}",
    )
    flash(f"Mapped IdP group '{idp_group}'.", "success")
    return redirect(url_for("settings.access"))


def _require_grant_admin():
    from flask import abort

    from .authz import audit_deny, has_fleet, rbac_mode

    if rbac_mode() == "scoped" and not has_fleet(current_user, "grant.admin"):
        audit_deny("grant.admin")
        abort(403)


def _users_in_idp_group(session, idp_group):
    from .models import User, UserIdpGroup

    ids = [
        row.user_id
        for row in session.query(UserIdpGroup).filter_by(group_name=idp_group).all()
    ]
    return session.query(User).filter(User.id.in_(ids)).all() if ids else []


@bp.post("/idp-mappings/<int:mid>/delete")
@roles_required("admin")
def delete_idp_mapping(mid: int):
    from .audit import log_event
    from .models import IdpRoleMapping

    _require_grant_admin()
    session = get_session()
    mapping = session.get(IdpRoleMapping, mid)
    if mapping is None:
        flash("Unknown mapping.", "error")
        return redirect(url_for("settings.access"))
    detail = (
        f"{mapping.idp_group} {mapping.role} "
        f"{mapping.scope_kind} {mapping.scope_value}"
    )
    affected = _users_in_idp_group(session, mapping.idp_group)
    session.delete(mapping)
    session.commit()
    from .authz import refresh_role_cache

    for user in affected:
        refresh_role_cache(user)
    log_event(
        current_user.username, "mapping-delete", permission="grant.admin", detail=detail
    )
    flash("Mapping deleted.", "success")
    return redirect(url_for("settings.access"))


@bp.post("/rotation/generate")
@roles_required("admin")
def rotation_generate():
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("settings.rotate_eauth")
    """Mint a replacement salt-api password and show it exactly once.
    Nothing is stored anywhere — the audit row records the act, never
    the secret, and the admin pastes it into the Secret by hand."""
    password = secrets.token_urlsafe(24)
    log_event(current_user.username, "rotation-password-generated")
    return _render_section("rotation", rotation_password=password)


@bp.post("/rotation/verify")
@roles_required("admin")
def rotation_verify():
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("settings.rotate_eauth")
    """Try a candidate password against salt-api with an ephemeral
    client. Proves a hand-applied rotation took; stores nothing."""
    import httpx

    from .salt_client import SaltApiError, SaltClient

    candidate = request.form.get("password", "")
    user = current_app.config["SALT_EAUTH_USER"]
    if not candidate:
        flash("Paste the candidate password first.", "error")
        return redirect(url_for("settings.rotation"))
    client = SaltClient(
        current_app.config["SALT_API_URL"],
        user,
        candidate,
        current_app.config["SALT_EAUTH_TYPE"],
        verify=current_app.config["SALT_API_VERIFY"],
    )
    try:
        client.login(http_timeout=15.0)
    except (SaltApiError, httpx.HTTPError, KeyError) as exc:
        flash(f"Salt API refused the candidate ({exc}). Nothing changed.", "error")
        log_event(current_user.username, "rotation-verify:failed")
    else:
        flash(f"Login as {user} succeeded.", "success")
        log_event(current_user.username, "rotation-verify:ok")
    return redirect(url_for("settings.rotation"))


@bp.post("/")
@roles_required("admin")
def save():
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("settings.write")
    from .authz import rbac_mode as _rbac_mode

    _mirror = _rbac_mode() == "scoped"
    session = get_session()
    posted = set(request.form)
    for key, meta in DEFS.items():
        if key not in posted:
            # Section forms post only their own keys: anything absent
            # was not on the page, so leave the stored row alone.
            continue
        if _mirror and key in ("oidc_admin_groups", "oidc_operator_groups"):
            # Read-only mirror while scoped: the mapping editor owns
            # these. The disabled inputs do not post; do not treat
            # their absence as a clear.
            continue
        value = request.form.get(key, "").strip()
        if key == "oidc_client_secret":
            if request.form.get("clear_oidc_client_secret") == "on":
                # Explicit clear: drop the override so the env applies.
                row = session.get(Setting, key)
                if row is not None:
                    session.delete(row)
                continue
            if not value:
                # Empty means keep the stored secret, not wipe it.
                continue
        elif not value:
            if key in OIDC_ENV_FALLBACK:
                # Clearing defers to the environment: drop any override.
                row = session.get(Setting, key)
                if row is not None:
                    session.delete(row)
                continue
            value = default_master_host() if key == "master_host" else meta["default"]
        if "options" in meta and value not in meta["options"]:
            value = meta["default"]
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
    # One transaction for values, flags, and the mapping rebuild: a
    # failed rebuild must not leave rbac_mode=scoped behind on its own.
    _save_rbac_control(session)
    session.commit()
    flash("Settings saved.", "success")
    section = request.form.get("section", "")
    if section not in SECTION_PAGES or section == "general":
        return redirect(url_for("settings.index"))
    return redirect(url_for(f"settings.{section}"))


def _save_rbac_control(session) -> None:
    """RBAC mode and fallback control. Entering scoped mode rebuilds
    ``origin=backfill`` fleet ladder mappings from the current OIDC
    group settings; manual mappings stay. Every user's role cache is
    recomputed so the badge agrees with the grants."""
    import logging

    from .authz import rbac_mode, refresh_role_cache
    from .models import IdpRoleMapping, User

    mode = request.form.get("rbac_mode", "").strip()
    fallback = request.form.get("rbac_role_fallback", "").strip()
    entering = False
    if mode in ("legacy", "scoped"):
        before = rbac_mode()
        _store_flag(session, "rbac_mode", mode)
        entering = mode == "scoped" and before != "scoped"
    if fallback in ("on", "off"):
        _store_flag(session, "rbac_role_fallback", fallback)
    if entering:
        deleted = (
            session.query(IdpRoleMapping)
            .filter_by(origin="backfill")
            .delete(synchronize_session=False)
        )
        admin_groups = {
            g.strip()
            for g in (get_setting("oidc_admin_groups") or "").split(",")
            if g.strip()
        }
        operator_groups = {
            g.strip()
            for g in (get_setting("oidc_operator_groups") or "").split(",")
            if g.strip()
        }
        # The unique key does not include origin: a kept manual row for
        # the same tuple would collide, so only insert what is absent.
        # A manual row already grants it, which is the better outcome.
        present = {
            (row.idp_group, row.role, row.scope_kind, row.scope_value)
            for row in session.query(IdpRoleMapping).all()
        }
        for name in sorted(admin_groups):
            key = (name, "admin", "fleet", "*")
            if key not in present:
                session.add(
                    IdpRoleMapping(
                        idp_group=name,
                        role="admin",
                        scope_kind="fleet",
                        scope_value="*",
                        origin="backfill",
                    )
                )
                present.add(key)
        for name in sorted(operator_groups):
            key = (name, "operator", "fleet", "*")
            if key not in present:
                session.add(
                    IdpRoleMapping(
                        idp_group=name,
                        role="operator",
                        scope_kind="fleet",
                        scope_value="*",
                        origin="backfill",
                    )
                )
                present.add(key)
        for user in session.query(User).all():
            refresh_role_cache(user)
        logging.getLogger("overstate_ui.authz").info(
            "rbac_mode entering scoped: rebuilt %d backfill mappings", deleted
        )


def _store_flag(session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
