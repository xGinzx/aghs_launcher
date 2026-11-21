# Aghs Launcher

A Windows launcher and local HTTP bridge for a subscribed Dota 2 Workshop game.
Python 3.10 or newer is required. Normal operation uses only the Python standard
library; there are no Python packages to install.

The default Workshop item is `2483181385`, the map is `aghanim3`, and the local
addon is **`aghs_public`**. This release uses its own adapter and protocol. Every
player must use the same public-release addon; an older private `aghs` addon is
not a matching client installation.

## Install and host

1. Subscribe to the Workshop item and wait for Steam to finish downloading it.
2. Install [Python for Windows](https://www.python.org/downloads/windows/).
3. Extract this source package into a folder of your choice.
4. Start Steam and quit Dota.
5. Run **Host Aghs.cmd**. Keep its window open while hosting.

The launcher discovers Steam libraries, builds a local addon, starts Dota with
console logging, and forwards the game's server-side HTTP requests. Building
uses additional disk space approximately equal to the Workshop VPK. The original
Workshop download is read but is never overwritten.

The public launcher does not require or start VG. Close any other launcher or
bridge before using it.

## Prepare another player's files

Each player can run **Prepare.cmd** with Dota closed. It builds the local addon
from that player's own Workshop download, without launching Dota or running a
bridge. The script is useful to hosts as well as joining players.

Alternatively, copy only these two generated files from the host into the same
relative folder in the other player's Dota installation:

```text
game/dota_addons/aghs_public/addoninfo.txt
game/dota_addons/aghs_public/pak01_dir.vpk
```

Keep the bridge's configuration, response files, and logs private. They are not
needed by joining clients. Runtime responses can contain player information.

If switching from a manually copied addon to local preparation, first move that
manual `aghs_public` folder aside. The preparer refuses to overwrite an unmanaged
folder. It does not modify other addons, including a pre-existing `aghs` folder.

## Join over the internet

The host must forward **UDP 27015** to the hosting PC and allow Dota through
Windows Firewall. The launcher sets `sv_lan 0`. This bridge does not configure
the router or Windows Firewall.

After the host loads the map, friends start Dota normally and enter:

```text
connect HOST_PUBLIC_IP:27015
```

Join the map's cooperative team through its UI, or use `jointeam good` if needed.
Wait at the setup screen for everyone to arrive. The host can use
`customgamesetup_auto_assign_players` to assign connected players.

Only the host runs the HTTP bridge. Each host launch generates its own mailbox
session token; clients do not need that token. Matching addon files and Dota
versions determine client compatibility. A shared token does not synchronize
addon versions.

## Workshop updates

1. Quit Dota and let Steam finish updating the Workshop game.
2. The host runs **Host Aghs.cmd**; joining players run **Prepare.cmd**.
3. Compare the displayed build identifiers. They should match when the Workshop
   input and this launcher's Lua sources match.

Players who copied the two generated files manually must receive fresh copies
after each update. Steam does not update a separate manually copied addon.

The preparer checks source metadata, the VPK directory hash, and patch contents.
It skips a rebuild when those inputs and the installed adapter are unchanged.
A build writes a temporary archive, verifies the replacement entries, and then
replaces the generated archive. A Workshop file that changes during building
causes the build to stop.

## Settings and command line

Copy `settings.example.json` to **settings.local.json** to customize paths or map
settings. The local file is excluded from Git and release ZIPs. Set `game_dir`
to the directory containing `dota_addons`, not the `dota` subdirectory.

```text
python launcher.py prepare
python launcher.py host
python launcher.py host --probe
python launcher.py prepare --game-dir "D:/SteamLibrary/steamapps/common/dota 2 beta/game"
```

For a manually launched Dota process, first prepare the addon with Dota closed.
Start Dota with `-console -condebug`, run `python launcher.py bridge`, and then
enter this in Dota's console:

```text
sv_lan 0
customgamesetup_enable_auto_launch 0
dota_launch_custom_game aghs_public aghanim3
```

`--probe` requests Steam's public `robots.txt` and prints the Lua callback status
and response size. It does not submit a player profile.

An empty `allowed_origins` list permits public HTTP/HTTPS destinations requested
by the locally installed map. Set exact origins such as `https://example.org`
to restrict them. Private and reserved network addresses are blocked, including
redirect destinations. TLS certificates are verified. The HTTP client connects
to the validated address and does not use environment HTTP proxies. Systems
that require an explicit HTTP proxy are currently unsupported.

## How HTTP forwarding works

The generated entry point installs `ahb_bridge.lua`, then loads the original
Workshop entry point stored as `ahb_workshop.lua`. Native HTTP requests are used
when Dota supplies them. A replacement request object is supplied when the native
factory returns no object.

The adapter prints framed requests into `game/dota/console.log`. Python reads
those frames and performs the requests. Responses are written to a session
mailbox under the generated addon's `scripts` folder. Lua reads the mailbox with
`LoadKeyValues` and invokes the original game's callback. Acknowledgments remove
completed response files. There is no inbound HTTP listener.

See [PROTOCOL.md](PROTOCOL.md) for the message format. Supported methods are
GET, HEAD, POST, PUT, PATCH, DELETE, and OPTIONS. The adapter supports headers,
GET/POST parameters, raw bodies, and request timeouts. It does not implement
every function in Steam's HTTP API. Maximum framed request size is 1 MiB;
maximum response body size is 4 MiB. Requests are not automatically retried.

The game's own backend still receives the requests and controls account data
and progression. This tool does not create a replacement backend, matchmaking
service, or dedicated server. Future game or engine updates may require changes.

## Development and source release

Validation for this release included Python/Lua integration tests, a build from
the real Workshop VPK, and a Dota listen-server smoke test. The smoke test
received a successful Steam probe and game-backend responses through AHB/1.
An internet connection from another player still requires a real remote test.

Run the Python tests from this directory:

```text
python -B -m unittest discover -s tests -v
```

For the Lua cross-language tests, install the optional development dependency:

```text
python -m pip install "lupa>=2.6,<3"
python -B -m unittest discover -s tests -v
```

The Lua tests use a Lua 5.1 runtime with mocked Dota engine functions. They check
the actual Lua adapter against the Python decoder, mailbox writer, and a
temporary local HTTP server. Tests use synthetic data rather than player data.
They do not substitute for testing in the current Dota client or a remote join.

Create a source-only ZIP using the explicit release-file allowlist:

```text
python -B tools/make_release.py
```

Only listed source files are packaged. Workshop archives, generated addons,
local settings, logs, Python bytecode, and runtime mailboxes are excluded. A
SHA-256 checksum is emitted beside the ZIP.

## License and provenance

The source in this package is offered under the [MIT License](LICENSE). This
license does not grant rights to Valve software, Workshop content, or another
program's assets. Users supply their own subscribed Workshop content locally.

See [PROVENANCE.md](PROVENANCE.md) for how this implementation was developed.
This project is unaffiliated with Valve, VG, and the Workshop map's authors.
