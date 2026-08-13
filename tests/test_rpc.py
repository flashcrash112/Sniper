import base64

import pytest
from aiohttp import web

from sniper.rpc import RpcError, RpcPool


class StubServer:
    """An RPC endpoint that can be told to misbehave."""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.runner = None
        self.url = None

    async def start(self):
        app = web.Application()
        app.router.add_post("/", self._handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/"
        return self.url

    async def stop(self):
        await self.runner.cleanup()

    async def _handle(self, request):
        body = await request.json()
        self.requests.append(body)
        return self.handler(body)


@pytest.mark.asyncio
async def test_html_error_page_becomes_a_readable_error():
    """Rate-limit and proxy pages are HTML; the message must say so."""
    server = StubServer(
        lambda body: web.Response(
            status=429, text="<html><body>Too Many Requests</body></html>"
        )
    )
    url = await server.start()
    try:
        async with RpcPool(url, [url]) as pool:
            with pytest.raises(RpcError) as exc:
                await pool.call("getBalance", ["x"])
    finally:
        await server.stop()

    message = str(exc.value)
    assert "HTTP 429" in message
    assert "non-JSON" in message
    assert "Too Many Requests" in message


@pytest.mark.asyncio
async def test_jsonrpc_error_is_surfaced():
    server = StubServer(
        lambda body: web.json_response(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad params"}}
        )
    )
    url = await server.start()
    try:
        async with RpcPool(url, [url]) as pool:
            with pytest.raises(RpcError, match="bad params"):
                await pool.call("getBalance", ["x"])
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_send_falls_through_to_a_healthy_endpoint():
    """One dead endpoint must not cost us the buy."""
    good = StubServer(lambda body: web.json_response({"jsonrpc": "2.0", "id": 1, "result": "SIG"}))
    bad = StubServer(lambda body: web.Response(status=500, text="boom"))
    good_url, bad_url = await good.start(), await bad.start()

    try:
        async with RpcPool(good_url, [bad_url, good_url]) as pool:
            body = pool.build_send_body(b"\x01\x02\x03")
            winner, results = await pool.send_transaction(body)
    finally:
        await good.stop()
        await bad.stop()

    assert winner.ok
    assert winner.signature == "SIG"
    assert winner.endpoint == good_url
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_send_reports_failure_when_every_endpoint_is_down():
    bad = StubServer(lambda body: web.Response(status=500, text="boom"))
    url = await bad.start()
    try:
        async with RpcPool(url, [url]) as pool:
            winner, _ = await pool.send_transaction(pool.build_send_body(b"\x01"))
    finally:
        await bad.stop()

    assert not winner.ok
    assert winner.error


@pytest.mark.asyncio
async def test_send_body_is_valid_json_with_the_right_flags():
    import json

    pool = RpcPool("http://x", ["http://x"])
    raw = b"\xde\xad\xbe\xef"
    body = json.loads(pool.build_send_body(raw))

    assert body["method"] == "sendTransaction"
    assert base64.b64decode(body["params"][0]) == raw
    options = body["params"][1]
    assert options["encoding"] == "base64"
    assert options["skipPreflight"] is True
    # Retries are handled by broadcasting to several endpoints, not by asking
    # one node to sit on the transaction.
    assert options["maxRetries"] == 0
