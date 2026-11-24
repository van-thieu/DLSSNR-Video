#!/usr/bin/env python3
"""Native, always-on-top home for the DLSS5 browser control panel.

When the browser presents through the DLSS5 Vulkan layer, anything painted inside the page is part
of the layer's swapchain. The extension's injected panel therefore gets enhanced along with the
video, side-by-side compare draws two copies of it, and the composition overlay ends up covering
the copy the user tries to click.

This window solves that by hosting the *same* extension UI in its own X11 surface. It is a normal
GTK window (WebKit2GTK renders the panel), so it never goes through a Vulkan swapchain and the
layer cannot enhance, duplicate or cover it. The panel markup and control definitions are not
duplicated: the web view loads the extension's own panel.css / content.js and only replaces the
`chrome.runtime.sendMessage` transport with a call into the native host, which already owns the
shared-memory settings.

Commands arrive on $TMPDIR/dlssnr-video/panel-<uid>.sock from dlssnr_bridge.py: show, hide, toggle,
quit. Launching is lazy -- the bridge starts this process the first time the toolbar toggles the
panel in an enhanced browser.
"""
import json
import os
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# This window must never be enhanced: drop the layer env before any toolkit initialises. Keep
# DLSSNR_SHM, though -- the bridge in this process still needs to reach the video shared memory.
for _k in ("VKLayer_DLSS5", "VK_INSTANCE_LAYERS", "VK_LOADER_DEBUG"):
    os.environ.pop(_k, None)

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gdk, Gtk, WebKit2  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dlssnr_bridge as bridge  # noqa: E402

UID = os.getuid()
PANEL_SOCK = os.path.join(tempfile.gettempdir(), f"dlssnr-video/panel-{UID}.sock")
EXT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOG_DIR = "/tmp/opencode"


