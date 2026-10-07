# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = [
#   "pillow==12.3.0",
#   "numpy==2.5.3",
#   "pyobjc-framework-Quartz==12.2.2",
#   "pyobjc-framework-ApplicationServices==12.2.2",
#   "pyobjc-framework-Cocoa==12.2.2",
# ]
# [tool.uv]
# exclude-newer = "2026-10-07T00:00:00Z"
# ///
"""point-and-speak helper (macOS).

Runs beside Claude Code. It keeps the last seconds of mouse movement, notices when the
microphone is in use (so it knows when the built-in voice dictation starts and stops, with
no microphone permission), and while you speak it keeps a few screen frames and reads what
is under the pointer when it rests. When the mod asks, it turns that into a small context
folder and a text block for the model.

  uv run --script pointer_helper.py            serve (prints {"port": N} on stdout)
  uv run --script pointer_helper.py --selftest check permissions and each sensor, then exit
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from lookbundle import DwellSnapshot, Recording, build_bundle, remove_bundle, sweep, valid_bundle_dir, valid_root
from lookcore import Sample
from lookimg import KeyframeKeeper

TRAIL_S = 25.0  # seconds of mouse trail kept
SAMPLE_HZ = 60.0
FRAME_HZ = 2.0  # screen frames per second, only while the microphone is in use
MAX_RECORDING_S = 40.0  # stop capturing frames past this, so a long call costs nothing
DWELL_S, DWELL_PX = 0.35, 12.0
LIVE_READ_S, LIVE_READ_PX = 0.3, 20.0  # element read while the pointer moves
MAX_SNAPSHOTS = 150
SECRET_HINT = re.compile(r"pass(word|code|phrase)|secret|token|api.?key|card number|cvv|security code|\bssn\b|private key", re.I)


# ---------- sensors (macOS) ----------

def _quartz():
    import Quartz

    return Quartz


def pointer() -> tuple[float, float, bool]:
    Q = _quartz()
    ev = Q.CGEventCreate(None)
    p = Q.CGEventGetLocation(ev)
    down = bool(Q.CGEventSourceButtonState(Q.kCGEventSourceStateCombinedSessionState, 0))
    return p.x, p.y, down


def display_under(x: float, y: float):
    """(display id, left, top, width, height in points) for the display holding the point."""
    Q = _quartz()
    err, ids, n = Q.CGGetDisplaysWithPoint(Q.CGPoint(x, y), 4, None, None)
    did = ids[0] if n and ids else Q.CGMainDisplayID()
    b = Q.CGDisplayBounds(did)
    return did, b.origin.x, b.origin.y, b.size.width, b.size.height


def screen_access() -> bool:
    return bool(_quartz().CGPreflightScreenCaptureAccess())


def _grab_inprocess(display_id: int) -> Image.Image | None:
    """CGDisplayCreateImage. Apple has been retiring the older capture calls, so this may be missing or return None."""
    try:
        Q = _quartz()
        img = Q.CGDisplayCreateImage(display_id)
        if img is None:
            return None
        w, h = Q.CGImageGetWidth(img), Q.CGImageGetHeight(img)
        bpr = Q.CGImageGetBytesPerRow(img)
        data = Q.CGDataProviderCopyData(Q.CGImageGetDataProvider(img))
        return Image.frombuffer("RGBA", (w, h), bytes(data), "raw", "BGRA", bpr, 1).convert("RGB")
    except Exception:
        return None


def _grab_cli(left: float, top: float, w: float, h: float) -> Image.Image | None:
    """The `screencapture` tool, for one screen rectangle in points. Slower, but it is Apple's supported route."""
    import os
    import subprocess
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".png", prefix="lookshot-")
    os.close(fd)
    try:
        r = subprocess.run(
            ["screencapture", "-x", "-t", "png", f"-R{int(left)},{int(top)},{int(w)},{int(h)}", path],
            capture_output=True,
            timeout=5,
        )
        if r.returncode != 0 or os.path.getsize(path) == 0:
            return None
        with Image.open(path) as im:
            return im.convert("RGB")
    except Exception:
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def grab(disp: tuple[int, float, float, float, float]) -> Image.Image | None:
    """One screenshot of a display as an RGB image, or None without Screen Recording permission.

    The permission is checked first, because without it `screencapture` still returns an image
    that leaves out other apps' windows, which would mislead the model.
    """
    if not screen_access():
        return None
    did, left, top, w, h = disp
    return _grab_inprocess(did) or _grab_cli(left, top, w, h)


