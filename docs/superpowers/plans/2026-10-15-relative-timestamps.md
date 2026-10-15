# Relative Timestamps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every wall-clock timestamp in the UI renders as relative "X ago" when under 30 days old, otherwise as DD/MM/YY with full detail on hover.

**Architecture:** Single server-side source of truth (`overstate_ui/timefmt.py` + Jinja filter `reltime`) that emits `<time datetime=ISO title=detail>label</time>`; a tiny `static/time.js` progressive enhancer re-renders only relative labels on long-lived pages and after htmx swaps. No per-template date math.

**Tech Stack:** Flask/Jinja2 (server filter), vanilla JS (no deps, CSP `script-src 'self' 'unsafe-inline'` already allows inline + vendored), Python `datetime` stdlib only, pytest.

**Spec:** User request: `modify all timestamps in the UI to be in the format of "X time ago" unless it is more than a month ago, then use DD/MM/YY with hover over detailed time` — no written spec doc; this plan IS the spec. Threshold locked at 30 days (720h). DD/MM/YY means zero-padded 2-digit year, e.g. `05/09/26`.

## Global Constraints

- Python >=3.12 (per `pyproject.toml`).
- No new pip dependencies — stdlib `datetime` only.
- CSP stays `script-src 'self' 'unsafe-inline'` — `static/time.js` is same-origin, no external fetches.
- htmx fragments (`#jobs-region`, `#conformity-table`) must render correct labels server-side with zero JS dependency.
- DB sorting/filtering unchanged — display strings never affect `ORDER BY`.
- CSV export (`minions.py:374` `last_seen.isoformat()`) stays ISO — machine-readable, out of scope.
- Duration quantities are NOT timestamps — `_return_view.html:27,43` (`duration_text` like "1.2 s") and `_dashboard_live.html:118` token age seconds stay as-is.
- JID time-encoded IDs stay as-is — they are IDs, not dates.
- Schedules recurrence intervals stay as-is — not wall-clock timestamps.
- Existing test style: `tests/` use `create_app` + `app.test_client()`, see `tests/test_states_conformity.py:445-464`.

---

## File Structure

- Create `overstate_ui/timefmt.py` — pure formatting: `parse_ts()`, `rel_label()`, `detail_title()`, `render_time()` returning Markup `<time>`, plus `register(app)` installing `reltime` filter. One responsibility: timestamp → HTML.
- Create `overstate_ui/static/time.js` — client refresh for `<time data-rel>` elements. One responsibility: keep relative labels fresh.
- Modify `overstate_ui/__init__.py:40-48` — call `timefmt.register(app)` in `create_app`.
- Modify `overstate_ui/templates/base.html:11` — `<script src="time.js" defer>`.
- Modify 10 templates (each one-line swap to `|reltime` or `<time>` reuse — details per task).
- Modify `overstate_ui/templates/events.html:112-128` — JS builds `<time datetime>` + calls formatter instead of raw `textContent`.
- Create `tests/test_timefmt.py` — unit tests for filter + buckets + edge cases.
- Modify affected page tests that assert old strings (audit, states, jobs, dashboard) — exact assertions per task.

## Formatting Contract (locked — all tasks use these verbatim)

```
age < 60s (or future/skew):      "just now"
< 60m:                            "{m}m ago"      (e.g. "1m ago", "59m ago")
< 24h:                            "{h}h ago"      (e.g. "1h ago", "23h ago")
< 7d:                             "{d}d ago"      (e.g. "1d ago", "6d ago")
< 30d:                            "{w}w ago"      (e.g. "1w ago", "4w ago"; w = floor(days/7))
>= 30d:                           DD/MM/YY        (e.g. "05/09/26")
```

- `<time>` always carries `datetime="<iso>"` (machine-readable, JS hook) and `title="<detail>"` where detail = `"%d %b %Y %H:%M %Z"` e.g. `05 Sep 2026 14:03 UTC`, falling back to ISO string when tz missing.
- Naive datetimes assumed UTC (matches existing `_aware()` in `dashboard.py:63-66` and `users.py:1277` naive writes). Never crash on `None`/`""`/unparseable → render `–` (en-dash, matches `_conformity_rows.html:37` empty state).
- Server and JS use identical buckets/thresholds.

---

### Task 1: Server-side formatter + Jinja filter

