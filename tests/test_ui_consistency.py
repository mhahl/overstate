"""UI consistency (group 1): shared header, button, pagination, table, and
back-link conventions.

Rendered assertions cover every page that renders without live Salt data.
The Keys table only renders with live roster data, so its classes are
pinned by a static template assertion instead.
"""

import pathlib
import re

from overstate_ui import create_app
from overstate_ui.audit import log_event
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Job, Minion, MinionGroup, User

TEMPLATES = (
    pathlib.Path(__file__).resolve().parent.parent / "overstate_ui" / "templates"
)

FILTER_PAGES = ["/audit/", "/groups/", "/jobs/", "/keys/", "/minions/", "/pillar/"]
TABLE_SEARCH_TEMPLATES = (
    "audit.html",
    "files.html",
    "groups.html",
    "jobs.html",
    "keys.html",
    "minions.html",
    "pillar.html",
    "reactor.html",
    "users.html",
)


def make_client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        session = get_session()
        for i in range(30):
            log_event("admin", f"action-{i:02d}")
            session.add(MinionGroup(name=f"g-{i:02d}", members=["web-01"]))
            session.add(Minion(id=f"web-{i:02d}", key_status="accepted", grains={}))
        for i in range(51):
            session.add(
                Job(
                    jid=f"hist-{i:03d}",
                    fun="test.ping",
                    tgt="*",
                    tgt_type="glob",
                    user="admin",
                    complete=True,
                )
            )
        session.commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    return c


def test_filter_submit_is_primary_on_every_filter_page():
    c = make_client()
    for path in FILTER_PAGES:
        html = c.get(path).data.decode()
        if path == "/groups/":
            assert re.search(r'class="[^"]*btn-outline[^"]*">Filter groups', html), path
            continue
        assert re.search(r'class="[^"]*btn-primary[^"]*">Filter', html), path
        assert 'class="btn btn-sm mb-1">Filter' not in html, path


def test_table_searches_use_compact_shared_component():
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    rendered = env.from_string(
        '{% import "_macros.html" as m %}'
        '{{ m.table_search_field("table-q", "q", "term", "Search", "Search table…", "Search table") }}'
    ).render()
    assert 'class="fieldset w-full max-w-xs sm:max-w-64 min-w-0"' in rendered
    assert 'class="input input-sm w-full min-w-0 gap-2"' in rendered
    assert 'icon-[lucide--search]' in rendered
    assert 'id="table-q" type="search" name="q"' in rendered
    assert 'aria-label="Search table"' in rendered

    for template_name in TABLE_SEARCH_TEMPLATES:
        source = (TEMPLATES / template_name).read_text()
        search_form = re.search(
            r'<form\b[^>]*role="search"[^>]*>.*?</form>', source, re.DOTALL
        )
        assert search_form, template_name
        assert "m.table_search_field(" in search_form.group(0), template_name
        assert "justify-end" in search_form.group(0), template_name


def test_pagination_uses_join_component():
    c = make_client()
    for path in (
        "/audit/?per_page=10",
        "/groups/?per_page=10",
        "/jobs/?tab=history&per_page=10",
        "/minions/?per_page=10",
    ):
        html = c.get(path).data.decode()
        assert 'class="join' in html, path
        assert "join-item" in html, path


def test_list_tables_pin_headers():
    c = make_client()
    for path in ("/audit/", "/groups/", "/users/", "/pillar/"):
        html = c.get(path).data.decode()
        assert "table-pin-rows" in html, path
    # The Keys table only renders with live roster data.
    keys_src = (TEMPLATES / "keys.html").read_text()
    assert "table-pin-rows" in keys_src


def test_list_tables_have_no_zebra_striping():
    # Zebra striping bleeds into the page background on wide tables
    # (Jobs history). Tables use plain rows with hover states instead.
    offenders = [
        path.name
        for path in sorted(TEMPLATES.glob("*.html"))
        if "table-zebra" in path.read_text()
    ]
    assert not offenders, "zebra striping still used: " + ", ".join(offenders)