def panel_log(*parts):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "dlss5-panel.log"), "a") as f:
            f.write(" ".join(str(p) for p in parts) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- messages
# The native panel is the same content.js, so every verb it sends is a real bridge command. Only
# ping is special: it must tell content.js to render its in-page panel *inside this web view*
# rather than hand control back to a native window (which would be recursive).
def handle_for_panel(msg):
    if msg.get("cmd", "") == "ping":
        return {"ok": True, "version": 1, "shm": bridge.SHM,
                "nativePanel": False, "enhanced": False}
    return bridge.handle(msg)


# --------------------------------------------------------------------------- local HTTP server
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>DLSS5 Controls</title><link rel="stylesheet" href="/panel.css">
<style>
html,body{margin:0;width:100%;height:100%;background:#121216;overflow:hidden}
/* In a page this is a floating 300px div; here it IS the window. Fill the WebKit view and pin the
   position so it can never be dragged out of sight. */
#dlss5-panel{
  position:absolute !important; top:0 !important; left:0 !important; right:0 !important;
  bottom:0 !important; width:100% !important; height:100% !important;
  max-height:100% !important; box-sizing:border-box !important;
  border:0 !important; border-radius:0 !important; box-shadow:none !important;
  overflow-y:auto !important;
}
#dlss5-head{cursor:default !important}
</style>
</head><body>
<script>
// Minimal chrome.runtime shim: the extension's own content.js builds the panel and wires every
// control; route its messages to the native bridge over localhost instead of to a service worker.
window.chrome = { runtime: {
  lastError: undefined,
  onMessage: { _ls: [], addListener(fn){ this._ls.push(fn); } },
  sendMessage(msg, cb){
    fetch('/msg', {method:'POST', headers:{'Content-Type':'application/json'},
                   body: JSON.stringify(msg)})
      .then(r => r.json()).then(r => { if (cb) cb(r); })
      .catch(e => { if (cb) cb({ok:false, error:String(e)}); });
  }
}};
// The extension panel's head is a drag handle in a web page. In this window the GTK title bar does
// the dragging, so swallow that mousedown before the page's handler can move the panel div.
document.addEventListener('mousedown', function(e){
  if (e.target && e.target.closest && e.target.closest('#dlss5-head') &&
      !e.target.closest('button')) e.stopPropagation();
}, true);
// A native panel closing must not suppress enhancement for the video still playing in the browser.
const _addEventListener = window.addEventListener.bind(window);
window.addEventListener = function(type, fn, opt){
  if (type === 'beforeunload' || type === 'pagehide') return;
  return _addEventListener(type, fn, opt);
};
</script>
<script src="/content.js"></script>
<script>
// No <video> in this document, so content.js will not attach its panel on its own. Pin it open.
(window.chrome.runtime.onMessage._ls || []).forEach(fn => {
  try { fn({cmd: 'show_panel'}); } catch (e) { console.error(e); }
});
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, body, ctype, code=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/panel.html" or path == "/":
            self._send(PAGE, "text/html; charset=utf-8")
        elif path in ("/panel.css", "/content.js"):
            name = "panel.css" if path == "/panel.css" else "content.js"
            try:
                with open(os.path.join(EXT_DIR, name), "rb") as f:
                    body = f.read()
                self._send(body, "text/css; charset=utf-8" if name == "panel.css"
                           else "application/javascript; charset=utf-8")
            except OSError as e:
                self._send(str(e), "text/plain", 404)
        else:
            self._send("not found", "text/plain", 404)

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/msg":
            self._send("not found", "text/plain", 404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            msg = json.loads(self.rfile.read(n).decode("utf-8"))
            resp = handle_for_panel(msg)
        except Exception as e:
            resp = {"ok": False, "error": repr(e)}
        self._send(json.dumps(resp), "application/json")


# --------------------------------------------------------------------------- GTK window
class PanelWindow:
    def __init__(self, url):
        self.win = Gtk.Window(title="DLSS5 Controls")
        self.win.set_default_size(312, 780)
        self.win.set_keep_above(True)
        # A dock hint keeps the panel above Chrome even when the video is fullscreen; fullscreen
        # windows normally sit above plain always-on-top ones.
        self.win.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.win.set_resizable(True)
        # Do not steal the keyboard from the video when the panel pops up on its own.
        self.win.set_focus_on_map(False)
        self.win.set_gravity(Gdk.Gravity.NORTH_EAST)
        self.win.connect("destroy", self.on_destroy)

        # Client-side title bar: dock/utility windows are often undecorated by the WM, so keep a
        # drag handle and close button of our own.
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("DLSS5 Controls")
        header.set_decoration_layout("close")
        self.win.set_titlebar(header)

        self.view = WebKit2.WebView()
        self.view.connect("context-menu", lambda *args: True)  # no right-click menu in a control HUD
        settings = self.view.get_settings()
        settings.set_property("enable-developer-extras", bool(os.environ.get("DLSS5_PANEL_DEV")))
        settings.set_property("enable-webgl", False)
        self.view.load_uri(url)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.pack_start(self.view, True, True, 0)
        self.win.add(box)

        screen = Gdk.Screen.get_default()
        x = max(0, screen.get_width() - 312 - 12)
        self.win.move(x, 36)

    def show(self):
        self.win.show_all()
        self.win.present()

    def hide(self):
        self.win.hide()

    def toggle(self):
        if self.win.get_visible():
            self.hide()
        else:
            self.show()

    def on_destroy(self, *args):
        Gtk.main_quit()


def socket_server(win):
    try:
        os.makedirs(os.path.dirname(PANEL_SOCK), exist_ok=True)
        if os.path.exists(PANEL_SOCK):
            os.unlink(PANEL_SOCK)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(PANEL_SOCK)
        srv.listen(8)
    except OSError as e:
        panel_log("socket failed:", e)
        return
    while True:
        try:
            conn, _ = srv.accept()
            with conn:
                data = conn.recv(256).decode("utf-8", "ignore").strip()
            action = data.split(" ", 1)[0]
            panel_log("action:", action)
            if action == "show":
                GLib.idle_add(win.show)
            elif action == "hide":
                GLib.idle_add(win.hide)
            elif action == "toggle":
                GLib.idle_add(win.toggle)
            elif action == "quit":
                GLib.idle_add(Gtk.main_quit)
        except Exception as e:
            panel_log("socket error:", e)


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    panel_log("panel http port", port)

    win = PanelWindow(f"http://127.0.0.1:{port}/panel.html")
    threading.Thread(target=socket_server, args=(win,), daemon=True).start()
    win.show()
    Gtk.main()
    try:
        os.unlink(PANEL_SOCK)
    except OSError:
        pass
    httpd.shutdown()


if __name__ == "__main__":
    main()
