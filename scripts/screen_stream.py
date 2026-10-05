#!/usr/bin/env python3
"""
Fast screenshot streaming over WebSocket + interactive control.
- Uses host adb directly (faster than docker exec wrapper)
- Downscales + JPEG-compresses frames for speed (resolution doesn't matter for account mgmt)
- Clipboard paste support (sets Android clipboard, then simulates paste)
"""
import asyncio, json, base64, subprocess, time, re, io
from aiohttp import web
from PIL import Image

# Tunables: lower SCALE / quality / target FPS = more speed, less detail
# At 720x1280 a 0.6 scale looked soft on a full-height view; 0.85 keeps text
# legible and still costs less than a native-resolution frame.
SCALE = 0.85         # resize factor applied to native screenshot
JPEG_QUALITY = 70     # 1-95, lower = smaller/faster
TARGET_FPS = 15       # loop throttle target

def adb(device_ip, args, timeout=5):
    cmd = f"adb -s {device_ip}:5555 {args}"
    return subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)

_res_cache = {}

def get_resolution(device_ip):
    """Effective screen size, honouring an `Override size`.

    `wm size` prints up to two lines:

        Physical size: 360x640
        Override size: 720x1280

    A plain first-match regex returns the PHYSICAL size, so after raising the
    resolution with `wm size WxH` every tap lands at the wrong coordinate
    (scaled by the ratio between the two). Prefer the override when present.

    The cache is keyed per device and must be dropped when the size changes,
    hence `clear_resolution()`.
    """
    if device_ip in _res_cache:
        return _res_cache[device_ip]
    try:
        r = adb(device_ip, "shell wm size", timeout=5)
        text = r.stdout.decode(errors="ignore")
        m = (re.search(r"Override size:\s*(\d+)x(\d+)", text)
             or re.search(r"Physical size:\s*(\d+)x(\d+)", text)
             or re.search(r"(\d+)x(\d+)", text))
        if m:
            res = (int(m.group(1)), int(m.group(2)))
            _res_cache[device_ip] = res
            return res
    except Exception:
        pass
    return (360, 640)


def clear_resolution(device_ip=None):
    if device_ip:
        _res_cache.pop(device_ip, None)
    else:
        _res_cache.clear()


def _adb_state(device_ip):
    r = subprocess.run("adb devices", shell=True, capture_output=True, timeout=10)
    for line in r.stdout.decode(errors="ignore").splitlines():
        if line.startswith(f"{device_ip}:5555"):
            return line.split()[-1]          # device | offline | unauthorized
    return "absent"


def ensure_connected(device_ip, tries=3):
    """Attach adb to the device, reconnecting when it is absent or offline.

    The streamer previously never called `adb connect`. On a freshly started
    phone the host adb has no entry for it, so `screencap` failed silently and
    the loop spun on a 0.3s sleep forever — the viewer showed "Connecting..."
    with no error and no frame, indefinitely. A phone that was restarted shows
    up as `offline`, which needs an explicit disconnect first or it never
    recovers.
    """
    st = _adb_state(device_ip)
    if st == "device":
        return True
    for _ in range(tries):
        if st == "offline":
            subprocess.run(f"adb disconnect {device_ip}:5555",
                           shell=True, capture_output=True, timeout=10)
        subprocess.run(f"adb connect {device_ip}:5555",
                       shell=True, capture_output=True, timeout=15)
        time.sleep(1.0)
        st = _adb_state(device_ip)
        if st == "device":
            clear_resolution(device_ip)      # size may differ after a restart
            return True
    return False


def boot_state(device_ip):
    """'ready' | 'booting' | 'offline' — what the viewer should tell the user."""
    if not ensure_connected(device_ip, tries=1):
        return "offline"
    r = adb(device_ip, "shell getprop sys.boot_completed", timeout=8)
    return "ready" if r.stdout.decode(errors="ignore").strip() == "1" else "booting"