def test_form_controls_have_accessible_names():
    # Every select/input/textarea needs its own accessible name: a
    # fieldset legend names the group, not the control. Placeholders
    # do not count. Hidden and button-type inputs are exempt, as are
    # controls wrapped in a <label> or matched by a <label for>.
    offenders = []
    for path in sorted(TEMPLATES.glob("*.html")):
        src = path.read_text()
        src = re.sub(r"{#.*?#}", "", src, flags=re.DOTALL)
        src = re.sub(r"{%.*?%}", "", src, flags=re.DOTALL)
        for_ids = set(re.findall(r'<label\b[^>]*\bfor="([^"]+)"', src))
        src = re.sub(r"<label\b[^>]*>.*?</label>", "", src, flags=re.DOTALL)
        for match in re.finditer(r"<(select|textarea|input)\b(.*?)>", src, re.DOTALL):
            tag, attrs = match.group(1), match.group(2)
            if tag == "input":
                kind = re.search(r'type="([^"]*)"', attrs)
                if kind is not None and kind.group(1) in (
                    "hidden",
                    "submit",
                    "button",
                    "reset",
                    "image",
                ):
                    continue
            if "aria-label" in attrs or "aria-labelledby" in attrs:
                continue
            ident = re.search(r'id="([^"]+)"', attrs)
            if ident and ident.group(1) in for_ids:
                continue
            offenders.append(f"{path.name}:{match.group(0)[:90]}")
    assert not offenders, "unlabeled controls:\n" + "\n".join(offenders)


def test_list_pages_offer_page_size_dropdown():
    # Mine/pillar/keys/files/vulns dropdowns are covered in their own
    # suites with salt fixtures; these routes render on DB data alone.
    c = make_client()
    for path in (
        "/audit/?per_page=10",
        "/groups/?per_page=10",
        "/jobs/?tab=history&per_page=10",
        "/minions/?per_page=10",
        "/users/?per_page=10",
    ):
        html = c.get(path).data.decode()
        assert 'name="per_page"' in html, path
        assert 'value="10" selected' in html, path


def test_users_list_searches_and_pages_without_rendering_every_account():
    from html.parser import HTMLParser
    from urllib.parse import parse_qs, urlsplit

    c = make_client()
    with c.application.app_context():
        session = get_session()
        session.add_all(User(username=f"person-{i:03d}", role="viewer") for i in range(121))
        session.commit()

    class NextLink(HTMLParser):
        href = None
        current = None

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.current = dict(attrs).get("href")

        def handle_data(self, data):
            if data.strip() == "Next":
                self.href = self.current

        def handle_endtag(self, tag):
            if tag == "a":
                self.current = None

    page = c.get("/users/?role=viewer&sort=username&dir=asc&page=2").data.decode()
    assert page.count('<td class="font-medium">person-') == 50
    assert '<td class="font-medium">person-050</td>' in page
    assert '<td class="font-medium">person-049</td>' not in page
    parser = NextLink()
    parser.feed(page)
    assert parse_qs(urlsplit(parser.href).query) == {
        "role": ["viewer"],
        "sort": ["username"],
        "dir": ["asc"],
        "page": ["3"],
        "per_page": ["50"],
    }

    filtered = c.get("/users/?role=viewer&q=person-10&page=99").data.decode()
    assert filtered.count('<td class="font-medium">person-') == 10
    assert "Page 1 of 1" in filtered
    assert re.search(r'href="/users/\?sort=role[^\"]*q=person-10', filtered)


def test_dashboard_heading_matches_page_headings():
    html = make_client().get("/").data.decode()
    assert 'font-bold tracking-tight">Fleet overview' in html


