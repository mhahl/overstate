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
        except ValueError:
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
    return ts.astimezone(dt.UTC).strftime("%d %b %Y %H:%M %Z")


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
