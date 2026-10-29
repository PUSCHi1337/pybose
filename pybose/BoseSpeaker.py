"""
Bose Speaker control using its WebSocket (like the BOSE app)

In order to control the device locally, you need to obtain the control token and device ID.
The control token is acquired from the online BOSE API (using BoseAuth.py), while the device ID
is obtained by discovering the device on the local network (using BoseDiscovery.py).

Note: The token is only valid for a limited time and does not renew automatically.
You may need to refetch the token periodically.
"""

from __future__ import annotations
import json
import asyncio
import logging
from ssl import SSLContext, CERT_NONE, PROTOCOL_TLS_CLIENT
import websockets
from threading import Event
from time import monotonic
from typing import Any, Callable, Dict, List, Optional, Union
from pybose.BoseAuth import BoseAuth

from . import BoseResponse as BR

_LOGGER = logging.getLogger(__name__)

# Default resources subscribed when connecting (same as in the BOSE app)
DEFAULT_SUBSCRIBE_RESOURCES: List[str] = [
    "/bluetooth/sink/list",
    "/system/power/control",
    "/audio/avSync",
    "/bluetooth/source/list",
    "/audio/bass",
    "/device/assumed/TVs",
    "/network/wifi/siteScan",
    "/system/update/start",
    "/system/update/status",
    "/bluetooth/sink/macAddr",
    "/content/nowPlaying/rating",
    "/bluetooth/source/stopScan",
    "/system/setup",
    "/homekit/info",
    "/bluetooth/source/pairStatus",
    "/device/configuredDevices",
    "/bluetooth/source/status",
    "/cast/teardown",
    "/bluetooth/sink/status",
    "/cast/setup",
    "/cec",
    "/cloudSync",
    "/system/challenge",
    "/bluetooth/sink/remove",
    "/bluetooth/source/connect",
    "/remote/integration/brandList",
    "/subscription",
    "/network/status",
    "/bluetooth/source/scanResult",
    "/content/playbackRequest",
    "/audio/eqSelect",
    "/audio/height",
    "/content/transportControl",
    "/grouping/activeGroups",
    "/audio/mode",
    "/bluetooth/source/pair",
    "/bluetooth/source/capability",
    "/bluetooth/source/disconnect",
    "/audio/subwooferGain",
    "/voice/setup/start",
    "/audio/center",
    "/network/wifi/status",
    "/content/nowPlaying/repeat",
    "/system/sources",
    "/content/nowPlaying",
    "/system/power/macro",
    "/bluetooth/sink/pairable",
    "/network/wifi/profile",
    "/cast/settings",
    "/audio/zone",
    "/content/nowPlaying/shuffle",
    "/bluetooth/source/capabilitySettings",
    "/remote/integration",
    "/audio/surround",
    "/accessories",
    "/audio/treble",
    "/adaptiq",
    "/accessories/playTones",
    "/system/power/timeouts",
    "/audio/dualMonoSelect",
    "/system/info",
    "/system/sources/status",
    "/audio/rebroadcastLatency/mode",
    "/audio/format",
    "/bluetooth/source/connectionStatus",
    "/system/power/mode/opticalAutoWake",
    "/content/nowPlaying/favorite",
    "/system/productSettings",
    "/bluetooth/sink/connectionStatus",
    "/bluetooth/source/remove",
    "/audio/autoVolume",
    "/system/capabilities",
    "/audio/volume/increment",
    "/bluetooth/sink/connect",
    "/bluetooth/source/volume",
    "/bluetooth/sink/disconnect",
    "/system/reset",
    "/audio/volume/decrement",
    "/audio/volume",
    "/remote/integration/directEntry",
    "/device/configure",
    "/device/setup",
    "/bluetooth/source/scan",
    "/voice/settings",
    "/system/activated",
]


