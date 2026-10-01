"""
Minimal aiohttp-compatible HTTP/WebSocket facades on top of Starlette/FastAPI.

Keeps existing handlers and unit tests working against familiar APIs:
- request.match_info / query / remote / app[...] / multipart()
- web.Response / json_response / FileResponse / HTTPFound
- WSMsgType + async iteration over WebSocket messages
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
import asyncio

from starlette.requests import Request
from starlette.responses import FileResponse as StarletteFileResponse
from starlette.responses import JSONResponse as StarletteJSONResponse
from starlette.responses import RedirectResponse
from starlette.responses import Response as StarletteResponse
from starlette.websockets import WebSocket, WebSocketDisconnect


class Response(StarletteResponse):
    """Starlette Response with aiohttp-like status/text/content_type."""

    def __init__(
        self,
        *,
        text: Optional[str] = None,
        body: Any = None,
        status: int = 200,
        content_type: Optional[str] = None,
        headers: Optional[dict] = None,
        **kwargs,
    ):
        if text is not None:
            self._text = text
            content = text.encode("utf-8")
        elif body is not None:
            if isinstance(body, bytes):
                content = body
                self._text = body.decode("utf-8", errors="replace")
            else:
                self._text = str(body)
                content = self._text.encode("utf-8")
        else:
            self._text = ""
            content = b""

        super().__init__(
            content=content,
            status_code=status,
            headers=headers,
            media_type=content_type,
            **kwargs,
        )

    @property
    def status(self) -> int:
        return self.status_code

    @property
    def content_type(self) -> Optional[str]:
        return self.media_type

    @property
    def text(self) -> str:
        return self._text


class JsonResponse(StarletteJSONResponse):
    """JSONResponse with aiohttp-like status/text/content_type."""

    @property
    def status(self) -> int:
        return self.status_code

    @property
    def content_type(self) -> str:
        return self.media_type or "application/json"

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


class FileResponse(StarletteFileResponse):
    """FileResponse keeping aiohttp's `_path` attribute for tests."""

    def __init__(self, path, headers: Optional[dict] = None, **kwargs):
        self._path = Path(path)
        super().__init__(path, headers=headers, **kwargs)

    @property
    def status(self) -> int:
        return self.status_code

    @property
    def content_type(self) -> Optional[str]:
        return self.media_type

    @property
    def text(self) -> str:
        return ""


def json_response(data: Any, status: int = 200, **kwargs) -> JsonResponse:
    return JsonResponse(data, status_code=status, **kwargs)


def HTTPFound(location: str) -> RedirectResponse:
    return RedirectResponse(url=location, status_code=302)


class _WebNamespace:
    Response = Response
    FileResponse = FileResponse
    json_response = staticmethod(json_response)
    HTTPFound = staticmethod(HTTPFound)


web = _WebNamespace()


class AppProxy:
    """Dict-like access to FastAPI/Starlette app.state (and plain dicts in tests)."""

    def __init__(self, app):
        self._app = app

    def __getitem__(self, key):
        if isinstance(self._app, dict):
            return self._app[key]
        state = getattr(self._app, "state", None)
        if state is not None and hasattr(state, key):
            return getattr(state, key)
        raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


