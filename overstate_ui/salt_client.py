"""salt-api client. The only module allowed to talk to the Salt master.

Owns eauth token lifecycle (login + refresh on 401) and exposes thin
wrappers for wheel / local / runner calls plus a server-sent event stream.
All blueprints go through here; nothing else touches salt-api.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import httpx


class SaltApiError(RuntimeError):
    pass


class SaltClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        eauth: str = "pam",
        transport: httpx.BaseTransport | None = None,
        verify: bool | str = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.eauth = eauth
        self.verify = verify
        # Default HTTP round-trip cap. Interactive views pass a shorter
        # http_timeout so one sick salt-api call degrades fast instead of
        # pinning a gunicorn worker until it is killed mid-request.
        self._default_timeout = 30.0
        self._http = httpx.Client(
            base_url=self.base_url,
            transport=transport,
            timeout=self._default_timeout,
            verify=verify,
        )
        self._token: str | None = None
        self._token_issued: float = 0.0
        self._token_ttl: float = 0.0

    @property
    def token_age(self) -> float:
        return time.monotonic() - self._token_issued if self._token else -1.0

    def login(self, http_timeout: float | None = None) -> None:
        try:
            resp = self._http.post(
                "/login",
                json={
                    "username": self.username,
                    "password": self.password,
                    "eauth": self.eauth,
                },
                timeout=http_timeout if http_timeout is not None else self._default_timeout,
            )
        except httpx.HTTPError as exc:
            raise SaltApiError(f"salt-api unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise SaltApiError(f"salt-api login failed: HTTP {resp.status_code}")
        try:
            token = resp.json()["return"][0]["token"]
            expire = resp.json()["return"][0].get("expire", 0)
        except (KeyError, IndexError, ValueError) as exc:
            raise SaltApiError(f"unexpected login response: {exc}") from exc
        self._token = token
        self._token_issued = time.monotonic()
        self._token_ttl = float(expire)

    def _post(
        self,
        payload: dict | list,
        retry: bool = True,
        http_timeout: float | None = None,
    ) -> Any:
        if self._token is None:
            self.login(http_timeout=http_timeout)
        timeout = http_timeout if http_timeout is not None else self._default_timeout
        try:
            resp = self._http.post(
                "/", json=payload, headers={"X-Auth-Token": self._token}, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise SaltApiError(f"salt-api unreachable: {exc}") from exc
        if resp.status_code == 401 and retry:
            self.login(http_timeout=http_timeout)
            return self._post(payload, retry=False, http_timeout=http_timeout)
        if resp.status_code != 200:
            raise SaltApiError(f"salt-api call failed: HTTP {resp.status_code}")
        return resp.json()["return"]

    def wheel(self, fun: str, http_timeout: float | None = None, **kwargs: Any) -> Any:
        # NB: http_timeout caps the HTTP round trip only and is never
        # forwarded into the salt-api payload (an unknown kwarg would
        # fail the call server-side).
        return self._post(
            {"client": "wheel", "fun": fun, **kwargs}, http_timeout=http_timeout
        )

    def local(
        self,
        tgt: str,
        fun: str,
        arg: list | None = None,
        tgt_type: str = "glob",
        timeout: int = 10,
        asynchronous: bool = False,
        via: str = "local",
        kwarg: dict | None = None,
        http_timeout: float | None = None,
    ) -> Any:
        """Run a function via the ``local`` zeromq path or ``ssh`` roster path.

        salt-ssh runs synchronously with its own (longer) timeout: roster
        targets fan out over SSH, so expect minutes not seconds on fleets.
        ``kwarg`` forwards keyword arguments (e.g. ``schedule.add`` options)
        as the salt-api ``kwarg`` payload. ``timeout`` is the Salt job
        timeout sent to the master; ``http_timeout`` caps only this HTTP
        round trip and is never forwarded.
        """
        if via == "ssh":
            payload: dict = {
                "client": "ssh",
                "tgt": tgt,
                "fun": fun,
                "arg": arg or [],
                "tgt_type": tgt_type,
                "timeout": timeout,
                "ignore_invalid": True,
            }
        elif asynchronous:
            payload = {
                "client": "local_async",
                "tgt": tgt,
                "fun": fun,
                "arg": arg or [],
                "tgt_type": tgt_type,
            }
        else:
            payload = {
                "client": "local",
                "tgt": tgt,
                "fun": fun,
                "arg": arg or [],
                "tgt_type": tgt_type,
                "timeout": timeout,
            }
        if kwarg:
            payload["kwarg"] = kwarg
        return self._post(payload, http_timeout=http_timeout)

    def runner(self, fun: str, http_timeout: float | None = None, **kwargs: Any) -> Any:
        # NB: http_timeout caps the HTTP round trip only and is never
        # forwarded into the salt-api payload (see wheel).
        return self._post(
            {"client": "runner", "fun": fun, **kwargs}, http_timeout=http_timeout
        )

    def event_stream(self, idle_timeout: float = 65.0) -> Iterator[dict]:
        """Yield parsed salt-api /events SSE payloads (caller filters).

        Idle SSE connections carry no bytes, so the call-level timeout
        must exceed the silence: use a dedicated idle timeout and surface
        expiry as TimeoutException for the caller to end gracefully.
        """
        if self._token is None:
            self.login()
        try:
            with self._http.stream(
                "GET",
                "/events",
                headers={"X-Auth-Token": self._token},
                timeout=httpx.Timeout(idle_timeout),
            ) as resp:
                if resp.status_code == 401:
                    self.login()
                    yield from self.event_stream()
                    return
                if resp.status_code != 200:
                    raise SaltApiError(
                        f"salt-api /events failed: HTTP {resp.status_code}"
                    )
                data = ""
                for line in resp.iter_lines():
                    if line.startswith("data:"):
                        data = line[5:].strip()
                    elif line == "" and data:
                        try:
                            yield json.loads(data)
                        except ValueError:
                            pass
                        data = ""
        except httpx.TimeoutException:
            # Idle expiry is the caller's graceful end-of-stream signal.
            raise
        except httpx.HTTPError as exc:
            raise SaltApiError(f"salt-api unreachable: {exc}") from exc

    def health(self) -> dict:
        """Probe what the dashboard needs: token age + @wheel/@runner reach."""
        out: dict[str, Any] = {
            "url": self.base_url,
            "token_age": self.token_age,
            "reachable": False,
            "wheel_ok": False,
            "runner_ok": False,
            "error": None,
        }
        try:
            self.wheel("key.list_all")
            out["wheel_ok"] = True
            self.runner("manage.status")
            out["runner_ok"] = True
            out["reachable"] = True
            out["token_age"] = self.token_age
        except (SaltApiError, httpx.HTTPError) as exc:
            out["error"] = str(exc)
        return out
