"""Offline regressions for connection ownership; added by PUSCHi1337, 2026-10-29."""
import asyncio
import importlib
import json
from contextlib import asynccontextmanager

import pytest
import websockets

from pybose.BoseAuth import BoseAuth
from pybose.BoseSpeaker import BoseSpeaker

module = importlib.import_module("pybose.BoseSpeaker")
CLOSED = object()


class Socket:
    def __init__(self, fail_resource=None):
        self.incoming = asyncio.Queue()
        self.closed = False
        self.close_code = None
        self.fail_resource = fail_resource
        self.readers = 0
        self.peak_readers = 0
        self.sent = []

    async def send(self, data):
        message = json.loads(data)
        resource = message["header"]["resource"]
        if self.closed or resource == self.fail_resource:
            await self.close()
            raise websockets.ConnectionClosedOK(None, None)
        self.sent.append(resource)
        await self.incoming.put(json.dumps({
            "header": {
                "msgtype": "RESPONSE", "reqID": message["header"]["reqID"],
                "status": 200, "resource": resource,
            },
            "body": {"group": []} if resource == "/system/capabilities" else {"ok": True},
        }))

    async def recv(self):
        self.readers += 1
        self.peak_readers = max(self.peak_readers, self.readers)
        try:
            if self.readers > 1:
                raise RuntimeError("two receivers on one socket")
            item = await self.incoming.get()
            if item is CLOSED:
                raise websockets.ConnectionClosedOK(None, None)
            return item
        finally:
            self.readers -= 1

    async def close(self):
        self.closed = True
        self.close_code = 1000
        for _ in range(max(1, self.readers)):
            await self.incoming.put(CLOSED)


class Connector:
    def __init__(self):
        self.calls = 0
        self.sockets = []
        self.offline = False
        self.gate = None
        self.fail_resource = None

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        await asyncio.sleep(0)
        if self.offline:
            raise OSError("simulated offline speaker")
        socket = Socket(self.fail_resource)
        self.sockets.append(socket)
        return socket


@asynccontextmanager
async def rig(monkeypatch):
    auth = BoseAuth()
    auth.set_access_token("dummy", "dummy", "dummy")
    speaker = BoseSpeaker("offline.invalid", device_id="dummy", bose_auth=auth,
                          request_timeout=0.2)
    speaker.has_capability = lambda resource: True
    connector = Connector()
    monkeypatch.setattr(module.websockets, "connect", connector)
    try:
        yield speaker, connector
    finally:
        await asyncio.wait_for(speaker.disconnect(), 1)
        for socket in connector.sockets:
            await socket.close()
        await asyncio.sleep(0)


async def started(connector):
    async def wait():
        while connector.calls == 0:
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), 1)


@pytest.mark.asyncio
async def test_concurrent_connects_share_one_socket_and_receiver(monkeypatch):
    async with rig(monkeypatch) as (speaker, connector):
        results = await asyncio.wait_for(asyncio.gather(
            *(speaker.connect() for _ in range(20)), return_exceptions=True), 2)
        assert not [r for r in results if isinstance(r, BaseException)]
        assert connector.calls == 1
        assert max(s.peak_readers for s in connector.sockets) == 1
        assert speaker.is_connected()


@pytest.mark.asyncio
async def test_concurrent_requests_recover_a_closed_socket_once(monkeypatch):
    async with rig(monkeypatch) as (speaker, connector):
        speaker._websocket = Socket()
        await speaker._websocket.close()
        results = await asyncio.wait_for(asyncio.gather(*(
            speaker._request("/dummy", "GET", waitForResponse=False)
            for _ in range(20)), return_exceptions=True), 2)
        assert not [r for r in results if isinstance(r, BaseException)]
        assert connector.calls == 1
        assert connector.sockets[0].sent.count("/dummy") == 20
        assert connector.sockets[0].peak_readers == 1


@pytest.mark.asyncio
async def test_receiver_and_request_share_recovery(monkeypatch):
    async with rig(monkeypatch) as (speaker, connector):
        await speaker.connect()
        old = speaker._websocket
        await old.close()
        await asyncio.sleep(0)
        result = await asyncio.wait_for(
            speaker._request("/dummy", "GET", waitForResponse=False), 2)
        assert result == {}
        assert connector.calls == 2
        assert speaker.is_connected()
        assert all(s.peak_readers == 1 for s in connector.sockets)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["/system/capabilities", "/subscription"])
async def test_handshake_send_failure_does_not_reenter_connect(monkeypatch, resource):
    async with rig(monkeypatch) as (speaker, connector):
        connector.fail_resource = resource
        if resource == "/subscription":
            speaker._subscribed_resources = ["/dummy"]
        result = (await asyncio.gather(
            asyncio.wait_for(speaker.connect(), 0.8), return_exceptions=True))[0]
        assert isinstance(result, websockets.ConnectionClosed)
        assert connector.calls == 1
        assert all(s.closed and s.readers == 0 for s in connector.sockets)
        assert not speaker.is_connected()


@pytest.mark.asyncio
async def test_offline_attempts_have_exponential_capped_cooldown(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0], raising=False)
    async with rig(monkeypatch) as (speaker, connector):
        connector.offline = True
        delay = 1
        for expected_calls in range(1, 10):
            with pytest.raises(OSError, match="simulated offline"):
                await speaker.connect()
            assert connector.calls == expected_calls
            for _ in range(10):
                with pytest.raises(ConnectionError):
                    await speaker.connect()
            assert connector.calls == expected_calls
            clock[0] += delay - 0.01
            with pytest.raises(ConnectionError):
                await speaker.connect()
            assert connector.calls == expected_calls
            clock[0] += 0.01
            delay = min(delay * 2, 60)