def ax_trusted() -> bool:
    from ApplicationServices import AXIsProcessTrusted

    return bool(AXIsProcessTrusted())


def element_at(x: float, y: float) -> dict | None:
    """What is under the pointer, from the accessibility tree. Never reads a password field."""
    try:
        from AppKit import NSRunningApplication
        from ApplicationServices import (
            AXUIElementCopyAttributeValue,
            AXUIElementCopyElementAtPosition,
            AXUIElementCreateSystemWide,
            AXUIElementGetPid,
            AXUIElementSetMessagingTimeout,
        )

        if not ax_trusted():
            return None
        sysw = AXUIElementCreateSystemWide()
        AXUIElementSetMessagingTimeout(sysw, 0.25)
        err, el = AXUIElementCopyElementAtPosition(sysw, x, y, None)
        if err != 0 or el is None:
            return None
        AXUIElementSetMessagingTimeout(el, 0.25)

        def attr(e, name):
            e2, v = AXUIElementCopyAttributeValue(e, name, None)
            return v if e2 == 0 else None

        role, sub = attr(el, "AXRole"), attr(el, "AXSubrole")
        secure = "Secure" in str(role or "") or "Secure" in str(sub or "")
        info: dict = {"role": str(role) if role else None}
        for key, name in (("title", "AXTitle"), ("description", "AXDescription")):
            v = attr(el, name)
            if v:
                info[key] = str(v)[:160]
        hint = " ".join(str(info.get(k, "")) for k in ("title", "description")) + " " + str(attr(el, "AXPlaceholderValue") or "")
        if SECRET_HINT.search(hint):
            secure = True
        if not secure:
            v = attr(el, "AXValue")
            if isinstance(v, str) and v:
                info["value"] = v[:200]
        win = attr(el, "AXWindow")
        if win is not None:
            t = attr(win, "AXTitle")
            if t:
                info["window"] = str(t)[:120]
        err, pid = AXUIElementGetPid(el, None)
        if err == 0:
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app is not None:
                info["app"] = str(app.localizedName())
        url = attr(el, "AXURL")
        if url is not None:
            info["url"] = str(url)[:200]
        return {k: v for k, v in info.items() if v}
    except Exception:
        return None


# ---------- microphone in use (CoreAudio, no permission) ----------

