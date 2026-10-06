# Redroid browser screen viewer

## Purpose

`screen_stream.py` provides a browser viewer and touch control for one redroid
container at a time. It serves the page on port 8004 through the HTTPS tunnel:

```text
https://phone.yklein89openyk.win/?device=172.17.0.3
```

The live service is `screen-stream.service` and runs
`/root/screen_stream.py`. The tracked source is
`scripts/screen_stream.py`.

## Touch and clipboard behavior

- Normal tap on the image calls `/tap` with normalized coordinates.
- Drag calls `/swipe`.
- Text typed on the user's real phone keyboard is captured by the hidden input
  and sent to Android; no visible typing bar is required.
- One-finger long-press is forwarded to Android as a 700 ms same-point swipe;
  Android then owns its native selection/Copy/Paste menu and clipboard.
- The page must be HTTPS. The browser may request clipboard permission once
  for transfers between the outside device and Android.

## Clipboard gestures

All three clipboard directions are available without a visible toolbar:

- **Android → Android:** hold one finger on text or an editable field. The
  viewer sends a 700 ms same-point Android touch, so Android itself opens its
  native selection/Copy/Paste menu and uses Android's own clipboard.
- **Outside → Android:** on mobile, tap the screen with two fingers; on a
  desktop, use `Ctrl+V` while the viewer page is open. This sends the user's
  browser/OS clipboard to Android.
- **Android → outside:** on mobile, hold two fingers on the screen; on a
  desktop, use `Ctrl+Shift+C`. The viewer reads Android clipboard through CDP
  and writes it to the user's browser/OS clipboard.

The two-finger gestures deliberately do not get forwarded to Android, so they
cannot collide with normal single-finger touch, drag, or long-press behavior.
A browser may request clipboard permission once for outside transfers.

## Copy from Android implementation

`/copy` uses Chrome DevTools Protocol through an ADB forward. The local port
is deterministic:

```text
9222 + (sum of the device IP octets modulo 100)
```

Do not use Python's built-in `hash(device_ip)`: Python randomizes its hash seed
between processes, so every service restart creates a different forward and
leaves stale forwards behind. `_cdp_port()` recreates the forward on every
request so stale mappings self-heal.

Android background clipboard restrictions mean copy must be verified with a
known round trip. A successful HTTP 200 response alone is not proof that text
was copied.

## Aspect ratio

The Android display is `360x640`, ratio `9:16`. The mobile CSS uses
`object-fit: contain` and does not force an unrelated height/width pair. The
live browser check measured matching image and container ratios:

```text
image 271.6875 x 483 = 0.5625
container 271.6875 x 483 = 0.5625
```

The JPEG is intentionally smaller (`270x480`) for transport; CSS scales it
without distortion.

## Performance measurements and tuning

Current viewer settings:

```python
SCALE = 0.75
JPEG_QUALITY = 65
TARGET_FPS = 24  # upper bound, not guaranteed output
```

The actual limit is device capture, not the loop target. Measurements on the
360x640 redroid device:

- raw `adb exec-out screencap`: median about 104 ms per frame
- live WebSocket stream: 55 frames in 6 seconds, about 9.17 FPS
- host: 3 CPUs, low load during the test

Do not add parallel `screencap` workers: that increases ADB/device contention
and can deliver old frames out of order. A true high-refresh viewer requires a
persistent video path such as scrcpy/H.264/WebRTC; changing JPEG FPS alone
cannot overcome the ADB capture limit.

Android-side tuning applied to `new01` without restarting it:

```text
memory       2 GiB
memory+swap  3 GiB
cpu-shares   1024
wm size      360x640
wm density   180
animations   window=0, transition=0, animator=0
```

`new01` had been close to its previous 1.5 GiB memory limit and was using swap.
The larger limit removes that pressure. CPU was not the bottleneck: the
container was idle at roughly 2% CPU during testing.

`cpm.py` defaults must match `360x640@180`; otherwise a future dashboard start
can reapply a larger screen and undo the fast configuration.

## Verification

Run the following after changing the viewer:

```bash
python3 -m py_compile scripts/screen_stream.py
systemctl is-active screen-stream
curl -s -o /dev/null -w '%{http_code}\n' \
  'https://phone.yklein89openyk.win/?device=172.17.0.3'
adb -s 172.17.0.3:5555 shell wm size
adb -s 172.17.0.3:5555 shell wm density
```

Verify the live stream itself, not only HTTP:

```bash
/root/venv-stream/bin/python3 - <<'PY'
import json, time, websocket
ws = websocket.create_connection(
    'ws://127.0.0.1:8004/stream?device=172.17.0.3',
    timeout=8, suppress_origin=True)
end = time.monotonic() + 5
frames = 0
while time.monotonic() < end:
    if json.loads(ws.recv()).get('type') == 'frame':
        frames += 1
ws.close()
print('frames:', frames)
PY
```

A copy/paste claim is complete only after a known text round trip, not after
an endpoint returns `{"ok": true}`.