def test_destructive_actions_sit_outside_primary_headers():
    c = make_client()
    users = c.get("/users/").data.decode()
    assert "Danger zone" not in users
    assert "Rotate salt-api password" not in users
    rotation = c.get("/settings/rotation").data.decode()
    assert "salt-api password rotation" in rotation
    detail = c.get("/jobs/hist-000").data.decode()
    assert 'btn-primary">Sync results' in detail


def test_primary_actions_live_in_page_headers():
    html = make_client().get("/reactor/").data.decode()
    assert ">Add reactor</a>" in html


def test_onboard_uses_steps_progress():
    html = make_client().get("/minions/onboard").data.decode()
    assert 'class="steps' in html


def test_events_stream_has_status_badge():
    html = make_client().get("/events/").data.decode()
    assert 'id="event-status"' in html


def test_minions_search_filters_live():
    html = make_client().get("/minions/").data.decode()
    assert 'hx-trigger="input changed delay:400ms, submit"' in html


def test_jobs_poll_shows_refreshing_indicator():
    html = make_client().get("/jobs/?tab=history").data.decode()
    assert 'id="jobs-refreshing"' in html


def test_sort_indicators_use_icons():
    html = make_client().get("/audit/?sort=action&dir=asc").data.decode()
    assert "icon-[lucide--arrow-up]" in html
    assert "↑" not in html


def test_confirm_fire_button_states_blast_radius():
    c = make_client()
    rv = c.post(
        "/jobs/run",
        data={
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "state.apply",
            "args": "web",
            "mode": "async",
        },
    )
    html = rv.data.decode()
    assert "Review before firing" in html
    assert "Fire on 30 minions" in html


def test_back_links_share_glyph_and_size():
    for name in TEMPLATES.glob("*.html"):
        src = name.read_text()
        assert "←" not in src, name.name
        for line in src.splitlines():
            # Every "‹ ..." control is a back/prev link: small ghost style.
            if "‹ " in line:
                assert "btn-sm" in line, f"{name.name}: {line.strip()}"


def test_templates_use_canonical_daisyui_field_classes():
    # daisyUI v5 dropped the `-bordered` field variants (bordered is the
    # default) and the `label-text` helper: unknown classes are ignored,
    # so pin the canonical names instead of shipping dead classes.
    legacy = ("input-bordered", "select-bordered", "textarea-bordered", "label-text")
    offenders = []
    for name in sorted(TEMPLATES.glob("*.html")):
        src = name.read_text()
        found = sorted({token for token in legacy if token in src})
        if found:
            offenders.append(f"{name.name}: {', '.join(found)}")
    assert not offenders, "legacy field classes: " + "; ".join(offenders)


def test_clean_ui_rhythm_and_logical_properties():
    # Clean UI reads as one system: a single breadcrumb pattern, one
    # page-header rhythm, one filter-bar surface, hairline borders from
    # one token, and logical (RTL-safe) spacing properties throughout.
    offenders = []
    physical = ("mr-auto", "ml-auto", "ml-1", "ml-2", "ml-5", "ml-14")
    for name in sorted(TEMPLATES.glob("*.html")):
        src = name.read_text()
        for token in physical + ("border-base-200", '<div class="breadcrumbs', '<ol class="breadcrumbs', '<ul class="breadcrumbs'):
            if token in src:
                offenders.append(f"{name.name}: {token}")
        for line in src.splitlines():
            if "mb-5" in line and "tabs" not in line:
                offenders.append(f"{name.name}: {line.strip()[:80]}")
            if "gap-3 mt-1 mb-4" in line:
                offenders.append(f"{name.name}: {line.strip()[:80]}")
            if 'role="search"' in line and ("rounded-lg" in line and "mb-4" in line):
                offenders.append(f"{name.name}: {line.strip()[:80]}")
    assert not offenders, "clean-ui rhythm: " + "; ".join(offenders)


