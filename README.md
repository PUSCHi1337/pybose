# pybose — experimental stability fork

> Fork maintained by [PUSCHi1337](https://github.com/PUSCHi1337), based on
> [cavefire/pybose](https://github.com/cavefire/pybose).
> Fork modifications dated 2026-10-28 and 2026-10-29 are developed with AI assistance.
> The upstream authorship, copyright notices and [GNU GPLv3 license](LICENSE) are retained.
> This is not an official Bose product or an upstream release.

## Fork development status

This development branch includes the discovery cleanup from [draft PR #1](https://github.com/PUSCHi1337/pybose/pull/1)
and the connection lifecycle work in [draft PR #2](https://github.com/PUSCHi1337/pybose/pull/2).
The latter shares concurrent connection attempts, avoids recursive reconnects during
the handshake, observes background failures and adds a per-speaker retry cooldown
of 1, 2, 4, 8, 16, 32 and then at most 60 seconds after failed handshakes.
One automatic recovery attempt is made after a connection loss; subsequent explicit
calls or polling can retry after the cooldown. A successful handshake resets it.
Calls during cooldown raise `BoseConnectionBackoffError` (a `ConnectionError`)
with a `retry_after` value. Concurrent calls share a handshake; cancelling one
waiter leaves it running, while `disconnect()` cancels it and awaits cleanup.

The fork CI runs simulated unit tests with network sockets blocked. It does not
test real Bose logins, device compatibility or long-running Home Assistant use,
and it does not establish the cause of a NAS crash. Consult the linked PR checks
for the exact tested commit. No fork release or Home Assistant installation
package is available. The integration's token-task lifecycle and stale entity
client references remain separate work in the
[companion integration fork](https://github.com/PUSCHi1337/Bose-Homeassistant).
Its mDNS monitor is not changed by this library patch, and a newly created client
has its own cooldown state.

The device lists, API descriptions and examples below originate from the upstream
project. They describe upstream functionality, not hardware tests of this fork.
Discuss fork-specific changes in the relevant fork PR; upstream maintainers are
not responsible for this experimental branch.


[![Python Tests](https://github.com/PUSCHi1337/pybose/actions/workflows/python-tests.yml/badge.svg?branch=fix%2Fconnection-lifecycle)](https://github.com/PUSCHi1337/pybose/actions/workflows/python-tests.yml)

This Python project provides an unofficial API to control Bose Soundbars and Speakers locally. The API was reverse-engineered by analyzing the Bose app's API calls and is **not officially supported by Bose**. It allows users to interact with their Bose devices through local network communication and provides a basic feature set for device control.

Original author's Home Assistant integration: [Bose-Homeassistant](https://github.com/cavefire/Bose-Homeassistant)

### Devices reported as tested by upstream
- Bose Soundbar Ultra
- Bose Soundbar 900
- Bose Music Amplifier
- Bose Soundbar 700
- Bose Portable Speaker

Other Bose devices may also work, but they have not been tested.

---

## Upstream installation reference

The command below installs the upstream PyPI package. It does **not** install this fork's draft fixes.

```bash
pip install pybose
```

## Usage

### BoseAuth
BOSE decided that in order to control your devices locally (!) you still need to acquire a token from their cloud. This is done by using the `BoseAuth` class. 

Sadly there is no official documentation about this (at least none reported by upstream), so the upstream author reverse-engineered the API calls made by the official app. 

**Note:** The token is a JWT with a limited lifetime and needs to be refreshed manually. Consider caching the token to reduce API calls.

#### Usage of `BoseAuth`
```python
bose_auth = BoseAuth()
control_token = bose_auth.getControlToken(email, password)

access_token = control_token['access_token']
refresh_token = control_token['refresh_token']
```

---

### BoseDiscovery
After you got your access token, you can begin by discovering your devices on the network. Therefore you can use the `BoseDiscovery` class.

```python
discovery = BoseDiscovery()
devices = discovery.discover_devices()
for device in devices:
    print(f"GUID: {device['GUID']}, IP: {device['IP']}")
```

You will need not only the **IP**, but also the **GUID** to connect to the device.

---

### BoseDevice
Now you are ready to finally control your speaker! You can use the `BoseSpeaker` class to interact with your device.

```python
bose = BoseSpeaker(
    bose_auth=auth
    device_id="your_device_GUID",
    host="your_device_IP"
)

bose.attach_receiver(lambda data: print(f"Received unsolicited message: {json.dumps(data, indent=4)}"))

await bose.connect()
response = await bose.set_power_state(True)
await bose.disconnect()
```

After attaching to the speaker, you can use the following functions:
* get_capabilities
* get_system_info
* get_audio_volume
* set_audio_volume
* get_now_playing
* get_bluetooth_status
* get_power_state
* set_power_state
* pause
* play
* skip_next
* skip_previous
* get_device_id
* subscribe
* switch_tv_source
* set_source
* get_sources
* get_audio_settings
* set_audio_settings
* get_accessories
* get_battery_status

**Note:** The device supports more. But for now, these are the only functions implemented. Feel free to add more, or open an issue if you have a specific need.

## Development

### Running Tests

This project includes both unit tests and integration tests.

#### Unit Tests
Unit tests mock external dependencies and can be run without credentials:

```bash
python -m pytest tests/unit/ -v
```

#### Integration Tests
Integration tests require real Bose account credentials and test against actual Bose APIs. Set the following environment variables:

```bash
export BOSE_EMAIL="your_email@example.com"
export BOSE_PASSWORD="your_password"
export BOSE_GWID="your_device_gwid"  # Optional, for product information tests
```

Run integration tests:

```bash
# Run all integration tests
python -m pytest tests/integration/ -v -m integration

# Run specific integration test
python -m pytest tests/integration/test_bose_auth_integration.py -v -m integration
```

#### Coverage Reports
Generate coverage reports in XML and JSON format:

```bash
python -m pytest tests/unit/ --cov=pybose --cov-report=xml --cov-report=json --cov-report=term
```

## Limitations
* **Unofficial API:** The API is not officially supported by Bose and may break at any time.
* **Token Lifetime:** The token has a limited lifetime and needs to be refreshed manually.
* **Rate Limiting:** The API may be rate-limited by Bose if too many requests are made in a short period of time.

## Contributing
This project is a work in progress, and contributions are welcome!
If you encounter issues, have feature requests, or want to contribute, feel free to submit a pull request or open an issue.

The upstream file `AvailableMethods.txt` lists the discovered `resources`.
Best way to find out, what data these functions need, is to use the official app and sniff the network traffic. The upstream author used `Proxymon` for this. There should be a websocket connection to `ws://<device_ip>:8082` which contains all the data you need.

## Wishlist

The upstream author's **Home Assistant** integration is available here: [Bose-Homeassistant](https://github.com/cavefire/Bose-Homeassistant)

**Upstream wishlist:**

- [x] Implement token refresh
- [x] Implement groups
- [x] Implement source (TV / Optical / AUX) switching
- [ ] Implement source (Bluetooth) switching
- [x] Implement equalizer settings
- [x] Implement bass module settings
- [x] Implement surround speaker settings

And lastly, a way to not use the BOSE cloud at all would be nice. Upstream has not established whether this is possible.

## Disclaimer
This project is not affiliated with Bose Corporation. The API is reverse-engineered and may break at any time. Use at your own risk.

**Be respectful and avoid spamming the API with unnecessary requests to ensure this project remains functional for everyone.**


**Original author's note to the BOSE legal team (retained from cavefire's README):**

All API keys used in this project are publicly available on the Bose website.

There was no need to be a computer specialist to find them, so: Please do not sue me for making people use their products in a way they want to.

If you have any issues with me publishing this, please contact me! I am happy to discuss this with you and make your products better.

## License
This project is licensed under GNU GPLv3 - see the [LICENSE](LICENSE) file for details.
