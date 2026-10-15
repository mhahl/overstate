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