class _Addr(ctypes.Structure):
    _fields_ = [("sel", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("elem", ctypes.c_uint32)]


class MicFlag:
    """True while any input device is in use by any process."""

    def __init__(self):
        self.ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
        self._devs: list[int] = []
        self._at = 0.0

    @staticmethod
    def _fc(s: str) -> int:
        return int.from_bytes(s.encode(), "big")

    def _addr(self, sel: str, scope: str = "glob") -> _Addr:
        return _Addr(self._fc(sel), self._fc(scope), 0)

    def _inputs(self) -> list[int]:
        a, n = self._addr("dev#"), ctypes.c_uint32()
        self.ca.AudioObjectGetPropertyDataSize(1, ctypes.byref(a), 0, None, ctypes.byref(n))
        arr = (ctypes.c_uint32 * (n.value // 4))()
        self.ca.AudioObjectGetPropertyData(1, ctypes.byref(a), 0, None, ctypes.byref(n), arr)
        out = []
        for d in arr:
            sa, sn = self._addr("stm#", "inpt"), ctypes.c_uint32()
            self.ca.AudioObjectGetPropertyDataSize(d, ctypes.byref(sa), 0, None, ctypes.byref(sn))
            if sn.value > 0:
                out.append(int(d))
        return out

    def is_on(self) -> bool:
        if time.time() - self._at > 5.0:  # devices come and go (headsets)
            self._devs, self._at = self._inputs(), time.time()
        for d in self._devs:
            a, v, n = self._addr("gone"), ctypes.c_uint32(), ctypes.c_uint32(4)
            if self.ca.AudioObjectGetPropertyData(d, ctypes.byref(a), 0, None, ctypes.byref(n), ctypes.byref(v)) == 0 and v.value:
                return True
        return False


# ---------- the daemon ----------

class Watcher:
    def __init__(self):
        self.trail: deque[tuple[float, float, float, bool]] = deque()  # epoch, x, y, down
        self.recent: deque[Recording] = deque(maxlen=6)
        self.mic = MicFlag()
        self.lock = threading.Lock()
        self._live: dict | None = None
        self.stop = threading.Event()
        self.enabled = False  # nothing is sampled, captured or read until the mod switches the helper on
        self.screenshots = True
        self.allowed_root: Path | None = None  # the one lookat folder this helper may write to or delete from

    def configure(self, on: bool, screenshots: bool, root: Path) -> None:
        self.allowed_root = root
        self.screenshots = screenshots
        self.enabled = on
        if not on:
            with self.lock:
                self.trail.clear()
                self.recent.clear()
            self._live = None

    # mouse trail
    def _sample_loop(self):
        step = 1.0 / SAMPLE_HZ
        while not self.stop.is_set():
            now = time.time()
            if not self.enabled:
                time.sleep(0.25)
                continue
            try:
                x, y, down = pointer()
                with self.lock:
                    self.trail.append((now, x, y, down))
                    while self.trail and now - self.trail[0][0] > TRAIL_S:
                        self.trail.popleft()
            except Exception:
                pass
            time.sleep(max(0.0, step - (time.time() - now)))

    def window_samples(self, t0: float, t1: float) -> list[tuple[float, float, float, bool]]:
        with self.lock:
            return [p for p in self.trail if t0 - 0.05 <= p[0] <= t1 + 0.05]

    # recording while the microphone is in use
    def _watch_loop(self):
        while not self.stop.is_set():
            if not self.enabled:
                self._live = None
                time.sleep(0.25)
                continue
            try:
                on = self.mic.is_on()
            except Exception:
                on = False
            now = time.time()
            if on and self._live is None:
                self._live = {"t0": now, "keeper": KeyframeKeeper(limit=3), "snaps": [], "last_frame": 0.0, "dwell_key": None, "disp": None, "last_read": None}
            if self._live is not None:
                self._tick(self._live, now)
                if not on:
                    self._finish(self._live, now)
                    self._live = None
            time.sleep(0.1)

    def _tick(self, live: dict, now: float):
        if now - live["t0"] > MAX_RECORDING_S:
            return
        x, y, _ = pointer()
        did, left, top, w, h = display_under(x, y)
        if self.screenshots and now - live["last_frame"] >= 1.0 / FRAME_HZ:
            live["last_frame"] = now
            img = grab((did, left, top, w, h))
            if img is not None:
                live["disp"] = (did, left, top, w, h)
                live["keeper"].offer(now - live["t0"], img)
                live["img"] = img
        # while the pointer moves: read what is under it about three times a second,
        # so a "this" said in passing still has an element to point at
        last = live.get("last_read")
        if last is None or (now - last[0] >= LIVE_READ_S and ((x - last[1]) ** 2 + (y - last[2]) ** 2) ** 0.5 >= LIVE_READ_PX):
            if len(live["snaps"]) < MAX_SNAPSHOTS:
                live["last_read"] = (now, x, y)
                live["snaps"].append(DwellSnapshot(now - live["t0"], x - left, y - top, element_at(x, y), None))
        # a dwell: the pointer rested here long enough, so read what is under it now
        recent = self.window_samples(now - DWELL_S, now)
        if len(recent) >= 5 and recent[-1][0] - recent[0][0] >= DWELL_S * 0.9:
            if all(((p[1] - x) ** 2 + (p[2] - y) ** 2) ** 0.5 <= DWELL_PX for p in recent):
                key = (round(x / 20), round(y / 20))
                if live["dwell_key"] != key:
                    live["dwell_key"] = key
                    live["snaps"].append(DwellSnapshot(now - live["t0"], x - left, y - top, element_at(x, y), None))
            else:
                live["dwell_key"] = None

    def _finish(self, live: dict, now: float):
        disp = live.get("disp") or display_under(*pointer()[:2])
        _, left, top, w, h = disp
        t0 = live["t0"]
        samples = [Sample(p[0] - t0, p[1] - left, p[2] - top, p[3]) for p in self.window_samples(t0, now)]
        rec = Recording(t0, now, samples, list(live["keeper"].frames), live["snaps"], (w, h))
        with self.lock:
            self.recent.append(rec)

    # a recording for a submitted prompt
    def recording_for(self, submit_at: float, max_age: float = 20.0) -> Recording | None:
        with self.lock:
            cands = [r for r in self.recent if submit_at - max_age <= r.t_end <= submit_at + 2.0 and r.t_end - r.t_start >= 0.4]
        return max(cands, key=lambda r: r.t_end) if cands else None

    def typed_recording(self, submit_at: float, seconds: float = 6.0) -> Recording | None:
        """For a prompt typed while hovering: the last few seconds of trail and one screenshot now."""
        x, y, _ = pointer()
        did, left, top, w, h = display_under(x, y)
        pts = self.window_samples(submit_at - seconds, submit_at)
        if len(pts) < 2:
            return None
        t0 = pts[0][0]
        samples = [Sample(p[0] - t0, p[1] - left, p[2] - top, p[3]) for p in pts]
        img = grab((did, left, top, w, h)) if self.screenshots else None
        frames = [(samples[-1].t, img)] if img is not None else []
        return Recording(t0, pts[-1][0], samples, frames, [], (w, h))

    def _orphan_loop(self):
        """Exit when Claude Code is gone, so the helper never keeps capturing with nobody to ask for it."""
        while not self.stop.wait(5.0):
            if _orphaned():
                os._exit(0)

    def start(self):
        threading.Thread(target=self._sample_loop, daemon=True).start()
        threading.Thread(target=self._watch_loop, daemon=True).start()
        threading.Thread(target=self._orphan_loop, daemon=True).start()


def _orphaned() -> bool:
    """True when the process that started us, or the one above it (`uv`), has been adopted by launchd."""
    parent = os.getppid()
    if parent == 1:
        return True
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(parent)], capture_output=True, text=True, timeout=3).stdout.strip()
        return out == "1"
    except Exception:
        return False


MAX_BODY = 64 * 1024


def make_handler(w: Watcher, token: str):
    token_bytes = token.encode()

    class H(BaseHTTPRequestHandler):
        timeout = 10  # a client that stalls cannot hold a thread for ever

        def log_message(self, *_):
            pass

        def _send(self, body: dict, status: int = 200):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self) -> bool:
            """Only the mod may call: it must send the secret token, and the Host must be the loopback address.

            Any web page can send a request to a local port, so the token is what keeps other programs
            and web pages out. The Host check also blocks DNS rebinding.
            """
            host_ok = self.headers.get("host", "") == f"127.0.0.1:{self.server.server_address[1]}"
            sent = self.headers.get("x-look-token", "").encode("utf-8", "replace")
            token_ok = hmac.compare_digest(sent, token_bytes)
            if not (host_ok and token_ok):
                self._send({"ok": False, "error": "forbidden"}, 403)
                return False
            return True

        def _the_root(self, value) -> Path | None:
            """The lookat folder the caller named, but only if it is the one the mod set up."""
            if w.allowed_root is None:
                return None
            root = Path(str(value))
            return root if valid_root(root) and root.resolve() == w.allowed_root.resolve() else None

        def do_GET(self):
            if not self._allowed():
                return
            self._send({"ok": True, "enabled": w.enabled, "screen": screen_access(), "accessibility": ax_trusted(), "mic_in_use": w.mic.is_on(), "recordings": len(w.recent)})

        def do_POST(self):
            if not self._allowed():
                return
            try:
                size = int(self.headers.get("content-length") or 0)
                if size < 0:
                    return self._send({"ok": False, "error": "bad length"}, 400)
                if size > MAX_BODY:
                    return self._send({"ok": False, "error": "too large"}, 413)
                body = json.loads(self.rfile.read(size) or b"{}")
                if self.path == "/enable":
                    root = Path(str(body["root"]))
                    if not valid_root(root):
                        return self._send({"ok": False, "error": "bad root"}, 400)
                    w.configure(bool(body.get("on")), bool(body.get("screenshots", True)), root)
                    return self._send({"ok": True, "enabled": w.enabled})
                if self.path == "/bundle":
                    d = Path(str(body["dir"]))
                    root = self._the_root(d.parent)
                    if root is None or not valid_bundle_dir(d, root):
                        return self._send({"ok": False, "error": "bad dir"}, 400)
                    if not w.enabled:
                        return self._send({"ok": True, "context": "", "dir": None, "files": []})
                    submit_at = float(body.get("submit_at") or time.time())
                    rec = w.recording_for(submit_at) or w.typed_recording(submit_at)
                    if rec is None:
                        return self._send({"ok": True, "context": "", "dir": None, "files": []})
                    out = build_bundle(rec, str(body.get("text", ""))[:20000], d)
                    return self._send({"ok": True, **out})
                if self.path == "/cleanup":
                    d = Path(str(body["dir"]))
                    root = self._the_root(body["root"])
                    if root is None or not valid_bundle_dir(d, root):
                        return self._send({"ok": False, "error": "bad dir"}, 400)
                    gone = remove_bundle(d, root) or not d.exists()
                    return self._send({"ok": gone})
                if self.path == "/sweep":
                    root = self._the_root(body["root"])
                    if root is None:
                        return self._send({"ok": False, "error": "bad root"}, 400)
                    return self._send({"ok": True, "removed": sweep(root, float(body.get("older_s", 3600)), time.time())})
            except (KeyError, ValueError, TypeError, OSError):
                return self._send({"ok": False, "error": "bad request"}, 400)
            self._send({"ok": False}, 404)

    return H


def selftest() -> int:
    ok = True
    x, y, down = pointer()
    print(f"pointer: ({x:.0f},{y:.0f}) button down={down}")
    did, left, top, w, h = display_under(x, y)
    print(f"display under pointer: id {did}, {w:.0f}x{h:.0f} points at ({left:.0f},{top:.0f})")
    print(f"microphone in use right now: {MicFlag().is_on()}")
    s = screen_access()
    print(f"Screen Recording permission: {'granted' if s else 'NOT granted'}")
    if s:
        img = grab((did, left, top, w, h))
        print(f"screenshot: {'%dx%d' % img.size if img else 'failed'}")
        ok &= img is not None
    else:
        ok = False
    a = ax_trusted()
    print(f"Accessibility permission: {'granted' if a else 'NOT granted'}")
    if a:
        print(f"element under pointer: {element_at(x, y)}")
    else:
        ok = False
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    w = Watcher()
    w.start()
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(w, token))
    print(json.dumps({"port": server.server_address[1], "token": token}), flush=True)
    try:
        server.serve_forever()
    finally:
        w.stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