class _MultipartPart:
    def __init__(self, name: str, filename: Optional[str], upload):
        self.name = name
        self.filename = filename
        self._upload = upload
        self._buffer: Optional[bytes] = None
        self._offset = 0

    async def read_chunk(self, size: int = 8192) -> bytes:
        if self._buffer is None:
            self._buffer = await self._upload.read()
            self._offset = 0
        chunk = self._buffer[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class _MultipartReader:
    def __init__(self, parts: list[_MultipartPart]):
        self._parts = parts
        self._index = 0

    async def next(self) -> Optional[_MultipartPart]:
        if self._index >= len(self._parts):
            return None
        part = self._parts[self._index]
        self._index += 1
        return part


class RequestBodyTooLarge(Exception):
    pass


class CompatRequest:
    """Wrap Starlette Request with aiohttp-like attributes used by handlers."""

    def __init__(self, request: Request):
        self._request = request
        self.match_info = request.path_params
        self.query = request.query_params
        self.headers = request.headers
        self.method = request.method
        self.path = request.url.path
        self.scheme = request.url.scheme
        self.cookies = request.cookies
        self._state: dict[str, Any] = {}
        self.user = None
        self.permissions = None

    def __getitem__(self, key):
        return self._state[key]

    def __setitem__(self, key, value):
        self._state[key] = value

    def get(self, key, default=None):
        return self._state.get(key, default)

    @property
    def remote(self) -> str:
        if self._request.client is None:
            return ""
        return self._request.client.host or ""

    @property
    def app(self):
        return AppProxy(self._request.app)

    @property
    def rel_url(self):
        return SimpleNamespace(query_string=self._request.url.query)

    async def json(self):
        return await self._request.json()

    async def read(self, max_bytes: Optional[int] = None) -> bytes:
        if max_bytes is None:
            return await self._request.body()
        body = bytearray()
        async for chunk in self._request.stream():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise RequestBodyTooLarge()
        return bytes(body)

    async def post(self):
        form = await self._request.form()
        return {key: form.get(key) for key in form}

    async def multipart(self) -> _MultipartReader:
        form = await self._request.form()
        parts: list[_MultipartPart] = []
        for name, value in form.multi_items():
            if hasattr(value, "read") and hasattr(value, "filename"):
                parts.append(_MultipartPart(name, value.filename, value))
        return _MultipartReader(parts)


def convert_aiohttp_path(path: str) -> str:
    """Convert aiohttp path patterns to FastAPI/Starlette path syntax."""
    import re

    def _repl(match: re.Match) -> str:
        name = match.group(1)
        constraint = match.group(2)
        if constraint in (".*", ".+"):
            return "{" + name + ":path}"
        if "|" in constraint:
            return "{" + name + "}"
        return match.group(0)

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*):([^}]+)\}", _repl, path)


def wrap_handler(handler):
    """Adapt an aiohttp-style handler(request) for FastAPI/Starlette."""

    async def endpoint(request: Request):
        from .auth import enforce
        compat = CompatRequest(request)
        denied = await enforce(compat)
        if denied is not None:
            return denied
        return await handler(compat)

    endpoint.__name__ = getattr(handler, "__name__", "endpoint")
    endpoint.__doc__ = getattr(handler, "__doc__", None)
    return endpoint


class WSMsgType:
    TEXT = "TEXT"
    BINARY = "BINARY"
    CLOSE = "CLOSE"
    ERROR = "ERROR"
    PING = "PING"
    PONG = "PONG"


class WSMessage:
    def __init__(self, msg_type: str, data: Any = None):
        self.type = msg_type
        self.data = data


class WebSocketWrapper:
    """Make Starlette WebSocket iterable like aiohttp WebSocketResponse."""

    _RECEIVE_POLL_SECONDS = 0.5

    def __init__(self, websocket: WebSocket):
        self._ws = websocket
        self._closed = False
        self._exception: Optional[BaseException] = None

    @property
    def closed(self) -> bool:
        if self._closed:
            return True
        try:
            client = self._ws.client_state.name
            app_state = self._ws.application_state.name
            return client == "DISCONNECTED" or app_state == "DISCONNECTED"
        except Exception:
            return self._closed

    async def send_bytes(self, data: bytes) -> None:
        await self._ws.send_bytes(data)

    async def ping(self) -> None:
        # Starlette has no portable server ping API; activity watchdog still applies.
        return None

    async def close(self, code: int = 1000) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._ws.close(code=code)
        except Exception:
            pass

    def exception(self) -> Optional[BaseException]:
        return self._exception

    def __aiter__(self):
        return self

    async def __anext__(self) -> WSMessage:
        while True:
            if self.closed:
                self._closed = True
                raise StopAsyncIteration
            try:
                message = await asyncio.wait_for(
                    self._ws.receive(),
                    timeout=self._RECEIVE_POLL_SECONDS,
                )
            except asyncio.TimeoutError:
                continue
            except WebSocketDisconnect:
                self._closed = True
                return WSMessage(WSMsgType.CLOSE)
            except Exception as exc:
                self._exception = exc
                self._closed = True
                return WSMessage(WSMsgType.ERROR)

            msg_type = message.get("type")
            if msg_type == "websocket.disconnect":
                self._closed = True
                return WSMessage(WSMsgType.CLOSE)

            if msg_type == "websocket.receive":
                if message.get("bytes") is not None:
                    return WSMessage(WSMsgType.BINARY, message["bytes"])
                if message.get("text") is not None:
                    return WSMessage(WSMsgType.TEXT, message["text"])

            return WSMessage(WSMsgType.TEXT, None)


# Expose WSMsgType on web for `web.WSMsgType` compatibility in websocket.py
web.WSMsgType = WSMsgType