@pytest.mark.asyncio
async def test_success_resets_the_offline_cooldown(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0], raising=False)
    async with rig(monkeypatch) as (speaker, connector):
        connector.offline = True
        with pytest.raises(OSError):
            await speaker.connect()
        clock[0] += 1
        connector.offline = False
        await speaker.connect()
        await speaker.disconnect()
        connector.offline = True
        with pytest.raises(OSError):
            await speaker.connect()
        clock[0] += 1
        connector.offline = False
        await speaker.connect()
        assert connector.calls == 4


@pytest.mark.asyncio
async def test_background_reconnect_failure_is_observed_once(monkeypatch):
    async with rig(monkeypatch) as (speaker, connector):
        failures = []
        speaker._on_exception = failures.append
        await speaker.connect()
        connector.offline = True
        await speaker._websocket.close()
        await asyncio.sleep(0.05)
        assert connector.calls == 2
        assert len(failures) == 1
        assert isinstance(failures[0], OSError)
        assert not speaker.is_connected()


@pytest.mark.asyncio
async def test_auto_reconnect_disabled_reports_disconnected(monkeypatch):
    async with rig(monkeypatch) as (speaker, connector):
        speaker._auto_reconnect = False
        await speaker.connect()
        await speaker._websocket.close()
        await asyncio.sleep(0.05)
        assert connector.calls == 1
        assert not speaker.is_connected()


@pytest.mark.asyncio
async def test_disconnect_cancels_an_inflight_connection(monkeypatch):
    async with rig(monkeypatch) as (speaker, connector):
        connector.gate = asyncio.Event()
        waiter = asyncio.create_task(speaker.connect())
        await started(connector)
        await speaker.disconnect()
        connector.gate.set()
        result = (await asyncio.gather(waiter, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
        assert not speaker.is_connected()
        assert not connector.sockets


@pytest.mark.asyncio
async def test_disconnect_does_not_reconnect_and_closes_receiver(monkeypatch):
    async with rig(monkeypatch) as (speaker, connector):
        await speaker.connect()
        socket = speaker._websocket
        receiver = speaker._receiver_task
        await speaker.disconnect()
        await asyncio.sleep(0.05)
        assert connector.calls == 1
        assert receiver.done()
        assert socket.closed and socket.readers == 0
        assert not speaker.is_connected()


@pytest.mark.asyncio
async def test_connect_on_a_ready_connection_is_idempotent(monkeypatch):
    async with rig(monkeypatch) as (speaker, connector):
        await speaker.connect()
        socket = speaker._websocket
        await speaker.connect()
        assert connector.calls == 1
        assert speaker._websocket is socket
        assert not socket.closed


@pytest.mark.asyncio
async def test_cancelling_one_waiter_preserves_the_shared_connection(monkeypatch):
    async with rig(monkeypatch) as (speaker, connector):
        connector.gate = asyncio.Event()
        first = asyncio.create_task(speaker.connect())
        await started(connector)
        second = asyncio.create_task(speaker.connect())
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        connector.gate.set()
        await asyncio.wait_for(second, 1)
        assert connector.calls == 1
        assert speaker.is_connected()


@pytest.mark.asyncio
async def test_request_waits_for_handshake_before_using_socket(monkeypatch):
    async with rig(monkeypatch) as (speaker, connector):
        entered = asyncio.Event()
        release = asyncio.Event()
        original = speaker.get_capabilities

        async def delayed_capabilities():
            entered.set()
            await release.wait()
            return await original()

        monkeypatch.setattr(speaker, "get_capabilities", delayed_capabilities)
        connecting = asyncio.create_task(speaker.connect())
        await asyncio.wait_for(entered.wait(), 1)
        request = asyncio.create_task(speaker._request("/dummy", "GET",
                                                      waitForResponse=False))
        await asyncio.sleep(0.02)
        assert "/dummy" not in connector.sockets[0].sent
        release.set()
        await asyncio.wait_for(asyncio.gather(connecting, request), 1)
        assert connector.calls == 1
        assert "/dummy" in connector.sockets[0].sent


@pytest.mark.asyncio
async def test_second_send_failure_is_not_retried_forever(monkeypatch):
    async with rig(monkeypatch) as (speaker, connector):
        speaker._websocket = Socket()
        await speaker._websocket.close()
        connector.fail_resource = "/dummy"
        with pytest.raises(websockets.ConnectionClosed):
            await asyncio.wait_for(speaker._request("/dummy", "GET",
                                                   waitForResponse=False), 1)
        assert connector.calls == 1
        assert not speaker.is_connected()


@pytest.mark.asyncio
async def test_stale_receiver_cannot_update_the_replacement_connection(monkeypatch):
    async with rig(monkeypatch) as (speaker, connector):
        old = Socket()
        speaker._websocket = old
        messages = []
        speaker.attach_receiver(messages.append)
        receiver = asyncio.create_task(speaker._receiver_loop())
        await asyncio.sleep(0)
        replacement = Socket()
        speaker._websocket = replacement
        speaker._connected = False
        await old.incoming.put(json.dumps({"header": {}, "body": {"stale": True}}))
        await old.close()
        await asyncio.wait_for(receiver, 0.5)
        assert not speaker.is_connected()
        assert messages == []
        assert connector.calls == 0
