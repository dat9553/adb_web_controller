#!/usr/bin/env python3
# Python 3 standard-library HTTP server for the CPTR ADB controller.
# Only exposes a small whitelist of ADB actions; it does not accept shell commands.

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import shutil

ADB = shutil.which("adb")
HOST = "0.0.0.0"
PORT = 8000
HTML_FILE = Path(__file__).with_name("controller.html")

_selected_device = None
_state_lock = threading.Lock()

# Keep the original adb_swipe.py vertical swipe coordinates.
SWIPES = {
    "up":    (540, 1600, 540, 200),
    "down":  (540, 400, 540, 1600),
    # Horizontal defaults for the same 1080px-wide coordinate space.
    "left":  (900, 960, 180, 960),
    "right": (180, 960, 900, 960),
}
ALLOWED_KEYEVENTS = {3, 4, 187}  # Home, Back, Recent Apps


def run_adb(args, timeout=15):
    command = [ADB] + list(args)
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "ADB not found: " + ADB
    except subprocess.TimeoutExpired:
        return 124, "", "ADB command timed out"


def json_response(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/controller.html", "/touchpad.html"):
            filename = "touchpad.html" if path == "/touchpad.html" else "controller.html"
            target_file = HTML_FILE.with_name(filename)
            try:
                content = target_file.read_bytes()
            except OSError:
                self.send_error(500, filename + " not found beside server.py")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
        elif path == "/health":
            json_response(self, 200, {"ok": True, "message": "Server is running"})
        else:
            self.send_error(404)

    def do_POST(self):
        global _selected_device
        path = urlparse(self.path).path
        if path not in ("/api/connect", "/api/swipe", "/api/tap", "/api/keyevent"):
            json_response(self, 404, {"ok": False, "message": "Unknown endpoint"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                raise ValueError("Invalid request size")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("JSON object required")
        except Exception as exc:
            json_response(self, 400, {"ok": False, "message": "Invalid request: " + str(exc)})
            return

        if path == "/api/connect":
            ip = str(data.get("ip", "")).strip()
            port = str(data.get("port", "")).strip()
            if not ip or any(c.isspace() for c in ip):
                json_response(self, 400, {"ok": False, "message": "Enter a valid IP or ADB serial"})
                return

            # Preserve adb_swipe.py behavior: blank port means use the value
            # directly as the ADB serial; do not append :port or run adb connect.
            if port:
                if not port.isdigit() or not (1 <= int(port) <= 65535):
                    json_response(self, 400, {"ok": False, "message": "Port must be 1–65535"})
                    return
                device = ip + ":" + port
                code, out, err = run_adb(["connect", device], timeout=20)
                if code != 0:
                    json_response(self, 502, {"ok": False, "message": err or out or "adb connect failed"})
                    return
                # adb connect may print "connected" or "already connected".
                if "unable" in (out + " " + err).lower() or "failed" in (out + " " + err).lower():
                    json_response(self, 502, {"ok": False, "message": out or err or "Connection failed"})
                    return
            else:
                device = ip

            code, out, err = run_adb(["devices"], timeout=15)
            if code != 0:
                json_response(self, 502, {"ok": False, "message": err or "Could not list ADB devices"})
                return

            states = {}
            for line in out.splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 2:
                    states[fields[0]] = fields[1]
            if states.get(device) != "device":
                state = states.get(device, "not listed")
                json_response(self, 409, {
                    "ok": False,
                    "message": "Device %s is %s. Check ADB authorization/connection." % (device, state)
                })
                return

            with _state_lock:
                _selected_device = device
            json_response(self, 200, {"ok": True, "message": "Connected: " + device})
            return

        with _state_lock:
            device = _selected_device
        if not device:
            json_response(self, 409, {"ok": False, "message": "Connect to the K30 first"})
            return

        if path == "/api/swipe":
            direction = str(data.get("direction", "")).lower()
            if direction not in SWIPES:
                json_response(self, 400, {"ok": False, "message": "Invalid swipe direction"})
                return
            try:
                duration = int(data.get("duration", 150))
            except (TypeError, ValueError):
                duration = 300
            if not 50 <= duration <= 3000:
                json_response(self, 400, {"ok": False, "message": "Duration must be 50–3000 ms"})
                return
            x1, y1, x2, y2 = SWIPES[direction]
            args = ["-s", device, "shell", "input", "swipe",
                    str(x1), str(y1), str(x2), str(y2), str(duration)]
            label = "Swipe " + direction

        elif path == "/api/tap":
            try:
                x, y = int(data.get("x")), int(data.get("y"))
            except (TypeError, ValueError):
                json_response(self, 400, {"ok": False, "message": "Tap coordinates must be integers"})
                return
            if not (0 <= x <= 10000 and 0 <= y <= 10000):
                json_response(self, 400, {"ok": False, "message": "Tap coordinates out of range"})
                return
            args = ["-s", device, "shell", "input", "tap", str(x), str(y)]
            label = "Tap %d, %d" % (x, y)

        else:  # /api/keyevent
            try:
                keycode = int(data.get("keycode"))
            except (TypeError, ValueError):
                json_response(self, 400, {"ok": False, "message": "Invalid keycode"})
                return
            if keycode not in ALLOWED_KEYEVENTS:
                json_response(self, 400, {"ok": False, "message": "Keyevent not allowed"})
                return
            args = ["-s", device, "shell", "input", "keyevent", str(keycode)]
            label = {3: "Home", 4: "Back", 187: "Recent Apps"}[keycode]

        code, out, err = run_adb(args)
        if code == 0:
            json_response(self, 200, {"ok": True, "message": label + " sent to " + device})
        else:
            json_response(self, 502, {"ok": False, "message": err or out or "ADB command failed"})


if __name__ == "__main__":
    if not Path(ADB).is_file():
        print("WARNING: ADB executable not found at: " + ADB)
        print("Edit ADB in server.py to match your adb.exe location.")
    print("ADB Controller server: http://0.0.0.0:%d/" % PORT)
    print("Open http://<PC-LAN-IP>:%d/ on CPTR on the J2 Prime." % PORT)
    print("Press Ctrl+C to stop.")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