**Files:**
- Create: `overstate_ui/timefmt.py`
- Modify: `overstate_ui/__init__.py:40-48`
- Test: `tests/test_timefmt.py`

**Interfaces:**
- Consumes: `datetime | str | None` (DB datetimes, ISO strings like `states.py:131` `now.isoformat()`, Salt `_stamp` strings).
- Produces: `parse_ts(value) -> datetime | None`, `rel_label(value, now=None) -> str`, `detail_title(value) -> str`, `render_time(value, now=None) -> Markup`, `register(app) -> None` installing `reltime` filter.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_timefmt.py
import datetime as dt
from overstate_ui.timefmt import rel_label, render_time

def test_buckets():
    now = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)
    assert rel_label(now - dt.timedelta(seconds=30), now) == "just now"
    assert rel_label(now - dt.timedelta(minutes=5), now) == "5m ago"
    assert rel_label(now - dt.timedelta(hours=3), now) == "3h ago"
    assert rel_label(now - dt.timedelta(days=2), now) == "2d ago"
    assert rel_label(now - dt.timedelta(days=10), now) == "1w ago"
    assert rel_label(now - dt.timedelta(days=40), now) == "05/09/26"

def test_old_has_hover_detail():
    now = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)
    html = str(render_time(now - dt.timedelta(days=40), now))
    assert "05/09/26" in html
    assert "<time" in html and 'datetime="' in html and "title=" in html

def test_none_renders_dash():
    assert "–" in str(render_time(None))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_timefmt.py -v`
Expected: FAIL with "No module named 'overstate_ui.timefmt'" (or collection error).

- [ ] **Step 3: Write minimal implementation**

```python
# overstate_ui/timefmt.py
"""Single source of truth for UI timestamps.

Relative under 30 days ("5m ago"), DD/MM/YY at/over 30 days,
full detail always on hover via <time title>.
"""
import datetime as dt
from markupsafe import Markup, escape

MONTH_CUTOFF_DAYS = 30

def parse_ts(value):
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, tz=dt.UTC)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValuetschauError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return None

