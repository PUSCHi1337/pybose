# pybose — Bose speaker stability fork

Python library for local control of Bose smart soundbars and speakers.

Forked from [cavefire/pybose](https://github.com/cavefire/pybose).
The original copyright and [GNU GPLv3 license](LICENSE) remain in effect.
Fork changes are maintained by [PUSCHi1337](https://github.com/PUSCHi1337),
dated 2026-10-28/29 and developed with AI assistance. This project is
unofficial and is not affiliated with Bose Corporation.

## Development status

The development branch combines discovery resource cleanup with coordinated
WebSocket reconnects. Changes are tracked in [PR #1](https://github.com/PUSCHi1337/pybose/pull/1)
and [PR #2](https://github.com/PUSCHi1337/pybose/pull/2).

[![Offline unit tests](https://github.com/PUSCHi1337/pybose/actions/workflows/python-tests.yml/badge.svg?branch=fix%2Fconnection-lifecycle)](https://github.com/PUSCHi1337/pybose/actions/workflows/python-tests.yml)

The tests use simulated sockets and discovery objects. They do not prove hardware
compatibility, long-term Home Assistant stability or the cause of a NAS hang.
See each PR's checks for the exact tested commit.

The companion integration is [PUSCHi1337/Bose-Homeassistant](https://github.com/PUSCHi1337/Bose-Homeassistant).
No release of this fork has been published to PyPI. A plain
`pip install pybose` does not install these fork changes.

## Connection and cleanup behavior

- Discovery always cancels its browser and closes only a Zeroconf instance it owns.
- Concurrent callers share one connection handshake and one receiver.
- Handshake failures do not recursively reconnect.
- Failed handshakes apply a per-client cooldown: 1, 2, 4, 8, 16, 32, then at most
  60 seconds. Success resets it. Calls during cooldown raise
  `BoseConnectionBackoffError`, a `ConnectionError` with `retry_after`.
- A dropped connection starts one observed automatic recovery attempt. Later
  explicit calls or polling may retry after the cooldown.
- A failed send is retried at most once after recovery.
- Cancelling one waiter leaves the shared handshake available to other callers.
  `disconnect()` cancels and awaits connection/recovery work.
- `await speaker.reconnect(host=new_address)` changes address on the same client,
  retaining all callbacks. At the same address it preserves connection sharing
  and the existing cooldown.

## Authentication and use

Device commands travel over the local network, but a Bose account/cloud token is
required for authentication. Tokens expire and must be refreshed by the caller.

Start with the [authentication](examples/auth.py), [discovery](examples/discovery.py)
and [speaker](examples/speaker.py) examples. Provide credentials through your local
environment; do not put them in source code or issue reports.

## Device coverage

The inherited implementation targets Bose smart soundbars and speakers controlled
by the Bose app, rather than SoundTouch. The inherited device reports include the
Soundbar Ultra, 900 and 700, Music Amplifier and Portable Speaker. These are
historical reports, not hardware validation of this fork's changes. Features
depend on each device's advertised capabilities.

## Development and support

Run the unit tests using `tests/requirements-unit.txt`:

```sh
python -m pytest tests/unit --disable-socket --allow-unix-socket --timeout=10 -q
```

The [fork pull requests](https://github.com/PUSCHi1337/pybose/pulls) contain the
current changes, results and discussion. Integration tests under
`tests/integration` use real accounts/devices and are intentionally excluded
from the offline CI workflow.

## License

GNU GPLv3; see [LICENSE](LICENSE). The inherited history and notices remain
available, and modified versions must be identified as modified.