# A speaker whose clock runs slightly behind rejects a freshly issued token
# with "JWT not before check failed" until its clock passes the token's nbf
# claim, so such requests are retried for a few seconds before giving up.
JWT_NOT_BEFORE_RETRIES = 3
JWT_NOT_BEFORE_RETRY_DELAY = 2

# A speaker simply stays quiet about requests it cannot answer (offline, in
# standby, or asked for a resource it does not implement), so every request
# stops waiting for its response after this many seconds instead of forever.
DEFAULT_REQUEST_TIMEOUT = 30

# Fork change by PUSCHi1337, 2026-10-29: bound retries per speaker instance.
RECONNECT_INITIAL_DELAY = 1
RECONNECT_MAX_DELAY = 60


class BoseSpeaker:
    def __init__(
        self,
        host: str,
        device_id: Optional[str] = None,
        version: int = 1,
        auto_reconnect: bool = True,
        bose_auth: BoseAuth = None,
        on_exception: Optional[Callable[[Exception], None]] = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self._device_id: Optional[str] = device_id
        self._request_timeout: float = request_timeout
        self._host: str = host
        self._version: int = version
        self._websocket: Optional[websockets.connect] = None
        self._ssl_context: SSLContext = SSLContext(PROTOCOL_TLS_CLIENT)
        self._ssl_context.check_hostname = False
        self._ssl_context.verify_mode = CERT_NONE
        self._subprotocol: str = "eco2"
        self._req_id: int = 1
        self._url: str = f"wss://{self._host}:8082/?product=Madrid-iOS:31019F02-F01F-4E73-B495-B96D33AD3664"
        self._responses: List[BR.BoseMessage] = []
        self._stop_event: Event = Event()
        self._receiver_task: Optional[asyncio.Task] = None
        self._receivers: Dict[int, Callable[[BR.BoseMessage], None]] = {}
        self._subscribed_resources: List[str] = []
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._capabilities: Optional[BR.Capabilities] = None
        self._auto_reconnect = auto_reconnect
        self._bose_auth: BoseAuth = bose_auth
        self._on_exception = on_exception
        self._connected = False
        self._connect_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._disconnect_lock = asyncio.Lock()
        self._address_lock = asyncio.Lock()
        self._disconnecting = False
        self._retry_at = 0.0
        self._reconnect_delay = RECONNECT_INITIAL_DELAY

    async def connect(self) -> None:
        """Share one connection handshake across concurrent callers."""
        if self._disconnecting:
            raise ConnectionError("Speaker disconnect is in progress.")
        task = self._connect_task
        if task is None or task.done():
            if self._connected:
                return
            retry_after = self._retry_at - monotonic()
            if retry_after > 0:
                raise BoseConnectionBackoffError(retry_after)
            self._stop_event.clear()
            task = asyncio.create_task(self._connect_once())
            self._connect_task = task
            task.add_done_callback(self._observe_task)
        # A cancelled polling caller must not cancel another caller's handshake.
        await asyncio.shield(task)

    async def reconnect(self, host: Optional[str] = None) -> None:
        """Reconnect without replacing the client or its registered callbacks.

        A changed address closes the previous transport first. At the same
        address, concurrent attempts and retry cooldown remain owned by connect().
        """
        async with self._address_lock:
            if host is not None and host != self._host:
                await self.disconnect()
                self._host = host
                self._url = (
                    f"wss://{self._host}:8082/"
                    "?product=Madrid-iOS:31019F02-F01F-4E73-B495-B96D33AD3664"
                )
            await self.connect()

    @staticmethod
    def _observe_task(task: asyncio.Task) -> None:
        """Retrieve failures even when every waiting caller has been cancelled."""
        if not task.cancelled():
            task.exception()

    async def _connect_once(self) -> None:
        """Open one socket; handshake requests must never reenter connect()."""
        try:
            await self._close_connection()
            self._websocket = await websockets.connect(
                self._url, subprotocols=[self._subprotocol], ssl=self._ssl_context
            )
            self._receiver_task = asyncio.create_task(self._receiver_loop())
            if self._subscribed_resources:
                await self.subscribe(self._subscribed_resources)
            await self.get_capabilities()
            if self._receiver_task.done():
                raise ConnectionError("Speaker disconnected during the handshake.")
            self._connected = True
            self._retry_at = 0.0
            self._reconnect_delay = RECONNECT_INITIAL_DELAY
            _LOGGER.info("WebSocket connection established.")
        except asyncio.CancelledError:
            await self._close_connection()
            raise
        except Exception:
            self._retry_at = monotonic() + self._reconnect_delay
            self._reconnect_delay = min(
                self._reconnect_delay * 2, RECONNECT_MAX_DELAY
            )
            await self._close_connection()
            raise

    async def _close_connection(self) -> None:
        """Close the owned transport and await its sole receiver."""
        self._connected = False
        receiver = self._receiver_task
        current = asyncio.current_task()
        if receiver and receiver is not current and not receiver.done():
            receiver.cancel()
        try:
            if self._websocket:
                await self._websocket.close()
        finally:
            if receiver and receiver is not current:
                await asyncio.gather(receiver, return_exceptions=True)

    async def disconnect(self) -> None:
        """Cancel pending recovery and close the receiver and transport."""
        async with self._disconnect_lock:
            self._disconnecting = True
            self._stop_event.set()
            try:
                current = asyncio.current_task()
                pending = [
                    task for task in (self._reconnect_task, self._connect_task)
                    if task and task is not current and not task.done()
                ]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await self._close_connection()
                _LOGGER.info("WebSocket connection closed.")
            finally:
                self._disconnecting = False

    def _schedule_reconnect(self) -> None:
        """Schedule at most one observed recovery attempt after receiver exit."""
        if (not self._auto_reconnect or self._stop_event.is_set()
                or self._disconnecting
                or (self._connect_task and not self._connect_task.done())
                or (self._reconnect_task and not self._reconnect_task.done())):
            return
        self._reconnect_task = asyncio.create_task(self._reconnect())
        self._reconnect_task.add_done_callback(self._observe_task)

    async def _reconnect(self) -> None:
        try:
            await self.connect()
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            _LOGGER.warning("Automatic speaker reconnect failed: %s", ex)
            if self._on_exception:
                self._on_exception(ex)

    def attach_receiver(self, callback: Callable[[BR.BoseMessage], None]) -> int:
        """Attach a callback to receive unsolicited messages."""
        receiver_id = max(self._receivers.keys(), default=0) + 1
        self._receivers[receiver_id] = callback
        return receiver_id

    def detach_receiver(self, receiver_id: int) -> None:
        """Detach a previously attached receiver."""
        self._receivers.pop(receiver_id, None)

    async def _request(
        self,
        resource: str,
        method: str,
        body: Optional[Dict[str, Any]] = None,
        withHeaders: bool = False,
        waitForResponse: bool = True,
        version: Optional[int] = None,
        checkCapabilities: bool = True,
        timeout: Optional[float] = None,
        _attempt: int = 0,
    ) -> Dict[str, Any]:
        """Send a request over the WebSocket and wait for the matching response."""
        if body is None:
            body = {}

        token: str = self._bose_auth.getCachedToken().get("access_token")
        req_id: int = self._req_id
        self._req_id += 1

        version = version if version is not None else self._version

        if checkCapabilities and not self.has_capability(resource):
            ex = BoseFunctionNotSupportedException(
                f"Resource {resource} is not supported by the device."
            )
            if self._on_exception:
                self._on_exception(ex)
            raise ex

        header: BR.BoseHeader = {
            "device": self._device_id,
            "method": method,
            "msgtype": "REQUEST",
            "reqID": req_id,
            "resource": resource,
            "status": 200,
            "token": token,
            "version": version,
        }
        message: BR.BoseMessage = {"body": body, "header": header}

        if self._device_id is None:
            await self._message_queue.put(message)
            _LOGGER.debug(
                f"Waiting for deviceID. Queued message: {json.dumps(message, indent=4)}"
            )
        else:
            in_handshake = asyncio.current_task() is self._connect_task
            if (not in_handshake and self._connect_task
                    and not self._connect_task.done()):
                await self.connect()
            if self._websocket is None:
                if in_handshake or not self._auto_reconnect or self._stop_event.is_set():
                    raise ConnectionError("Speaker is disconnected.")
                await self.connect()
            # Retry a failed send once, sharing recovery with every other caller.
            for attempt in range(2):
                socket = self._websocket
                try:
                    await socket.send(json.dumps(message))
                    break
                except websockets.ConnectionClosed as ex:
                    if socket is self._websocket:
                        self._connected = False
                    if (in_handshake or attempt or not self._auto_reconnect
                            or self._stop_event.is_set()):
                        if not self._auto_reconnect and self._on_exception:
                            self._on_exception(ex)
                        raise
                    await self.connect()

            _LOGGER.debug(f"Sent message: {json.dumps(message, indent=4)}")

        if not waitForResponse:
            return {}

        if timeout is None:
            timeout = self._request_timeout
        deadline = asyncio.get_running_loop().time() + timeout

        while True:
            for response in self._responses:
                resp_header = response.get("header", {})
                if (
                    resp_header.get("msgtype") == BR.BoseHeaderMsgTypeEnum.RESPONSE
                    and resp_header.get("reqID") == req_id
                ):
                    self._responses.remove(response)
                    status = resp_header.get("status")
                    if status is None:
                        ex = BoseRequestException(
                            method,
                            resource,
                            body,
                            999,
                            999,
                            f"pybose could not parse the message: {response}",
                        )
                        if self._on_exception:
                            self._on_exception(ex)
                        raise ex
                    if status != 200:
                        error = response.get(
                            "error",
                            {
                                "status": 999,
                                "message": f"pybose could not determine the error, but status code is {status}",
                            },
                        )
                        if (
                            status == 401
                            and _attempt < JWT_NOT_BEFORE_RETRIES
                            and "not before" in str(error.get("message", "")).lower()
                        ):
                            # Expected and handled; only the final failure is raised.
                            _LOGGER.debug(
                                "Speaker rejected the token for '%s %s' as not yet valid (clock skew), retrying in %s seconds",
                                method,
                                resource,
                                JWT_NOT_BEFORE_RETRY_DELAY,
                            )
                            await asyncio.sleep(JWT_NOT_BEFORE_RETRY_DELAY)
                            return await self._request(
                                resource,
                                method,
                                body,
                                withHeaders,
                                waitForResponse,
                                version,
                                checkCapabilities,
                                timeout=timeout,
                                _attempt=_attempt + 1,
                            )
                        ex = BoseRequestException(
                            method,
                            resource,
                            body,
                            status,
                            error["code"],
                            error["message"],
                        )
                        if self._on_exception:
                            self._on_exception(ex)
                        raise ex
                    return response["body"] if not withHeaders else response
            if asyncio.get_running_loop().time() >= deadline:
                ex = BoseRequestException(
                    method,
                    resource,
                    body,
                    408,
                    408,
                    f"no response from the speaker within {timeout} seconds",
                )
                if self._on_exception:
                    self._on_exception(ex)
                raise ex
            await asyncio.sleep(0.1)

    async def _receiver_loop(self) -> None:
        """Continuously receive and process messages from the WebSocket."""
        socket = self._websocket
        try:
            while not self._stop_event.is_set():
                message = await socket.recv()
                if socket is not self._websocket:
                    return
                _LOGGER.debug(f"Received message: {message}")
                parsed_message: BR.BoseMessage = json.loads(message)
                header: BR.BoseHeader = parsed_message.get("header", {})
                if header.get("device") is not None and self._device_id is None:
                    self._device_id = header["device"]
                    _LOGGER.debug(
                        f"Received first message from device. Device ID: {self._device_id}"
                    )
                    _LOGGER.debug(
                        f"Sending {self._message_queue.qsize()} queued messages."
                    )
                    while not self._message_queue.empty():
                        queued_msg = await self._message_queue.get()
                        queued_msg["header"]["device"] = self._device_id
                        await socket.send(json.dumps(queued_msg))
                if (
                    header.get("msgtype") == BR.BoseHeaderMsgTypeEnum.RESPONSE
                    and header.get("reqID") is not None
                ):
                    _LOGGER.debug(f"Response received for reqID: {header['reqID']}")
                    self._responses.append(parsed_message)
                else:
                    for receiver in self._receivers.values():
                        receiver(parsed_message)
        except websockets.ConnectionClosed:
            if self._stop_event.is_set() or socket is not self._websocket:
                return
            self._connected = False
            _LOGGER.warning("WebSocket connection lost.")
            self._schedule_reconnect()
        except Exception as e:
            if not self._stop_event.is_set() and socket is self._websocket:
                self._connected = False
                _LOGGER.error("Error in receiver loop: %s", e)
                self._schedule_reconnect()

    async def get_capabilities(self) -> BR.Capabilities:
        """Retrieve the device capabilities."""
        self._capabilities = BR.Capabilities(
            await self._request("/system/capabilities", "GET", checkCapabilities=False)
        )
        return self._capabilities

    def has_capability(self, endpoint: str) -> bool:
        """Return True if the device has the specified capability."""
        if self._capabilities is None:
            ex = BoseCapabilitiesNotLoadedException()
            if self._on_exception:
                self._on_exception(ex)
            raise ex
        groups: List[BR.CapabilityGroup] = self._capabilities.get("group", [])
        endpoints: List[str] = [
            ep.get("endpoint") for group in groups for ep in group.get("endpoints", [])
        ]
        return endpoint in endpoints

    async def get_system_info(self) -> BR.SystemInfo:
        """Retrieve system information."""
        return BR.SystemInfo(await self._request("/system/info", "GET"))

    async def get_audio_volume(self) -> BR.AudioVolume:
        """Retrieve the current audio volume."""
        return BR.AudioVolume(await self._request("/audio/volume", "GET"))

    async def set_audio_volume(self, volume: int) -> BR.AudioVolume:
        """Set the audio volume to the specified value."""
        body = {"value": volume}
        return BR.AudioVolume(await self._request("/audio/volume", "PUT", body))

    async def set_audio_volume_muted(self, muted: bool) -> BR.AudioVolume:
        """Set the audio volume muted state."""
        body = {"muted": muted}
        return BR.AudioVolume(await self._request("/audio/volume", "PUT", body))

    async def get_now_playing(self) -> BR.ContentNowPlaying:
        """Retrieve the currently playing content."""
        return BR.ContentNowPlaying(await self._request("/content/nowPlaying", "GET"))

    async def get_bluetooth_source_status(self) -> BR.BluetoothSourceStatus:
        """Retrieve Bluetooth source status."""
        return await self._request("/bluetooth/source/status", "GET")  # type: ignore

    async def get_bluetooth_sink_status(self) -> BR.BluetoothSinkStatus:
        """Retrieve Bluetooth sink status."""
        return await self._request("/bluetooth/sink/status", "GET")  # type: ignore

    async def get_bluetooth_sink_list(self) -> BR.BluetoothSinkList:
        """Retrieve list of Bluetooth sink devices."""
        return await self._request("/bluetooth/sink/list", "GET")  # type: ignore

    async def set_bluetooth_sink_pairable(self) -> None:
        """Make the device pairable for Bluetooth connections."""
        await self._request("/bluetooth/sink/pairable", "POST", withHeaders=True)

    async def connect_bluetooth_sink_device(self, mac_address: str) -> None:
        """Connect to a specific Bluetooth sink device by MAC address."""
        body = {"mac": mac_address}
        await self._request("/bluetooth/sink/connect", "POST", body, withHeaders=True)

    async def disconnect_bluetooth_sink_device(self, mac_address: str) -> None:
        """Disconnect from a specific Bluetooth sink device by MAC address."""
        body = {"mac": mac_address}
        await self._request(
            "/bluetooth/sink/disconnect", "POST", body, withHeaders=True
        )

    async def remove_bluetooth_sink_device(self, mac_address: str) -> None:
        """Remove (unpair) a specific Bluetooth sink device by MAC address."""
        body = {"mac": mac_address}
        await self._request("/bluetooth/sink/remove", "POST", body, withHeaders=True)

    async def get_power_state(self) -> BR.SystemPowerControl:
        """Retrieve the power state of the device."""
        return await self._request("/system/power/control", "GET")

    async def set_power_state(self, state: bool) -> None:
        """Set the device power state."""
        body = {"power": "ON" if state else "OFF"}
        await self._request("/system/power/control", "POST", body)

    async def _control_transport(self, control: str) -> BR.ContentNowPlaying:
        """Send a transport control command."""
        body = {"state": control}
        return BR.ContentNowPlaying(
            await self._request("/content/transportControl", "PUT", body)
        )

    async def pause(self) -> BR.ContentNowPlaying:
        """Pause playback."""
        return await self._control_transport("PAUSE")

    async def play(self) -> BR.ContentNowPlaying:
        """Resume playback."""
        return await self._control_transport("PLAY")

    async def skip_next(self) -> BR.ContentNowPlaying:
        """Skip to the next item."""
        return await self._control_transport("SKIPNEXT")

    async def skip_previous(self) -> BR.ContentNowPlaying:
        """Skip to the previous item."""
        return await self._control_transport("SKIPPREVIOUS")

    async def seek(self, position: Union[float, int]) -> BR.ContentNowPlaying:
        """Seek to a specified position (in seconds)."""
        body = {"position": position, "state": "SEEK"}
        return await self._request("/content/transportControl", "PUT", body)

    async def request_playback_preset(
        self, preset: BR.Preset, initiator_id: str
    ) -> bool:
        """Request a playback preset."""
        content_item = preset["actions"][0]["payload"]["contentItem"]
        body = {
            "source": content_item.get("source"),
            "initiatorID": initiator_id,
            "sourceAccount": content_item.get("sourceAccount"),
            "preset": {
                "location": content_item.get("location"),
                "name": content_item.get("name"),
                "containerArt": content_item.get("containerArt"),
                "presetable": content_item.get("presetable"),
                "type": content_item.get("type"),
            },
        }
        return await self._request("/content/playbackRequest", "POST", body)

    def get_device_id(self) -> Optional[str]:
        """Return the device ID (if available)."""
        return self._device_id

    async def subscribe(
        self, resources: List[str] = DEFAULT_SUBSCRIBE_RESOURCES
    ) -> dict:
        """Subscribe to a list of resources."""
        body = {
            "notifications": [
                {"resource": resource, "version": 1} for resource in resources
            ]
        }
        self._subscribed_resources = resources
        return await self._request("/subscription", "PUT", body, version=2)

    async def switch_tv_source(self) -> BR.ContentNowPlaying:
        """Switch the speaker’s source to TV."""
        return await self.set_source("PRODUCT", "TV")

    async def set_source(self, source: str, sourceAccount: str) -> BR.ContentNowPlaying:
        """Set the playback source."""
        body = {"source": source, "sourceAccount": sourceAccount}
        return BR.ContentNowPlaying(
            await self._request("/content/playbackRequest", "POST", body)
        )

    async def get_sources(self) -> BR.Sources:
        """Retrieve available sources."""
        return BR.Sources(await self._request("/system/sources", "GET"))

    async def get_audio_setting(self, option: str) -> BR.Audio:
        """Retrieve an audio setting value."""
        if option not in [
            "bass",
            "treble",
            "center",
            "subwooferGain",
            "surround",
            "height",
            "avSync",
        ]:
            ex = BoseInvalidAudioSettingException(option)
            if self._on_exception:
                self._on_exception(ex)
            raise ex
        return BR.Audio(await self._request("/audio/" + option, "GET"))

    async def set_audio_setting(self, option: str, value: int) -> BR.Audio:
        """Set an audio setting value."""
        if option not in [
            "bass",
            "treble",
            "center",
            "subwooferGain",
            "surround",
            "height",
            "avSync",
        ]:
            ex = BoseInvalidAudioSettingException(option)
            if self._on_exception:
                self._on_exception(ex)
            raise ex
        return BR.Audio(
            await self._request("/audio/" + option, "POST", {"value": value})
        )

    async def get_accessories(self) -> BR.Accessories:
        """Retrieve accessories information."""
        return BR.Accessories(await self._request("/accessories", "GET"))

    async def put_accessories(
        self, subs_enabled: Optional[bool] = None, rears_enabled: Optional[bool] = None
    ) -> bool:
        """Update accessories settings."""
        if subs_enabled is None and rears_enabled is None:
            accessories = await self.get_accessories()
            subs_enabled = accessories.enabled.subs  # type: ignore
            rears_enabled = accessories.enabled.rears  # type: ignore
        body = {"enabled": {"rears": rears_enabled, "subs": subs_enabled}}
        return await self._request("/accessories", "PUT", body)

    async def get_battery_status(self) -> BR.Battery:
        """Retrieve battery status."""
        return BR.Battery(await self._request("/system/battery", "GET"))

    async def get_audio_mode(self) -> BR.AudioMode:
        """Retrieve the audio mode."""
        return BR.AudioMode(await self._request("/audio/mode", "GET"))

    async def set_audio_mode(self, mode: str) -> bool:
        """Set the audio mode."""
        result = await self._request("/audio/mode", "POST", {"value": mode})
        return result.get("value") == mode

    async def get_dual_mono_setting(self) -> BR.DualMonoSettings:
        """Retrieve the dual mono setting."""
        return BR.DualMonoSettings(await self._request("/audio/dualMonoSelect", "GET"))

    async def set_dual_mono_setting(self, value: Union[int, str]) -> bool:
        """Set the dual mono setting."""
        result = await self._request("/audio/dualMonoSelect", "POST", {"value": value})
        return result.get("value") == value

    async def get_rebroadcast_latency_mode(self) -> BR.RebroadcastLatencyMode:
        """Retrieve the rebroadcast latency mode."""
        return await self._request("/audio/rebroadcastLatency/mode", "GET")

    async def set_rebroadcast_latency_mode(self, mode: str) -> bool:
        """Set the rebroadcast latency mode."""
        result = await self._request(
            "/audio/rebroadcastLatency/mode", "PUT", {"mode": mode}
        )
        return result.get("value") == mode

    async def get_active_groups(self) -> List[BR.ActiveGroup]:
        """Retrieve active groups."""
        groups = await self._request("/grouping/activeGroups", "GET")
        return [BR.ActiveGroup(group) for group in groups.get("activeGroups", [])]

    async def set_active_group(self, other_product_ids: List[str]) -> bool:
        """Set the active group including this device and other products."""
        body = {"products": [{"productId": self._device_id, "role": "NORMAL"}]}
        for product_id in other_product_ids:
            body["products"].append({"productId": product_id, "role": "NORMAL"})
        return await self._request("/grouping/activeGroups", "POST", body)

    async def add_to_active_group(
        self, active_group_id: str, other_product_ids: List[str]
    ) -> bool:
        """Add products to the active group."""
        body = {
            "activeGroupId": active_group_id,
            "addProducts": [
                {"productId": pid, "role": "NORMAL"} for pid in other_product_ids
            ],
            "addGroups": [],
            "removeGroups": [],
            "removeProducts": [],
        }
        return await self._request("/grouping/activeGroups", "PUT", body)

    async def remove_from_active_group(
        self, active_group_id: str, other_product_ids: List[str]
    ) -> bool:
        """Remove products from the active group."""
        body = {
            "name": "",
            "activeGroupId": active_group_id,
            "addProducts": [],
            "addGroups": [],
            "removeGroups": [],
            "removeProducts": [
                {"productId": pid, "role": "NORMAL"} for pid in other_product_ids
            ],
        }
        return await self._request("/grouping/activeGroups", "PUT", body)

    async def stop_active_groups(self) -> bool:
        """Stop all active groups."""
        return await self._request("/grouping/activeGroups", "DELETE")

    async def get_system_timeout(self) -> BR.SystemTimeout:
        """Retrieve the system timeout settings."""
        return BR.SystemTimeout(await self._request("/system/power/timeouts", "GET"))

    async def set_system_timeout(
        self, no_audio: bool, no_video: bool
    ) -> BR.SystemTimeout:
        """Set system timeout settings."""
        body = {"noAudio": no_audio, "noVideo": no_video}
        return BR.SystemTimeout(
            await self._request("/system/power/timeouts", "PUT", body)
        )

    async def get_cec_settings(self) -> BR.CecSettings:
        """Retrieve the CEC settings."""
        return BR.CecSettings(await self._request("/cec", "GET"))

    async def set_cec_settings(
        self, mode: BR.CecSettingsSupportedValuesEnum
    ) -> BR.CecSettings:
        """Set the CEC settings."""
        return BR.CecSettings(await self._request("/cec", "PUT", {"mode": mode}))

    async def get_product_settings(self) -> BR.ProductSettings:
        """Retrieve the product settings."""
        return BR.ProductSettings(await self._request("/system/productSettings", "GET"))

    async def get_network_status(self) -> BR.NetworkStatus:
        """Retrieve the network status."""
        return BR.NetworkStatus(await self._request("/network/status", "GET"))

    async def get_wifi_status(self) -> BR.WifiStatus:
        """Retrieve the WiFi status."""
        return BR.WifiStatus(await self._request("/network/wifi/status", "GET"))

    def is_connected(self) -> bool:
        """Return True if the WebSocket is connected."""
        return self._connected

    async def set_chromecast(self, enable: bool = True) -> Dict[str, Any]:
        """Enable Chromecast on the device (PUT /cast/setup)."""
        body: Dict[str, Any] = {
            "bosePersonID": self._bose_auth.getControlToken().get("bosePersonID")
        }
        return await self._request(
            "/cast/setup" if enable else "/cast/teardown", "PUT", body
        )


class BoseConnectionBackoffError(ConnectionError):
    """A previous connection failed; retry after the per-speaker cooldown."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"Speaker reconnect cooldown: retry in {retry_after:.2f}s.")


class BoseFunctionNotSupportedException(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(self.message)


class BoseCapabilitiesNotLoadedException(Exception):
    def __init__(self) -> None:
        self.message = "Capabilities not loaded yet."
        super().__init__(self.message)


class BoseInvalidAudioSettingException(Exception):
    def __init__(self, setting: str) -> None:
        self.setting = setting
        self.message = f"Invalid audio setting: {setting}"
        super().__init__(self.message)


class BoseRequestException(Exception):
    def __init__(
        self,
        method: str,
        resource: str,
        body: Dict[str, Any],
        http_status: int,
        error_status: int,
        message: str,
    ) -> None:
        self.method = method
        self.resource = resource
        self.body = body
        self.http_status = http_status
        self.error_status = error_status
        self.message = message

        super().__init__(
            f"'{method} {resource}' returned {http_status}: Bose Error #{error_status} - {self.message}"
        )
        _LOGGER.debug(f"Request body for previous error: {json.dumps(body, indent=4)}")
