from types import SimpleNamespace

import pytest

from testrift_server import http_compat


def make_request(stream):
    return http_compat.CompatRequest(SimpleNamespace(
        stream=stream, path_params={}, query_params={}, headers={},
        method="POST", url=SimpleNamespace(path="/", scheme="http"), cookies={},
    ))


@pytest.mark.asyncio
async def test_bounded_reader_rejects_chunk_before_copying(monkeypatch):
    copied = []

    class TrackedBuffer(bytearray):
        def extend(self, chunk):
            copied.append(chunk)
            super().extend(chunk)

    async def stream():
        yield b"ab"
        yield b"123456789"

    monkeypatch.setattr(http_compat, "bytearray", TrackedBuffer, raising=False)
    request = make_request(stream)
    with pytest.raises(http_compat.RequestBodyTooLarge):
        await request.read(max_bytes=4)
    assert copied == [b"ab"]


@pytest.mark.asyncio
async def test_bounded_reader_accepts_exact_limit():
    async def stream():
        yield b"ab"
        yield b"cd"

    request = make_request(stream)
    assert await request.read(max_bytes=4) == b"abcd"