"""Live check of the helper's local server (macOS). Starts the real helper and tries to misuse it.

  python3 check_server.py

The helper must refuse a request with no token, a wrong token, a token with odd characters and a wrong Host.
It must refuse every file action until the mod names the one folder it may use, and then refuse any other
folder, even one that looks right (`.claude/lookat`). Exit code 0 means every check held.
"""
import http.client
import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
proc = subprocess.Popen(["uv", "run", "--script", str(HERE / "pointer_helper.py")], stdout=subprocess.PIPE, text=True, cwd=HERE)
failed = []


def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        failed.append(name)


try:
    hello = json.loads(proc.stdout.readline())
    port, token = hello["port"], hello["token"]

    def call(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = {"host": f"127.0.0.1:{port}", **(headers or {})}
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        return r.status, (json.loads(data) if data else {})

    good = {"x-look-token": token}
    check("GET with no token is refused", call("GET", "/")[0] == 403)
    check("GET with a wrong token is refused", call("GET", "/", headers={"x-look-token": "nope"})[0] == 403)
    check("GET with a wrong Host is refused", call("GET", "/", headers={**good, "host": "evil.example"})[0] == 403)
    s, r = call("GET", "/", headers=good)
    check("GET with the token works", s == 200)
    check("the helper starts switched off", r.get("enabled") is False)

    # a token with a non-ASCII character used to crash the handler thread
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    raw.sendall(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nx-look-token: café\r\n\r\n".encode("utf-8"))
    check("a token with a non-ASCII character is refused, not a crash", b" 403 " in raw.recv(200))
    raw.close()
    check("... and the server still answers", call("GET", "/", headers=good)[0] == 200)

    base = Path(tempfile.mkdtemp())
    root = base / "proj" / ".claude" / "lookat"
    other = base / "other" / ".claude" / "lookat"
    root.mkdir(parents=True)
    other.mkdir(parents=True)
    mine, theirs, victim = root / "abc-123", other / "abc-123", base / "Documents"
    for d in (mine, theirs, victim):
        d.mkdir(parents=True, exist_ok=True)
    (victim / "keep.txt").write_text("precious")

    s, _ = call("POST", "/cleanup", {"dir": str(mine), "root": str(root)}, good)
    check("no file action works before the mod names its folder", s == 400 and mine.exists())
    s, _ = call("POST", "/enable", {"on": True, "screenshots": True, "root": str(base)}, good)
    check("naming a folder that is not .claude/lookat is refused", s == 400)
    s, r = call("POST", "/enable", {"on": True, "screenshots": False, "root": str(root)}, good)
    check("the mod can switch the helper on with its folder", s == 200 and r.get("enabled") is True)

    s, _ = call("POST", "/cleanup", {"dir": str(victim), "root": str(base)}, good)
    check("cleanup of a folder outside lookat is refused", s == 400 and (victim / "keep.txt").exists())
    s, _ = call("POST", "/cleanup", {"dir": str(theirs), "root": str(other)}, good)
    check("cleanup in ANOTHER well-formed lookat folder is refused", s == 400 and theirs.exists())
    s, _ = call("POST", "/sweep", {"root": str(other), "older_s": 0}, good)
    check("sweep of another lookat folder is refused", s == 400 and theirs.exists())
    s, _ = call("POST", "/bundle", {"text": "x", "dir": str(theirs.parent / "new-111"), "submit_at": 1}, good)
    check("a bundle written into another lookat folder is refused", s == 400 and not (theirs.parent / "new-111").exists())
    s, _ = call("POST", "/cleanup", {"dir": str(mine), "root": str(root)})
    check("cleanup with no token is refused", s == 403 and mine.exists())
    s, r = call("POST", "/cleanup", {"dir": str(mine), "root": str(root)}, good)
    check("cleanup of a real bundle folder works", s == 200 and r.get("ok") and not mine.exists())

    s, r = call("POST", "/enable", {"on": False, "root": str(root)}, good)
    s2, r2 = call("POST", "/bundle", {"text": "this", "dir": str(root / "zzz-999"), "submit_at": 1}, good)
    check("a switched-off helper gives no context", s2 == 200 and r2.get("context") == "")

    s, _ = call("POST", "/cleanup", "not json", good)
    check("a bad request body gets 400, not a crash", s == 400 and call("GET", "/", headers=good)[0] == 200)
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    raw.sendall(f"POST /cleanup HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nx-look-token: {token}\r\ncontent-length: -5\r\n\r\n".encode())
    check("a negative content-length is refused, not a hang", b" 400 " in raw.recv(200))
    raw.close()
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    raw.sendall(f"POST /cleanup HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nx-look-token: {token}\r\ncontent-length: 70000\r\n\r\n".encode())
    check("a body over 64 KB is refused", b" 413 " in raw.recv(200))
    raw.close()
finally:
    proc.terminate()
sys.exit(1 if failed else 0)
