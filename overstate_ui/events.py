"""Filtered event viewer. Raw payloads never reach the browser: the server
subscribes to salt-api /events, matches tag prefixes, and streams only
{tag, stamp} summaries over SSE, capped per connection."""

import json
import time

import httpx
from flask import (
    Blueprint,
    Response,
    flash,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)
from flask_login import login_required

from .dashboard import get_salt
from .salt_client import SaltApiError

bp = Blueprint("events", __name__, url_prefix="/events")

TAG_CHOICES = ["salt/job", "salt/auth", "salt/minion/", "salt/key", "salt/run/"]

MAX_EVENTS = 50
MAX_SECONDS = 60


def match_prefixes(tag: str, prefixes: list[str]) -> bool:
    return any(tag.startswith(p) for p in prefixes)


@bp.route("/")
@login_required
def index():
    prefixes = request.args.getlist("tag") or ["salt/job"]
    valid = [p for p in prefixes if p in TAG_CHOICES]
    if not valid:
        flash("Select at least one event family.", "error")
        return redirect(url_for("events.index", tag="salt/job"))
    return render_template("events.html", choices=TAG_CHOICES, selected=valid)


@bp.route("/stream")
@login_required
def stream():
    prefixes = [p for p in request.args.getlist("tag") if p in TAG_CHOICES]
    if not prefixes:
        prefixes = ["salt/job"]

    def filtered():
        sent = 0
        start = time.monotonic()
        try:
            for event in get_salt().event_stream():
                tag = str(event.get("tag", ""))
                if not match_prefixes(tag, prefixes):
                    continue
                data = event.get("data", {})
                summary = {
                    "tag": tag,
                    "stamp": str(data.get("_stamp", "")),
                }
                yield f"data: {json.dumps(summary)}\n\n"
                sent += 1
                if sent >= MAX_EVENTS or time.monotonic() - start > MAX_SECONDS:
                    break
        except SaltApiError as exc:
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
        except httpx.TimeoutException:
            yield "event: error\ndata: {\"error\": \"event stream idle timeout\"}\n\n"
        yield "event: done\ndata: {}\n\n"

    return Response(stream_with_context(filtered()), mimetype="text/event-stream")
