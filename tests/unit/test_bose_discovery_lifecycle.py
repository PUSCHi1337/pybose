"""Discovery lifecycle regressions using the real module and fake network objects."""

import gc
import importlib
import socket
from types import SimpleNamespace
from weakref import WeakSet

import pytest
from pytest_socket import SocketBlockedError
from zeroconf import ServiceStateChange


discovery_module = importlib.import_module("pybose.BoseDiscovery")
pytestmark = pytest.mark.usefixtures("socket_disabled")


@pytest.fixture
def discovery_runtime(monkeypatch):
    """Model browser registrations and HA's shared Zeroconf without any sockets."""
    state = SimpleNamespace(
        active_browsers=set(),
        browsers_started=0,
        browsers_cancelled=0,
        events=[],
        waits=[],
        publish_device=True,
        start_error=None,
        wait_error=None,
        cancel_error=None,
        owned_context=None,
    )

    class ServiceInfo:
        properties = {b"GUID": b"fake-guid"}

        def parsed_addresses(self):
            return ["198.51.100.42"]

    class Context:
        def __init__(self, shared=False):
            self.shared = shared
            self.close_calls = 0
            self.closed = False

        def get_service_info(self, service_type, name):
            assert not self.closed
            assert service_type == "_bose-passport._tcp.local."
            return ServiceInfo()

        def close(self):
            self.close_calls += 1
            state.events.append("zeroconf.close")
            # HaZeroconf.close deliberately does nothing: its owner is HA.
            if not self.shared:
                self.closed = True

    class Browser:
        def __init__(self, context, service_type, handlers):
            if state.start_error is not None:
                raise state.start_error
            self.handlers = list(handlers)
            state.browsers_started += 1
            state.active_browsers.add(self)
            state.events.append("browser.start")
            if state.publish_device:
                for handler in self.handlers:
                    handler(context, service_type, "FakeService", ServiceStateChange.Added)

        def cancel(self):
            state.browsers_cancelled += 1
            state.events.append("browser.cancel")
            state.active_browsers.remove(self)
            self.handlers.clear()
            if state.cancel_error is not None:
                raise state.cancel_error

    def create_context():
        state.owned_context = Context()
        return state.owned_context

    def wait(timeout):
        state.waits.append(timeout)
        if state.wait_error is not None:
            raise state.wait_error

    state.shared_context = Context(shared=True)
    monkeypatch.setattr(discovery_module, "Zeroconf", create_context)
    monkeypatch.setattr(discovery_module, "ServiceBrowser", Browser)
    monkeypatch.setattr(discovery_module.time, "sleep", wait)
    return state


def make_discovery(state, owned):
    if owned:
        return discovery_module.BoseDiscovery()
    return discovery_module.BoseDiscovery(zeroconf=state.shared_context)


@pytest.mark.parametrize("owned", [False, True], ids=["shared", "owned"])
@pytest.mark.parametrize("interrupted", [False, True], ids=["completed", "interrupted"])
def test_scan_releases_browser_and_only_closes_its_own_context(
    discovery_runtime, owned, interrupted
):
    state = discovery_runtime
    discovery = make_discovery(state, owned)
    context = state.owned_context if owned else state.shared_context
    if interrupted:
        state.wait_error = RuntimeError("scan interrupted")
        with pytest.raises(RuntimeError, match="scan interrupted"):
            discovery.discover_devices(timeout=7)
    else:
        assert discovery.discover_devices(timeout=7) == [
            {"GUID": "fake-guid", "IP": "198.51.100.42"}
        ]
    assert state.waits == [7]
    assert state.browsers_cancelled == 1
    assert len(state.active_browsers) == 0
    assert context.close_calls == int(owned)
    assert context.closed is owned
    if owned:
        assert state.events[-2:] == ["browser.cancel", "zeroconf.close"]


@pytest.mark.parametrize("owned", [False, True], ids=["shared", "owned"])
def test_browser_start_failure_still_releases_owned_context(discovery_runtime, owned):
    state = discovery_runtime
    discovery = make_discovery(state, owned)
    context = state.owned_context if owned else state.shared_context
    state.start_error = RuntimeError("browser start failed")
    with pytest.raises(RuntimeError, match="browser start failed"):
        discovery.discover_devices(timeout=0)
    assert state.browsers_started == 0
    assert state.browsers_cancelled == 0
    assert context.close_calls == int(owned)
    assert context.closed is owned


@pytest.mark.parametrize("owned", [False, True], ids=["shared", "owned"])
def test_cancel_failure_does_not_skip_owned_context_cleanup(discovery_runtime, owned):
    state = discovery_runtime
    discovery = make_discovery(state, owned)
    context = state.owned_context if owned else state.shared_context
    state.cancel_error = RuntimeError("browser cancellation failed")
    with pytest.raises(RuntimeError, match="browser cancellation failed"):
        discovery.discover_devices(timeout=0)
    assert state.browsers_cancelled == 1
    assert context.close_calls == int(owned)
    assert context.closed is owned


def test_repeated_scans_release_browser_registrations_and_discovery_objects(
    discovery_runtime,
):
    state = discovery_runtime
    instances = WeakSet()
    for _ in range(500):
        discovery = make_discovery(state, owned=False)
        instances.add(discovery)
        discovery.discover_devices(timeout=0)
    del discovery
    gc.collect()
    assert state.browsers_started == 500
    assert state.browsers_cancelled == 500
    assert len(state.active_browsers) == 0
    assert len(instances) == 0
    assert state.shared_context.close_calls == 0


def test_empty_scan_resets_previous_results_without_closing_shared_context(
    discovery_runtime,
):
    state = discovery_runtime
    discovery = make_discovery(state, owned=False)
    first_result = discovery.discover_devices(timeout=0)
    state.publish_device = False
    assert discovery.discover_devices(timeout=0) == []
    assert first_result == [{"GUID": "fake-guid", "IP": "198.51.100.42"}]
    assert state.shared_context.closed is False


def test_lifecycle_tests_cannot_create_network_sockets():
    with pytest.warns(UserWarning, match=r"socket\.socket"), pytest.raises(SocketBlockedError):
        socket.socket()