def test_templates_have_balanced_tags():
    # A mismatched open/close tag silently reparents the rest of the page
    # in the browser (e.g. drawer content escaping its container), so the
    # HTML each route actually serves must nest cleanly. Rendered output
    # is checked rather than raw templates so Jinja control flow and the
    # spec's optional end tags (like <option>) cannot cause false alarms.
    from html.parser import HTMLParser

    void = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
    # Tags the HTML spec auto-closes: every browser parses a missing end
    # tag here identically, so it cannot reparent content.
    autoclosed = {"option": ("option", "optgroup", "select", "datalist")}

    class Balance(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.open = []
            self.errors = []

        def _close_through(self, tag):
            while self.open and self.open[-1] in autoclosed and tag in autoclosed[self.open[-1]]:
                self.open.pop()

        def handle_starttag(self, tag, attrs):
            self._close_through(tag)
            if tag not in void:
                self.open.append(tag)

        def handle_startendtag(self, tag, attrs):
            self._close_through(tag)

        def handle_endtag(self, tag):
            if tag in void:
                return
            # An explicit </option> matches its own opener; only a
            # *different* closer (</select>, next <option>) sweeps it shut.
            while self.open and self.open[-1] in autoclosed and tag != self.open[-1] and tag in autoclosed[self.open[-1]]:
                self.open.pop()
            if not self.open or self.open[-1] != tag:
                self.errors.append(f"</{tag}> closes {self.open[-1] if self.open else 'nothing'}")
            if tag in self.open:
                self.open = self.open[:len(self.open) - 1 - self.open[::-1].index(tag)]

    routes = [
        "/", "/minions/", "/minions/web-00", "/keys/", "/jobs/?tab=history",
        "/jobs/?tab=saved", "/jobs/hist-000", "/states/", "/pillar/",
        "/pillar/diff", "/schedules/", "/events/", "/audit/", "/users/",
        "/users/groups", "/settings/", "/settings/access", "/settings/rotation",
        "/settings/sso", "/groups/", "/files/", "/files/?tab=repo", "/reactor/",
        "/mine/", "/console/", "/jobs/new", "/minions/onboard",
        "/vulnerabilities/",
    ]
    c = make_client()
    offenders = []
    for route in routes:
        rv = c.get(route)
        assert rv.status_code == 200, route
        parser = Balance()
        parser.feed(rv.data.decode())
        if parser.errors or parser.open:
            detail = "; ".join(parser.errors[:3])
            if parser.open:
                detail += f"; unclosed: {parser.open[-3:]}"
            offenders.append(f"{route}: {detail}")
    assert not offenders, "unbalanced tags: " + " | ".join(offenders)


def test_shared_visual_system_covers_app_shell_settings_and_console():
    css = (
        pathlib.Path(__file__).resolve().parent.parent
        / "overstate_ui"
        / "static"
        / "app.css"
    ).read_text()
    assert ':root[data-theme="wireframe"]' not in css
    assert "#term" in css
    assert "prefers-reduced-motion: reduce" in css

    source = (
        pathlib.Path(__file__).resolve().parent.parent / "assets" / "app.css"
    ).read_text()
    assert "themes: corporate --default, dark" in source
    for rule in (
        ":focus-visible",
        "#term",
        "#console-form",
        "@media (prefers-reduced-motion: reduce)",
    ):
        assert rule in source
        assert rule in css

    shell = (TEMPLATES / "base.html").read_text()
    for marker in (
        'data-theme="corporate"',
        "drawer lg:drawer-open",
        "drawer-side",
        "menu-title",
        "Operate",
        "Configure",
        "Observe",
        "System",
        "menu-active",
        "palette-btn",
        "⌘K",
        "max-w-7xl",
        "htmx-error-note",
        "_confirm.html",
    ):
        assert marker in shell, marker
    assert "_nav.html" not in shell
    assert 'aria-label="Secondary"' not in shell
    assert 'id="content"' not in shell
    assert not (TEMPLATES / "_nav.html").exists()