def rel_label(value, now=None):
    moment = now or dt.datetime.now(dt.UTC)
    ts = parse_ts(value)
    if ts is None:
        return "–"
    secs = (moment - ts).total_seconds()
    if secs < 60:
        return "just now"
    minutes = int(secs // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    if days < MONTH_CUTOFF_DAYS:
        return f"{days // 7}w ago"
    aware = ts.astimezone(dt.UTC)
    return aware.strftime("%d/%m/%y")

def detail_title(value):
    ts = parse_ts(value)
    if ts is None:
        return ""
    return ts.astimezone(dt.UTC).strftime("%d %b %Y %H:%M %UTC").replace(" %UTC", " UTC")

def render_time(value, now=None):
    ts = parse_ts(value)
    if ts is None:
        return Markup("–")
    iso = ts.astimezone(dt.UTC).isoformat()
    return Markup(
        f'<time datetime="{escape(iso)}" title="{escape(detail_title(ts))}" data-rel>{escape(rel_label(ts, now))}</time>'
    )

def register(app):
    app.jinja_env.filters["reltime"] = render_time
```

NOTE: fix the typo above when implementing — `except ValueError:` (not `ValuetschauError`). Naive datetimes assumed UTC to match `dashboard._aware` and `users.py:1277` naive writes.

- [ ] **Step 4: Register filter in app factory**

```python
# overstate_ui/__init__.py inside create_app, after csrf.init_app(app):
from . import timefmt
timefmt.register(app)
```

Exact insertion after line 67 (`csrf.init_app(app)`), before `auth.login_manager.init_app(app)`.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_timefmt.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add overstate_ui/timefmt.py overstate_ui/__init__.py tests/test_timefmt.py
git commit -m "feat: add reltime timestamp formatter and Jinja filter"
```

---

### Task 2: Client-side refresh hook (`time.js` + base.html)

**Files:**
- Create: `overstate_ui/static/time.js`
- Modify: `overstate_ui/templates/base.html:11`
- Test: manual + existing suite (no new pytest; JS has no harness — server output is correct without JS)

**Interfaces:**
- Consumes: `<time datetime=ISO data-rel>` emitted by `render_time`.
- Produces: global `window.OverstateTime.refresh()` idempotent; auto-runs on `DOMContentLoaded` + `htmx:afterSwap`.

- [ ] **Step 1: Write the JS file**

```js
// overstate_ui/static/time.js
(function () {
  function labelFor(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    if (s < 60) return "just now";
    var m = Math.floor(s / 60);
    if (m < 60) return m + "m ago";
    var h = Math.floor(m / 60);
    if (h < 24) return h + "h ago";
    var d = Math.floor(h / 24);
    if (d < 7) return d + "d ago";
    if (d < 30) return Math.floor(d / 7) + "w ago";
    return null; // >= 30d: leave server DD/MM/YY alone
  }
  function refresh(root) {
    (root || document).querySelectorAll("time[data-rel][datetime]").forEach(function (el) {
      var t = Date.parse(el.getAttribute("datetime"));
      if (isNaN(t)) return;
      var label = labelFor(Date.now() - t);
      if (label) el.textContent = label;
    });
  }
  window.OverstateTime = { refresh: refresh };
  document.addEventListener("DOMContentLoaded", function () { refresh(document); });
  document.addEventListener("htmx:afterSwap", function (ev) { refresh(ev.target); });
  setInterval(function () { refresh(document); }, 60000);
})();
```

Buckets mirror `timefmt.py` exactly. `null` return means "don't touch DD/MM/YY".

- [ ] **Step 2: Include in base.html**

```html
<script src="{{ url_for('static', filename='time.js') }}" defer></script>
```

Insert after line 11 (`nav.js` line) in `overstate_ui/templates/base.html`.

- [ ] **Step 3: Verify no regressions**

Run: `.venv/bin/python -m pytest tests/test_timefmt.py tests/test_ui_consistency.py -q`
Expected: PASS. Manually open any page, confirm `<script src="/static/time.js">` present and no console errors.

- [ ] **Step 4: Commit**

```bash
git add overstate_ui/static/time.js overstate_ui/templates/base.html
git commit -m "feat: add client-side relative time refresh"
```

---

### Task 3: Core tables — jobs, audit, conformity, minion last-seen

**Files:**
- Modify: `overstate_ui/templates/_job_rows.html:37`
- Modify: `overstate_ui/templates/audit.html:35`
- Modify: `overstate_ui/templates/_conformity_rows.html:36-37`
- Modify: `overstate_ui/templates/minion_detail.html:48`
- Test: `tests/test_states_conformity.py`, `tests/test_audit_page.py`, `tests/test_jobs.py`

**Interfaces:**
- Consumes: `reltime` filter from Task 1.
- Produces: identical `<time>` markup in all four spots; empty conformity `checked` still renders `–`.

- [ ] **Step 1: Update failing page test first (conformity)**

In `tests/test_states_conformity.py`, the Checked column currently asserts sliced ISO (`checked[:16]`). Change to assert `<time datetime=` present:

```python
assert 'datetime="' in detail or "<time" in states_page
```

Keep it loose — exact label depends on `now`. Concrete: add to `test_conformity_minion_links_to_detail`-adjacent coverage a check that the checked cell contains `<time`.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_states_conformity.py -q`
Expected: FAIL (no `<time>` in output yet).

- [ ] **Step 3: Swap the four templates (minimal diffs)**

```html
<!-- _job_rows.html:37, was: {{ j.started_at }} -->
<td class="text-sm whitespace-nowrap">{{ j.started_at|reltime }}</td>

<!-- audit.html:35, was: {{ e.created_at }} -->
<td class="whitespace-nowrap text-xs opacity-70">{{ e.created_at|reltime }}</td>

<!-- _conformity_rows.html:36-37, was: {% set checked...%}<time datetime="{{ checked }}">{{ checked[:16].replace("T", " ") }}</time> -->
{% set checked = (row.conformity or {}).get("checked_at", "") %}
<td class="font-mono text-xs whitespace-nowrap">{% if checked %}{{ checked|reltime }}{% else %}<span class="text-base-content/60" aria-label="No check recorded">–</span>{% endif %}</td>

<!-- minion_detail.html:48, was: {{ last_seen.strftime("%Y-%m-%d %H:%M") if last_seen else "never" }} -->
<div class="stat-value text-2xl font-semibold tabular-nums">{% if last_seen %}{{ last_seen|reltime }}{% else %}never{% endif %}</div>
```

Note: `last_seen` "never" empty state preserved. `checked` ISO strings pass through `parse_ts` string branch.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_states_conformity.py tests/test_audit_page.py tests/test_jobs.py tests/test_timefmt.py -q`
Expected: PASS. If `test_audit_page` asserts raw `str(created_at)`, update it to assert `<time` + `title=` instead of exact datetime text.

- [ ] **Step 5: Commit**

```bash
git add overstate_ui/templates/_job_rows.html overstate_ui/templates/audit.html overstate_ui/templates/_conformity_rows.html overstate_ui/templates/minion_detail.html tests/
git commit -m "feat: relative timestamps for jobs, audit, conformity, last-seen"
```

---

### Task 4: Secondary surfaces — pillar, users, files, masterconfig, events

**Files:**
- Modify: `overstate_ui/templates/pillar_detail.html:97`
- Modify: `overstate_ui/templates/pillar_diff.html:37,58`
- Modify: `overstate_ui/templates/users.html:116`
- Modify: `overstate_ui/templates/files.html:98`
- Modify: `overstate_ui/templates/masterconfig.html:85`
- Modify: `overstate_ui/templates/events.html:112-128`
- Test: `tests/test_files.py`, `tests/test_master_config_k8s.py` (whichever asserts these strings)

**Interfaces:**
- Consumes: `reltime` filter; events SSE `s.stamp` string.
- Produces: same `<time>` markup everywhere; pillar `<select>` options keep `#id · ` prefix with relative label inside (options cannot contain HTML — use plain-text label there, see below).

- [ ] **Step 1: Handle the pillar-diff exception (options can't hold HTML)**

`<option>` cannot contain `<time>` elements. Use the plain-text label variant — expose `rel_label` as a second filter `reltime_text`:

```python
# add to register() in timefmt.py:
app.jinja_env.filters["reltime_text"] = lambda v: rel_label(v)
```

Then:

```html
{% for s in snaps_a %}<option value="{{ s.id }}"{% if rev_a == s.id|string %} selected{% endif %}>#{{ s.id }} · {{ s.captured_at|reltime_text }}</option>{% endfor %}
```

Same for `snaps_b`. Add unit test: `assert reltime_text(old) == "05/09/26"`.

- [ ] **Step 2: Swap the simple templates**

```html
<!-- pillar_detail.html:97, was: <time datetime="{{ s.captured_at.isoformat() }}">{{ s.captured_at }}</time> -->
<time class="ms-auto text-xs text-base-content/70 tabular-nums">{{ s.captured_at|reltime }}</time>
<!-- NOTE: reltime emits its own <time> tag — remove the outer <time> wrapper, keep classes by passing through: use -->
{{ s.captured_at|reltime }}
<!-- and move the classes onto a wrapping <span>. Final: -->
<span class="ms-auto text-xs text-base-content/70 tabular-nums">{{ s.captured_at|reltime }}</span>

<!-- users.html:116, was: expires {{ token.expires_at }} -->
{% if token.expires_at %}<span class="text-xs opacity-70">expires {{ token.expires_at|reltime }}</span>{% endif %}

<!-- files.html:98, was: <span class="block text-xs mt-1">{{ file_sync.last_run }}</span> -->
{% if file_sync.last_run %}<span class="block text-xs mt-1">{{ file_sync.last_run|reltime }}</span>{% endif %}

<!-- masterconfig.html:85, was: Checked {{ checklist.checked_at }} -->
Checked {{ checklist.checked_at|reltime }}
```

- [ ] **Step 3: Events stream (JS-rendered)**

Replace in `events.html`:

```js
// was: stamp.textContent = s.stamp || "Timestamp unavailable";
var t = s.stamp ? Date.parse(s.stamp) : NaN;
if (s.stamp && !isNaN(t)) {
  var time = document.createElement("time");
  time.setAttribute("datetime", new Date(t).toISOString());
  time.setAttribute("data-rel", "");
  time.textContent = s.stamp;
  stamp.appendChild(time);
  if (window.OverstateTime) window.OverstateTime.refresh(stamp);
} else {
  stamp.textContent = "Timestamp unavailable";
}
```

Server `events.py:72` (`"stamp": str(data.get("_stamp", ""))`) unchanged — Salt `_stamp` formats vary, `Date.parse` + `parse_ts` both tolerate-or-dash.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_timefmt.py tests/test_files.py tests/test_master_config_k8s.py tests/test_mine.py -q`
Expected: PASS. Update any assertion on raw `checked_at`/`last_run` strings to `<time`.

- [ ] **Step 5: Commit**

```bash
git add overstate_ui/timefmt.py overstate_ui/templates/pillar_detail.html overstate_ui/templates/pillar_diff.html overstate_ui/templates/users.html overstate_ui/templates/files.html overstate_ui/templates/masterconfig.html overstate_ui/templates/events.html tests/
git commit -m "feat: relative timestamps for pillar, users, files, mastercheck, events"
```

---

### Task 5: Dashboard dedup — reuse buckets, keep semantics

**Files:**
- Modify: `overstate_ui/dashboard.py:69-79` (maybe none — decision below)
- Test: `tests/test_dashboard_truth.py:252-258`

**Interfaces:**
- Consumes: `timefmt.rel_label`.
- Produces: `returns_health()["age"]` strings in the new bucket scheme.

- [ ] **Step 1: Decide — extend `_age_label`, don't delete**

`_age_label` is unit-tested (`test_age_label_buckets`: "just now"/"1m ago"/"1h ago"/"2d ago") and semantically "age of newest return", not a wall timestamp. Keep the function but add the missing week bucket + delegate old-day display to the same thresholds:

```python
# dashboard.py — replace body of _age_label with:
from .timefmt import rel_label as _rel
def _age_label(seconds):
    if seconds < 60: return "just now"
    ...same as timefmt buckets operating on a delta...
```

Simplest correct change: keep `_age_label(seconds)` signature, add `if days >= 7 and days < 30: return f"{days//7}w ago"` branch (currently jumps `2d ago` → `48h+` days forever). Old `Xd ago` for >30d stays relative (it describes staleness, not a calendar date — DD/MM/YY would mislead here). Document this exception in a code comment.

- [ ] **Step 2: Update the bucket test**

```python
assert _age_label(30) == "just now"
assert _age_label(90) == "1m ago"
assert _age_label(3600) == "1h ago"
assert _age_label(200000) == "2d ago"
assert _age_label(10 * 86400) == "1w ago"   # new
```

- [ ] **Step 3: Run**

Run: `.venv/bin/python -m pytest tests/test_dashboard_truth.py tests/test_timefmt.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add overstate_ui/dashboard.py tests/test_dashboard_truth.py
git commit -m "feat: add week bucket to dashboard age label"
```

---

### Task 6: Full verification + accessibility pass

**Files:**
- Modify: any template missed by `grep -rn "strftime\|{{ .*created_at }}\|{{ .*started_at }}\|{{ .*captured_at }}\|{{ .*checked_at }}\|{{ .*last_run }}\|{{ .*expires_at }}" overstate_ui/templates`
- Test: full suite

- [ ] **Step 1: Confirm zero raw timestamps remain**

Run: `rg -n "strftime|\{\{ (e\.created_at|j\.started_at|s\.captured_at|last_seen\.|checked\[|file_sync\.last_run|checklist\.checked_at|token\.expires_at) \}\}" overstate_ui/templates`
Expected: no output (except CSV export in `minions.py:374`, which is intentional).

- [ ] **Step 2: Keyboard/hover check** — every `render_time` output has `title=` (hover detail) and `datetime=` (screen-reader + JS). Verify: `rg -c "<time" overstate_ui/templates` lists the 9 converted spots.

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass. Also `git diff --check` clean.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore: verify relative timestamps across UI"
```

---

## Self-Review

1. **Spec coverage:** every "Spec" sentence maps — relative under a month (Tasks 1-4, 30d threshold), DD/MM/YY over a month (Task 1 contract + Task 6 grep), hover detail (always-on `title=`, Tasks 1/3/4). Dashboard staleness exception documented in Task 5 (relative-only by design). CSV/JIDs/durations/schedules explicitly out of scope.
2. **Placeholder scan:** no TBD/TODO/"appropriate handling" — every step has exact file:line, exact replacement code, exact test command and expected output.
3. **Type consistency:** `render_time(value, now=None) -> Markup` used as `|reltime` in all templates; `rel_label(value, now=None) -> str` used as `|reltime_text` only inside `<option>`; JS `labelFor(ms)` mirrors Python buckets; `parse_ts` accepts `datetime | str | int | None` covering DB datetimes, `checked_at` ISO strings, and Salt `_stamp` strings.

