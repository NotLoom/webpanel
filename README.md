# Dizzy's Web Panel

Phone-friendly, dark Material-style control panel for VTube Studio.
Make buttons with your own text + image; tap one on your phone and VTube Studio fires that hotkey.

## Standalone executable (no Python needed)

Grab a prebuilt binary from the [Releases page](https://github.com/NotLoom/webpanel/releases)
(Linux, Windows and macOS builds are produced automatically for every `v*` tag),
or build one yourself:

```bash
./build.sh                 # -> dist/dizzys-web-panel
```

Run the binary exactly like the script; all options below work the same:

```bash
./dist/dizzys-web-panel --port 9000
```

`dizzys_panel_data.json` and `dizzys_panel_token.txt` are written next to the executable,
so keep it in its own folder (e.g. move it out of your Downloads folder first).

## Run from source

### One-time setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

The only dependency is `aiohttp` (Python 3.10+; tested on 3.14).

### Run

```bash
./run.sh                             # http://<your-pc-ip>:8080
./run.sh --port 9000                 # custom port
./run.sh --host 127.0.0.1            # this PC only, no LAN access
./run.sh --vts ws://localhost:8001   # custom VTube Studio API URL
```

Then open the printed `http://<your-pc-ip>:8080` on your phone (same Wi-Fi).
`./run.sh` just calls `.venv/bin/python dizzys_web_panel.py`, so you never have to activate the venv by hand.

## In VTube Studio

1. Settings -> turn ON **Start API** (default port 8001).
2. First run: VTube Studio pops up "allow Dizzy's Web Panel" -> **Allow**.
3. In the panel, tap **Import all** to pull in every hotkey of the current model at once.

## Files created at runtime

| File | What it is |
|---|---|
| `dizzys_panel_data.json` | your buttons (label, action, image) |
| `dizzys_panel_token.txt` | VTube Studio auth token - keep private |

Both are git-ignored (the token is a credential).

## Notes

The server has no authentication and listens on all interfaces by default (0.0.0.0), so anyone on your
Wi-Fi can press buttons and change the layout. Use `./run.sh --host 127.0.0.1` (or
`dizzys-web-panel --host 127.0.0.1`) if you don't want that.

Downloaded macOS builds are unsigned, so Gatekeeper may block the first launch. Right-click the binary
and choose **Open**, or clear the flag with
`xattr -d com.apple.quarantine dizzys-web-panel-macos-arm64`.

Prebuilt binaries come from [`.github/workflows/build.yml`](.github/workflows/build.yml), which runs
PyInstaller on Linux, Windows and macOS and attaches the results to the GitHub release on every tag.