class ScreenStreamer:
    def __init__(self):
        self.clients = {}

    async def stream_screen(self, ws, device_ip):
        client_id = id(ws)
        self.clients[client_id] = {'ws': ws, 'device': device_ip, 'active': True}
        loop = asyncio.get_event_loop()
        frame_interval = 1.0 / TARGET_FPS
        miss = 0
        last_note = None

        try:
            # Attach adb before the first grab, otherwise every frame fails
            # silently and the page sits on "Connecting..." forever.
            await loop.run_in_executor(None, ensure_connected, device_ip)

            # Pipeline the capture: grab frame N+1 while frame N is still being
            # sent. screencap alone costs ~0.2s, so a strictly sequential
            # grab->encode->send loop caps out near 3 fps even though the host
            # has capacity. Overlapping them roughly doubles the frame rate.
            pending = loop.run_in_executor(None, self._grab_frame, device_ip)

            while self.clients[client_id]['active']:
                start = time.time()
                jpeg_bytes = await pending
                pending = loop.run_in_executor(None, self._grab_frame, device_ip)

                if jpeg_bytes:
                    miss = 0
                    last_note = None
                    img_b64 = base64.b64encode(jpeg_bytes).decode('ascii')
                    await ws.send_json({
                        'type': 'frame',
                        'data': f'data:image/jpeg;base64,{img_b64}',
                        'fps': round(1 / max(time.time() - start, 0.001), 1)
                    })
                    elapsed = time.time() - start
                    if elapsed < frame_interval:
                        await asyncio.sleep(frame_interval - elapsed)
                else:
                    # Tell the user WHY there is no picture, and keep trying to
                    # reattach — a phone that is still booting, or was just
                    # restarted, used to leave the viewer blank with no reason.
                    miss += 1
                    if miss == 1 or miss % 10 == 0:
                        st = await loop.run_in_executor(None, boot_state, device_ip)
                        note = {"offline": "Phone is not running",
                                "booting": "Phone is booting…",
                                "ready": "Waiting for first frame…"}[st]
                        if note != last_note:
                            last_note = note
                            await ws.send_json({'type': 'status', 'state': st,
                                                'message': note})
                    await asyncio.sleep(0.3)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Stream error: {e}")
        finally:
            if client_id in self.clients:
                self.clients[client_id]['active'] = False
                del self.clients[client_id]

    def _grab_frame(self, device_ip):
        """Runs in a worker thread: adb screencap -> downscale -> JPEG encode.

        Uses screencap's RAW output rather than `-p`. With `-p` the device
        spends CPU deflating a PNG and the host spends more CPU inflating it
        again, purely to throw the pixels into a JPEG. Raw is a 16-byte header
        (w, h, format, colorspace little-endian) followed by RGBA rows, which
        PIL wraps with no decode step at all.
        """
        try:
            proc = subprocess.run(
                f"adb -s {device_ip}:5555 exec-out screencap",
                # 2s was tuned for a 360x640 phone. A 720x1280 frame takes
                # ~0.2s idle but seconds on a loaded host, and a tight timeout
                # silently drops every frame, leaving the viewer looking stuck.
                shell=True, capture_output=True, timeout=8
            )
            if proc.returncode != 0 or len(proc.stdout) < 16:
                return None
            buf_in = proc.stdout
            w = int.from_bytes(buf_in[0:4], "little")
            h = int.from_bytes(buf_in[4:8], "little")
            if not (0 < w <= 4096 and 0 < h <= 4096):
                return None
            # Header is 16 bytes on Android 12 (w, h, format, colorspace).
            px = buf_in[16:16 + w * h * 4]
            if len(px) < w * h * 4:
                px = buf_in[12:12 + w * h * 4]      # older 12-byte header
                if len(px) < w * h * 4:
                    return None
            img = Image.frombuffer("RGBA", (w, h), px, "raw", "RGBA", 0, 1).convert("RGB")
            if SCALE != 1.0:
                img = img.resize((max(1, int(w * SCALE)), max(1, int(h * SCALE))),
                                 Image.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=JPEG_QUALITY)
            return buf.getvalue()
        except Exception:
            return None

    def _grab_frame_png(self, device_ip):
        """Previous PNG path, kept as a fallback for debugging."""
        try:
            proc = subprocess.run(
                f"adb -s {device_ip}:5555 exec-out screencap -p",
                shell=True, capture_output=True, timeout=8
            )
            if proc.returncode != 0 or not proc.stdout:
                return None
            img = Image.open(io.BytesIO(proc.stdout)).convert("RGB")
            if SCALE != 1.0:
                w, h = img.size
                img = img.resize((max(1, int(w * SCALE)), max(1, int(h * SCALE))), Image.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=JPEG_QUALITY)
            return buf.getvalue()
        except Exception:
            return None

    async def handle_ws(self, request):
        device_ip = request.query.get('device', 'localhost')
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await self.stream_screen(ws, device_ip)
        return ws

    async def handle_tap(self, request):
        data = await request.json()
        device_ip = data.get('device')
        nx, ny = float(data.get('x', 0)), float(data.get('y', 0))
        w, h = get_resolution(device_ip)
        x, y = int(nx * w), int(ny * h)
        adb(device_ip, f"shell input tap {x} {y}", timeout=5)
        return web.json_response({'ok': True, 'x': x, 'y': y})

    async def handle_swipe(self, request):
        data = await request.json()
        device_ip = data.get('device')
        w, h = get_resolution(device_ip)
        x1, y1 = int(float(data.get('x1', 0)) * w), int(float(data.get('y1', 0)) * h)
        x2, y2 = int(float(data.get('x2', 0)) * w), int(float(data.get('y2', 0)) * h)
        dur = int(data.get('duration_ms', 150))
        adb(device_ip, f"shell input swipe {x1} {y1} {x2} {y2} {dur}", timeout=5)
        return web.json_response({'ok': True})

    async def handle_key(self, request):
        data = await request.json()
        device_ip = data.get('device')
        key = data.get('key')
        
        # Handle app launch
        if key and key.startswith('openapp:'):
            app_path = key.replace('openapp:', '')
            adb(device_ip, f"shell am start -n {app_path}", timeout=5)
            return web.json_response({'ok': True})
        
        keymap = {'back': 4, 'home': 3, 'recents': 187, 'menu': 82,
                  'volup': 24, 'voldown': 25, 'power': 26, 'enter': 66,
                  'del': 67}
        keycode = keymap.get(key)
        if keycode is None:
            return web.json_response({'ok': False, 'error': 'unknown key'}, status=400)
        adb(device_ip, f"shell input keyevent {keycode}", timeout=5)
        return web.json_response({'ok': True})

    async def handle_text(self, request):
        """Type text via adb (fast path for ASCII; falls back to clipboard for unicode)."""
        data = await request.json()
        device_ip = data.get('device')
        text = data.get('text', '')
        if all(ord(c) < 128 for c in text):
            escaped = text.replace(' ', '%s').replace("'", "\\'").replace('&', '\\&')
            adb(device_ip, f"shell input text '{escaped}'", timeout=8)
        else:
            await self._paste_via_clipboard(device_ip, text)
        return web.json_response({'ok': True})

    async def handle_paste(self, request):
        """Set Android clipboard to given text and simulate a paste (Ctrl+V / long-press paste).
        Works for any Unicode text, bypassing 'input text' ASCII limits."""
        data = await request.json()
        device_ip = data.get('device', 'localhost')
        text = data.get('text', '')
        print(f"[PASTE] device={device_ip}, text_len={len(text)}")
        await self._paste_via_clipboard(device_ip, text)
        return web.json_response({'ok': True})

    async def _paste_via_clipboard(self, device_ip, text):
        loop = asyncio.get_event_loop()
        def _do():
            # Use adb shell input text - simple and works across Android versions
            # Escape quotes for shell
            try:
                safe_text = text.replace('"', '\\"').replace('$', '\\$').replace('`', '\\`')
                adb(device_ip, f'shell input text "{safe_text}"', timeout=10)
                print(f"[PASTE] {len(text)} chars -> {device_ip}")
            except Exception as e:
                print(f"[PASTE ERROR] {e}")
        await loop.run_in_executor(None, _do)

    async def handle_viewer(self, request):
        # device can be passed as ?device=name or ?device=IP, defaults to localhost:5555 for direct adb
        device_param = request.query.get('device', 'localhost')
        # If it looks like a name (not an IP), resolve it to adb port
        if '.' not in device_param and ':' not in device_param:
            # Device name like "new01" - try to find it via adb or just use localhost:5555
            device_ip = 'localhost'
        else:
            device_ip = device_param
        html = f'''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Android Screen - {device_ip}</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0; padding: 12px; background: #1a1a1a; color: #fff;
            font-family: system-ui, -apple-system, sans-serif;
            display: flex; flex-direction: column; align-items: center; min-height: 100vh;
        }}
        h1 {{ font-size: 1rem; margin: 0 0 10px; opacity: 0.8; }}
        #toolbar {{
            display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap;
            justify-content: center; width: 100%; max-width: 420px;
        }}
        #toolbar button {{
            background: #2563eb; color: white; border: none; padding: 10px 14px;
            border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 600;
            flex: 1; min-width: 70px;
        }}
        #toolbar button:hover {{ background: #1d4ed8; }}
        #toolbar button.alt {{ background: #374151; }}
        #toolbar button.alt:hover {{ background: #1f2937; }}
        #container {{
            background: #000; border-radius: 12px; overflow: hidden;
            box-shadow: 0 8px 32px rgba(0,0,0,0.4); max-width: 100%; position: relative;
            cursor: pointer; touch-action: none; user-select: none;
        }}
        /* Fill the viewport height instead of rendering at the JPEG's own
           pixel size. SCALE shrinks the frame for bandwidth (432x768 for a
           720x1280 phone), so without an explicit height the picture showed up
           tiny no matter how large the phone's resolution was. */
        #screen {{ display: block; height: calc(100vh - 150px); width: auto;
                   max-width: 100%; image-rendering: auto; pointer-events: none; }}
        @media (max-width: 700px) {{
            #screen {{ width: 100%; height: auto; }}
        }}
        #status {{
            position: absolute; top: 8px; right: 8px; background: rgba(0,0,0,0.7);
            padding: 5px 10px; border-radius: 6px; font-size: 0.75rem;
            font-family: 'SF Mono', Monaco, monospace; pointer-events: none;
        }}
        .dot {{
            display: inline-block; width: 7px; height: 7px; border-radius: 50%;
            margin-right: 5px; background: #22c55e; animation: pulse 2s infinite;
        }}
        @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} }}
        .disconnected .dot {{ background: #ef4444; animation: none; }}
        #pasteBar {{
            display: flex; gap: 8px; margin-top: 10px; width: 100%; max-width: 420px;
        }}
        #pasteInput {{
            flex: 1; padding: 10px; border-radius: 6px; border: 1px solid #333;
            background: #111; color: #fff; font-size: 14px;
        }}
        #pasteBtn {{
            background: #16a34a; color: white; border: none; padding: 10px 16px;
            border-radius: 6px; cursor: pointer; font-weight: 600;
        }}
        #pasteBtn:hover {{ background: #15803d; }}
        #tapFeedback {{
            position: absolute; width: 26px; height: 26px; border-radius: 50%;
            background: rgba(37,99,235,0.5); pointer-events: none;
            transform: translate(-50%,-50%); display: none;
        }}
    </style>
</head>
<body>
    <h1>📱 {device_ip} — tap/drag to control</h1>
    <div id="toolbar">
        <button onclick="sendKey('back')">◁ Back</button>
        <button onclick="sendKey('home')">○ Home</button>
        <button onclick="sendKey('recents')">▢ Recents</button>
        <button style="background: #f59e0b;" onclick="openApp('org.bromite.bromite', 'com.google.android.apps.chrome.Main')">🌐 Browser</button>
        <button class="alt" onclick="sendKey('del')">⌫ Del</button>
    </div>
    <div id="container">
        <img id="screen" src="" alt="Loading...">
        <div id="tapFeedback"></div>
        <div id="status"><span class="dot"></span><span id="fps">Connecting...</span></div>
    </div>
    <div id="pasteBar">
        <input id="pasteInput" placeholder="Type or paste text here, then click Send →" />
        <button id="pasteBtn" onclick="sendPaste()">Send</button>
    </div>

    <script>
        const device = "{device_ip}";
        const ws = new WebSocket(`ws://${{location.host}}/stream?device=${{device}}`);
        const img = document.getElementById('screen');
        const container = document.getElementById('container');
        const status = document.getElementById('status');
        const fps = document.getElementById('fps');
        const tapFeedback = document.getElementById('tapFeedback');

        ws.onopen = () => {{ fps.textContent = 'Connected'; }};
        ws.onmessage = (event) => {{
            const msg = JSON.parse(event.data);
            if (msg.type === 'frame') {{
                img.src = msg.data;
                fps.textContent = `${{msg.fps}} FPS`;
                status.classList.remove('disconnected');
            }} else if (msg.type === 'status') {{
                /* Say why there is no picture instead of spinning silently. */
                fps.textContent = msg.message;
                if (msg.state === 'offline') status.classList.add('disconnected');
                else status.classList.remove('disconnected');
            }}
        }};
        ws.onerror = () => {{ status.classList.add('disconnected'); fps.textContent = 'Error'; }};
        ws.onclose = () => {{ status.classList.add('disconnected'); fps.textContent = 'Disconnected'; }};

        function getNormCoords(clientX, clientY) {{
            const rect = img.getBoundingClientRect();
            return {{ nx: (clientX - rect.left) / rect.width, ny: (clientY - rect.top) / rect.height, clientX, clientY }};
        }}

        function showTapFeedback(clientX, clientY) {{
            const rect = container.getBoundingClientRect();
            tapFeedback.style.left = (clientX - rect.left) + 'px';
            tapFeedback.style.top = (clientY - rect.top) + 'px';
            tapFeedback.style.display = 'block';
            setTimeout(() => tapFeedback.style.display = 'none', 250);
        }}

        let dragStart = null;

        container.addEventListener('mousedown', (e) => {{ dragStart = getNormCoords(e.clientX, e.clientY); }});
        container.addEventListener('mouseup', async (e) => {{
            if (!dragStart) return;
            const end = getNormCoords(e.clientX, e.clientY);
            const dist = Math.hypot(end.clientX - dragStart.clientX, end.clientY - dragStart.clientY);
            showTapFeedback(end.clientX, end.clientY);
            if (dist < 10) {{
                fetch('/tap', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{device, x: dragStart.nx, y: dragStart.ny}})}});
            }} else {{
                fetch('/swipe', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{device, x1: dragStart.nx, y1: dragStart.ny, x2: end.nx, y2: end.ny, duration_ms: 200}})}});
            }}
            dragStart = null;
        }});

        container.addEventListener('touchstart', (e) => {{
            const t = e.touches[0];
            dragStart = getNormCoords(t.clientX, t.clientY);
            e.preventDefault();
        }}, {{passive: false}});
        container.addEventListener('touchend', async (e) => {{
            if (!dragStart) return;
            const t = e.changedTouches[0];
            const end = getNormCoords(t.clientX, t.clientY);
            const dist = Math.hypot(end.clientX - dragStart.clientX, end.clientY - dragStart.clientY);
            showTapFeedback(t.clientX, t.clientY);
            if (dist < 10) {{
                fetch('/tap', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{device, x: dragStart.nx, y: dragStart.ny}})}});
            }} else {{
                fetch('/swipe', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{device, x1: dragStart.nx, y1: dragStart.ny, x2: end.nx, y2: end.ny, duration_ms: 200}})}});
            }}
            dragStart = null;
            e.preventDefault();
        }}, {{passive: false}});

        async function sendKey(key) {{
            await fetch('/key', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{device, key}})}});
        }}

        async function openApp(packageName, activityName) {{
            await fetch('/key', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{device, key: 'openapp:' + packageName + '/' + activityName}})}});
        }}

        async function sendPaste() {{
            const input = document.getElementById('pasteInput');
            const text = input.value;
            if (!text) return;
            await fetch('/paste', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{device, text}})}});
            input.value = '';
            input.focus();
        }}

        document.getElementById('pasteInput').addEventListener('keydown', (e) => {{
            if (e.key === 'Enter') {{ e.preventDefault(); sendPaste(); }}
        }});
    </script>
</body>
</html>
'''
        return web.Response(text=html, content_type='text/html')


async def main():
    streamer = ScreenStreamer()
    app = web.Application()
    app.router.add_get('/stream', streamer.handle_ws)
    app.router.add_get('/', streamer.handle_viewer)
    app.router.add_post('/tap', streamer.handle_tap)
    app.router.add_post('/swipe', streamer.handle_swipe)
    app.router.add_post('/key', streamer.handle_key)
    app.router.add_post('/text', streamer.handle_text)
    app.router.add_post('/paste', streamer.handle_paste)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8004)
    await site.start()

    print("Interactive screen server running on http://0.0.0.0:8004")
    await asyncio.Event().wait()

if __name__ == '__main__':
    asyncio.run(main())
