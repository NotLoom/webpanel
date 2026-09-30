#!/usr/bin/env python3
"""
Dizzy's Web Panel
-----------------
A phone-friendly, dark Material-style control panel for VTube Studio.
Make buttons with your own text + image; tap one to fire a VTube Studio hotkey.

Setup:
    pip install aiohttp
    python dizzys_web_panel.py

Standalone executable:
    ./build.sh   ->  dist/dizzys-web-panel

In VTube Studio: Settings -> turn ON "Start API" (default port 8001).
First run: VTube Studio shows a popup asking to allow "Dizzy's Web Panel" -> Allow.
Then open the printed http://<your-pc-ip>:8080 link on your phone (same Wi-Fi).
"""

import argparse
import asyncio
import json
import os
import socket
import sys
import uuid
from pathlib import Path

from aiohttp import ClientSession, ClientWSTimeout, WSMsgType, web


def app_dir() -> Path:
    """Folder that stores the data files.

    Running from source this is the script's folder; running as a frozen
    executable (PyInstaller) it is the folder the executable lives in, so
    buttons and the auth token survive restarts and temp-dir cleanup.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE = app_dir()
DATA_FILE = BASE / "dizzys_panel_data.json"
TOKEN_FILE = BASE / "dizzys_panel_token.txt"
PLUGIN_NAME = "Dizzy's Web Panel"
PLUGIN_DEV = "Dizzy"


# ───────────────────────── VTube Studio client ─────────────────────────
class VTS:
    def __init__(self, url: str):
        self.url = url
        self.session = None
        self.ws = None
        self.authed = False
        self.lock = asyncio.Lock()
        self.token = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""

    async def _send(self, mtype, data=None, timeout=10):
        rid = uuid.uuid4().hex[:12]
        payload = {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": rid,
            "messageType": mtype,
        }
        if data is not None:
            payload["data"] = data
        await self.ws.send_json(payload)
        while True:
            msg = await asyncio.wait_for(self.ws.receive(), timeout)
            if msg.type == WSMsgType.TEXT:
                r = json.loads(msg.data)
                if r.get("requestID") == rid:
                    return r
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                raise ConnectionError("VTube Studio closed the connection")

    async def _close(self):
        self.authed = False
        try:
            if self.ws and not self.ws.closed:
                await self.ws.close()
        except Exception:
            pass
        try:
            if self.session and not self.session.closed:
                await self.session.close()
        except Exception:
            pass
        self.ws = None
        self.session = None

    async def _connect(self):
        await self._close()
        self.session = ClientSession()
        self.ws = await self.session.ws_connect(
            self.url, heartbeat=20, timeout=ClientWSTimeout(ws_close=5)
        )

        if not self.token:
            r = await self._send(
                "AuthenticationTokenRequest",
                {"pluginName": PLUGIN_NAME, "pluginDeveloper": PLUGIN_DEV},
                timeout=60,  # waiting for you to click Allow in VTube Studio
            )
            tok = r.get("data", {}).get("authenticationToken")
            if not tok:
                raise RuntimeError("VTube Studio denied the plugin request")
            self.token = tok
            TOKEN_FILE.write_text(tok)

        r = await self._send(
            "AuthenticationRequest",
            {
                "pluginName": PLUGIN_NAME,
                "pluginDeveloper": PLUGIN_DEV,
                "authenticationToken": self.token,
            },
        )
        if not r.get("data", {}).get("authenticated"):
            self.token = ""
            TOKEN_FILE.unlink(missing_ok=True)
            raise RuntimeError("VTube Studio rejected the saved token. Refresh and tap Allow again.")
        self.authed = True

    async def call(self, mtype, data=None):
        async with self.lock:
            for attempt in (1, 2):
                try:
                    if not self.ws or self.ws.closed or not self.authed:
                        await self._connect()
                    return await self._send(mtype, data)
                except Exception:
                    await self._close()
                    if attempt == 2:
                        raise


def friendly(e: Exception) -> str:
    name = e.__class__.__name__
    if name in ("ClientConnectorError", "ClientConnectionError", "ConnectionRefusedError", "OSError"):
        return "Can't reach VTube Studio. Is it running with the API enabled?"
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return "VTube Studio didn't respond. Did you click Allow on the plugin popup?"
    return str(e) or name


# ───────────────────────── Storage ─────────────────────────
def load_buttons():
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8")).get("buttons", [])
    except Exception:
        return []


def save_buttons(buttons):
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"buttons": buttons}), encoding="utf-8")
    os.replace(tmp, DATA_FILE)


def clean_button(b):
    img = b.get("image", "")
    if not (isinstance(img, str) and img.startswith("data:image/") and len(img) <= 600_000):
        img = ""
    return {
        "id": str(b.get("id") or uuid.uuid4().hex)[:40],
        "label": str(b.get("label", ""))[:60],
        "hotkeyID": str(b.get("hotkeyID", ""))[:200],
        "image": img,
    }


# ───────────────────────── API routes ─────────────────────────
async def index(request):
    return web.Response(text=PAGE, content_type="text/html")


async def api_state(request):
    vts: VTS = request.app["vts"]
    out = {"buttons": load_buttons(), "connected": False, "error": "", "model": "", "hotkeys": []}
    try:
        r = await vts.call("HotkeysInCurrentModelRequest", {})
        d = r.get("data", {})
        if r.get("messageType") == "APIError":
            raise RuntimeError(d.get("message", "VTube Studio error"))
        out["connected"] = True
        out["model"] = d.get("modelName") or ""
        out["hotkeys"] = [
            {
                "hotkeyID": h.get("hotkeyID"),
                "name": h.get("name") or h.get("type") or "Unnamed",
                "type": h.get("type", ""),
            }
            for h in d.get("availableHotkeys", [])
        ]
        if not d.get("modelLoaded", True):
            out["error"] = "No model is loaded in VTube Studio."
    except Exception as e:
        out["error"] = friendly(e)
    return web.json_response(out)


async def api_put_buttons(request):
    try:
        body = await request.json()
        if not isinstance(body, list):
            raise ValueError
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    buttons = [clean_button(b) for b in body if isinstance(b, dict)][:200]
    save_buttons(buttons)
    return web.json_response(buttons)


async def api_trigger(request):
    vts: VTS = request.app["vts"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    hid = str(body.get("hotkeyID", ""))
    if not hid:
        return web.json_response({"ok": False, "error": "No action set on this button"}, status=400)
    try:
        r = await vts.call("HotkeyTriggerRequest", {"hotkeyID": hid})
        if r.get("messageType") == "APIError":
            return web.json_response({"ok": False, "error": r.get("data", {}).get("message", "VTube Studio error")})
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": friendly(e)})


async def on_cleanup(app):
    await app["vts"]._close()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description=PLUGIN_NAME)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--vts", default="ws://localhost:8001", help="VTube Studio API websocket URL")
    args = ap.parse_args()

    app = web.Application(client_max_size=10 * 1024 * 1024)
    app["vts"] = VTS(args.vts)
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_put("/api/buttons", api_put_buttons)
    app.router.add_post("/api/trigger", api_trigger)
    app.on_cleanup.append(on_cleanup)

    print(f"\n  {PLUGIN_NAME}")
    print(f"  On this PC : http://localhost:{args.port}")
    print(f"  On phone   : http://{lan_ip()}:{args.port}   (same Wi-Fi)")
    print(f"  Data files : {BASE}\n")
    web.run_app(app, host=args.host, port=args.port, print=None)


# ───────────────────────── Frontend ─────────────────────────
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f0f12">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Dizzy's Web Panel</title>
<style>
:root{--bg:#0f0f12;--surface:#1a1a20;--surface2:#24242c;--outline:#34343f;--text:#ececf1;--muted:#9a9aa8;
--primary:#b69cff;--onprimary:#21114d;--ok:#6fdc8c;--bad:#ff6b6b;--r:20px}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
[hidden]{display:none!important}
html,body{margin:0;background:var(--bg);color:var(--text);
font-family:Roboto,system-ui,-apple-system,"Segoe UI",sans-serif;touch-action:manipulation}
body{min-height:100dvh;padding-bottom:calc(110px + env(safe-area-inset-bottom))}
button{font:inherit;color:inherit}

header{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:4px;
padding:calc(10px + env(safe-area-inset-top)) 12px 10px 20px;background:rgba(15,15,18,.85);
backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-bottom:1px solid var(--outline)}
h1{flex:1;margin:0;font-size:20px;font-weight:600;letter-spacing:.2px}
.icon{width:44px;height:44px;border:0;border-radius:50%;background:none;display:grid;place-items:center;cursor:pointer;color:var(--text)}
.icon:active{background:var(--surface2)}
.icon svg{width:24px;height:24px;fill:currentColor}
#editBtn .done{display:none}
body.editing #editBtn{color:var(--primary)}
body.editing #editBtn .pen{display:none}
body.editing #editBtn .done{display:block}
.spin svg{animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.status{display:flex;align-items:center;gap:10px;padding:14px 20px 6px}
.chip{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:999px;background:var(--surface);
border:1px solid var(--outline);font-size:13px;color:var(--muted);max-width:100%}
.chip b{color:var(--text);font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--bad);flex:none}
.chip.ok .dot{background:var(--ok);box-shadow:0 0 8px var(--ok)}
.banner{margin:8px 16px 0;padding:12px 16px;border-radius:14px;background:#2a1c1f;border:1px solid #5a2f36;color:#ffb4ba;font-size:14px;line-height:1.35}

.editbar{display:flex;align-items:center;gap:12px;margin:10px 16px 0;padding:10px 10px 10px 16px;border-radius:16px;
background:var(--surface);border:1px solid var(--outline);font-size:13px;color:var(--muted)}
.editbar span{flex:1}

#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px;padding:14px 16px 0}
.tile{position:relative;aspect-ratio:1;border:0;border-radius:var(--r);overflow:hidden;padding:0;cursor:pointer;
display:flex;align-items:flex-end;background-size:cover;background-position:center;color:#fff;
box-shadow:0 1px 3px rgba(0,0,0,.5);transition:transform .12s;user-select:none;-webkit-user-select:none}
.tile:active{transform:scale(.95)}
.tile .ph{position:absolute;inset:0;display:grid;place-items:center;font-size:48px;font-weight:700;opacity:.5}
.tile .lbl{position:relative;width:100%;padding:30px 12px 11px;text-align:left;font-size:15px;font-weight:600;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;background:linear-gradient(transparent,rgba(0,0,0,.7))}
.tile.fired{animation:pulse .5s ease-out}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(182,156,255,.85)}100%{box-shadow:0 0 0 20px rgba(182,156,255,0)}}
.badge{position:absolute;top:8px;right:8px;width:30px;height:30px;border-radius:50%;background:rgba(0,0,0,.65);
display:none;place-items:center}
.badge svg{width:16px;height:16px;fill:#fff}
body.editing .badge{display:grid}
body.editing .tile{outline:2px dashed var(--primary);outline-offset:-3px}

.empty{margin:48px 28px;text-align:center;color:var(--muted);line-height:1.5}
.empty h2{color:var(--text);font-size:18px;margin:0 0 6px;font-weight:600}
.empty .row{display:flex;gap:10px;justify-content:center;margin-top:18px;flex-wrap:wrap}

.btn{border:0;border-radius:999px;padding:11px 20px;font-size:14px;font-weight:600;cursor:pointer;letter-spacing:.2px}
.btn:active{filter:brightness(.85)}
.btn.filled{background:var(--primary);color:var(--onprimary)}
.btn.tonal{background:var(--surface2);color:var(--text)}
.btn.text{background:none;color:var(--primary);padding:11px 14px}
.btn.danger{color:var(--bad)}
.btn:disabled{opacity:.4;pointer-events:none}

.fab{position:fixed;right:20px;bottom:calc(22px + env(safe-area-inset-bottom));width:60px;height:60px;border:0;border-radius:20px;
background:var(--primary);color:var(--onprimary);display:grid;place-items:center;cursor:pointer;z-index:6;
box-shadow:0 6px 18px rgba(0,0,0,.55);transition:transform .12s}
.fab:active{transform:scale(.92)}
.fab svg{width:28px;height:28px;fill:currentColor}

#scrim{position:fixed;inset:0;background:rgba(0,0,0,.6);opacity:0;pointer-events:none;transition:opacity .2s;z-index:10}
#scrim.open{opacity:1;pointer-events:auto}
#sheet{position:fixed;left:0;right:0;bottom:0;margin:0 auto;max-width:520px;z-index:11;background:var(--surface);
border-radius:28px 28px 0 0;padding:10px 20px calc(20px + env(safe-area-inset-bottom));transform:translateY(105%);
transition:transform .26s cubic-bezier(.2,.8,.2,1);max-height:92dvh;overflow:auto;border:1px solid var(--outline);border-bottom:0}
#sheet.open{transform:none}
.grab{width:36px;height:4px;border-radius:2px;background:var(--outline);margin:0 auto 14px}
#sheet h2{margin:0 0 16px;font-size:20px;font-weight:600}
.field{display:block;margin-bottom:14px}
.field span{display:block;font-size:12px;color:var(--muted);margin:0 0 6px 4px;letter-spacing:.3px}
.field input,.field select{width:100%;padding:14px;font-size:16px;border-radius:14px;border:1px solid var(--outline);
background:var(--surface2);color:var(--text);outline:none;appearance:none;-webkit-appearance:none}
.field select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%239a9aa8'%3E%3Cpath d='M7 10l5 5 5-5z'/%3E%3C/svg%3E");
background-repeat:no-repeat;background-position:right 10px center;background-size:24px;padding-right:40px}
.field input:focus,.field select:focus{border-color:var(--primary)}
.imgrow{display:flex;align-items:center;gap:14px;margin-bottom:10px}
.prev{width:84px;height:84px;border-radius:18px;background:var(--surface2) center/cover;border:1px solid var(--outline);
display:grid;place-items:center;font-size:12px;color:var(--muted);flex:none}
.imgbtns{display:flex;flex-direction:column;align-items:flex-start;gap:4px}
.actions{display:flex;align-items:center;gap:6px;margin-top:14px}
.actions .sp{flex:1}
.move{display:flex;gap:6px;margin-top:4px}

#toast{position:fixed;left:50%;bottom:calc(100px + env(safe-area-inset-bottom));transform:translate(-50%,20px);opacity:0;
background:#2f2f3a;color:var(--text);padding:12px 18px;border-radius:14px;font-size:14px;z-index:20;max-width:88vw;
text-align:center;pointer-events:none;transition:.22s;box-shadow:0 6px 18px rgba(0,0,0,.5)}
#toast.show{opacity:1;transform:translate(-50%,0)}
</style>
</head>
<body>
<header>
  <h1>Dizzy's Web Panel</h1>
  <button class="icon" id="refresh" aria-label="Refresh"><svg viewBox="0 0 24 24"><path d="M17.65 6.35A7.96 7.96 0 0 0 12 4a8 8 0 1 0 7.73 10h-2.08A6 6 0 1 1 12 6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg></button>
  <button class="icon" id="editBtn" aria-label="Edit mode">
    <svg class="pen" viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>
    <svg class="done" viewBox="0 0 24 24"><path d="M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
  </button>
</header>

<div class="status"><div class="chip" id="chip"><span class="dot"></span><b id="chipText">Connecting…</b></div></div>
<div class="banner" id="banner" hidden></div>
<div class="editbar" id="editBar" hidden>
  <span>Edit mode: tap a button to change it</span>
  <button class="btn tonal" id="importAll">Import all</button>
</div>

<main id="grid"></main>

<div class="empty" id="empty" hidden>
  <h2>No buttons yet</h2>
  Pull every action from VTube Studio in one tap, or build them one at a time.
  <div class="row">
    <button class="btn filled" id="importAll2">Import all actions</button>
    <button class="btn tonal" id="addBtn2">Add a button</button>
  </div>
</div>

<button class="fab" id="fab" aria-label="Add button"><svg viewBox="0 0 24 24"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg></button>

<div id="scrim"></div>
<div id="sheet" role="dialog" aria-modal="true">
  <div class="grab"></div>
  <h2 id="sheetTitle">New button</h2>
  <label class="field"><span>BUTTON TEXT</span><input id="fLabel" maxlength="40" placeholder="Wave" autocomplete="off"></label>
  <label class="field"><span>VTUBE STUDIO ACTION</span><select id="fHotkey"></select></label>
  <div class="imgrow">
    <div class="prev" id="fPrev">No image</div>
    <div class="imgbtns">
      <button class="btn tonal" id="fPick">Choose image</button>
      <button class="btn text" id="fClear">Remove image</button>
    </div>
  </div>
  <input type="file" id="fFile" accept="image/*" hidden>
  <div class="move" id="moveRow">
    <button class="btn text" id="fLeft">← Move earlier</button>
    <button class="btn text" id="fRight">Move later →</button>
  </div>
  <div class="actions">
    <button class="btn text danger" id="fDelete">Delete</button>
    <span class="sp"></span>
    <button class="btn text" id="fCancel">Cancel</button>
    <button class="btn filled" id="fSave">Save</button>
  </div>
</div>
<div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const uid = () => Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
const JSONH = {'Content-Type': 'application/json'};
let state = {connected: false, error: '', model: '', hotkeys: [], buttons: []};
let editMode = false, draft = null, autoLabel = '';

function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('show'), 2600);
}
const getJSON = async (u, o) => (await fetch(u, o)).json();
const hue = s => { let h = 0; for (const c of s || 'x') h = (h * 31 + c.charCodeAt(0)) % 360; return h; };

async function load(spin) {
  if (spin) $('#refresh').classList.add('spin');
  try { state = await getJSON('/api/state'); }
  catch (e) { state.connected = false; state.error = "Can't reach the panel server."; }
  $('#refresh').classList.remove('spin');
  render();
}

function render() {
  $('#chip').className = 'chip' + (state.connected ? ' ok' : '');
  $('#chipText').textContent = state.connected ? (state.model || 'Connected') : 'Offline';
  const bn = $('#banner'); bn.hidden = !state.error; bn.textContent = state.error;
  document.body.classList.toggle('editing', editMode);
  $('#editBar').hidden = !editMode;

  const grid = $('#grid'); grid.replaceChildren();
  for (const b of state.buttons) {
    const t = document.createElement('button');
    t.className = 'tile';
    if (b.image) t.style.backgroundImage = `url("${b.image}")`;
    else {
      const h = hue(b.label || b.id);
      t.style.background = `linear-gradient(135deg, hsl(${h} 45% 30%), hsl(${(h + 50) % 360} 50% 18%))`;
      const ph = document.createElement('span'); ph.className = 'ph';
      ph.textContent = ((b.label || '?').trim().charAt(0) || '?').toUpperCase();
      t.append(ph);
    }
    const l = document.createElement('span'); l.className = 'lbl'; l.textContent = b.label || 'Untitled';
    t.append(l);
    const bd = document.createElement('span'); bd.className = 'badge';
    bd.innerHTML = '<svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>';
    t.append(bd);
    t.onclick = () => editMode ? openEditor(b) : fire(b, t);
    grid.append(t);
  }
  $('#empty').hidden = state.buttons.length > 0;
}

async function fire(b, el) {
  if (navigator.vibrate) navigator.vibrate(15);
  el.classList.remove('fired'); void el.offsetWidth; el.classList.add('fired');
  try {
    const r = await getJSON('/api/trigger', {method: 'POST', headers: JSONH, body: JSON.stringify({hotkeyID: b.hotkeyID})});
    if (!r.ok) { toast(r.error || 'Action failed'); load(); }
  } catch (e) { toast('Panel server unreachable'); }
}

async function saveButtons(list) {
  try {
    const r = await fetch('/api/buttons', {method: 'PUT', headers: JSONH, body: JSON.stringify(list)});
    if (!r.ok) throw 0;
    state.buttons = await r.json(); render(); return true;
  } catch (e) { toast('Could not save'); return false; }
}

/* ---------- editor ---------- */
function fillHotkeys() {
  const sel = $('#fHotkey'); sel.replaceChildren();
  const add = (v, t, dis) => { const o = document.createElement('option'); o.value = v; o.textContent = t; if (dis) o.disabled = true; sel.append(o); };
  if (!state.hotkeys.length) add('', state.connected ? 'No actions found in this model' : 'Not connected to VTube Studio', true);
  else add('', 'Choose an action…', true);
  const known = state.hotkeys.some(h => h.hotkeyID === draft.hotkeyID);
  if (draft.hotkeyID && !known) add(draft.hotkeyID, (draft.label || 'Saved action') + ' (unavailable now)');
  for (const h of state.hotkeys) add(h.hotkeyID, h.type ? `${h.name}  ·  ${h.type}` : h.name);
  sel.value = draft.hotkeyID || '';
}
function drawPreview() {
  const p = $('#fPrev');
  p.style.backgroundImage = draft.image ? `url("${draft.image}")` : '';
  p.textContent = draft.image ? '' : 'No image';
  $('#fClear').disabled = !draft.image;
}
function openEditor(b) {
  draft = b ? {...b} : {id: '', label: '', hotkeyID: '', image: ''};
  autoLabel = '';
  $('#sheetTitle').textContent = b ? 'Edit button' : 'New button';
  $('#fLabel').value = draft.label;
  fillHotkeys(); drawPreview();
  $('#fDelete').hidden = !b; $('#moveRow').hidden = !b;
  $('#scrim').classList.add('open'); $('#sheet').classList.add('open');
}
function closeEditor() { $('#scrim').classList.remove('open'); $('#sheet').classList.remove('open'); draft = null; }

$('#fHotkey').onchange = e => {
  draft.hotkeyID = e.target.value;
  const h = state.hotkeys.find(x => x.hotkeyID === draft.hotkeyID);
  const cur = $('#fLabel').value;
  if (h && (!cur.trim() || cur === autoLabel)) { autoLabel = h.name; $('#fLabel').value = h.name; }
};
$('#fPick').onclick = () => $('#fFile').click();
$('#fClear').onclick = () => { draft.image = ''; drawPreview(); };
$('#fFile').onchange = async e => {
  const f = e.target.files[0]; e.target.value = ''; if (!f) return;
  try { draft.image = await shrink(f); drawPreview(); } catch (_) { toast('Could not read that image'); }
};
function shrink(file) {
  return new Promise((res, rej) => {
    const url = URL.createObjectURL(file), im = new Image();
    im.onload = () => {
      const S = 320, c = document.createElement('canvas'); c.width = c.height = S;
      const x = c.getContext('2d'); x.fillStyle = '#1a1a20'; x.fillRect(0, 0, S, S);
      const m = Math.min(im.width, im.height);
      x.drawImage(im, (im.width - m) / 2, (im.height - m) / 2, m, m, 0, 0, S, S);
      URL.revokeObjectURL(url); res(c.toDataURL('image/jpeg', .85));
    };
    im.onerror = () => { URL.revokeObjectURL(url); rej(); };
    im.src = url;
  });
}
$('#fSave').onclick = async () => {
  draft.label = $('#fLabel').value.trim();
  if (!draft.hotkeyID) return toast('Pick an action first');
  if (!draft.label) draft.label = 'Untitled';
  const list = state.buttons.slice();
  const i = draft.id ? list.findIndex(b => b.id === draft.id) : -1;
  if (i >= 0) list[i] = draft; else { draft.id = uid(); list.push(draft); }
  if (await saveButtons(list)) closeEditor();
};
$('#fDelete').onclick = async () => {
  if (!confirm('Delete this button?')) return;
  if (await saveButtons(state.buttons.filter(b => b.id !== draft.id))) closeEditor();
};
function move(d) {
  const list = state.buttons.slice(), i = list.findIndex(b => b.id === draft.id), j = i + d;
  if (i < 0 || j < 0 || j >= list.length) return;
  [list[i], list[j]] = [list[j], list[i]]; saveButtons(list);
}
$('#fLeft').onclick = () => move(-1);
$('#fRight').onclick = () => move(1);
$('#fCancel').onclick = closeEditor;
$('#scrim').onclick = closeEditor;
document.addEventListener('keydown', e => { if (e.key === 'Escape' && draft) closeEditor(); });

/* ---------- top-level actions ---------- */
async function importAll() {
  if (!state.hotkeys.length) return toast('No actions found. Check VTube Studio.');
  const have = new Set(state.buttons.map(b => b.hotkeyID));
  const fresh = state.hotkeys.filter(h => !have.has(h.hotkeyID))
    .map(h => ({id: uid(), label: h.name, hotkeyID: h.hotkeyID, image: ''}));
  if (!fresh.length) return toast('Everything is already added');
  if (await saveButtons(state.buttons.concat(fresh))) toast(`Added ${fresh.length} button${fresh.length > 1 ? 's' : ''}`);
}
$('#importAll').onclick = importAll;
$('#importAll2').onclick = importAll;
$('#fab').onclick = () => openEditor(null);
$('#addBtn2').onclick = () => openEditor(null);
$('#refresh').onclick = () => load(true);
$('#editBtn').onclick = () => { editMode = !editMode; render(); };
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });

load(true);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
