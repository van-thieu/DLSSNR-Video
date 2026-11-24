#!/usr/bin/env python3
"""Native Messaging host for the DLSS5 browser extension.

Speaks Chrome/Firefox native messaging (4-byte little-endian length + JSON) on stdin/stdout and
translates the extension's verbs into the same shared-memory controls the Qt helper uses. `play`
hands a video URL to the running DLSS5 Video player over a local socket, or launches it.
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

UID = os.getuid()
# A confined browser rewrites ~ to its snap home. Always resolve project paths against the real
# user home (overridable with DLSSNR_HOME) so the native host finds the same tools and config.
HOME_DIR = os.environ.get("DLSSNR_HOME", "/home/vthieu")
# tempfile.gettempdir() honours TMPDIR. The Firefox snap has a private /tmp, so its wrapper points
# TMPDIR at a shared, non-hidden directory that the browser, native host, player and panel all see.
TMP = tempfile.gettempdir()
RUNTIME = os.path.join(TMP, "dlssnr-video")
SHM = os.environ.get("DLSSNR_SHM", os.path.join(os.environ.get("DLSSNR_RUNTIME_DIR", os.path.join(HOME_DIR, "dlssnr-runtime")), "shm.bin"))
SHMCTL = os.path.join(HOME_DIR, ".local/bin/dlssnr-video-shmctl")
HELPER_CLI = os.path.join(HOME_DIR, ".local/bin/dlssnr-video-helper")
PLAYER = os.path.join(HOME_DIR, ".local/bin/dlssnr-gui-video")
APP_SOCK = os.path.join(RUNTIME, f"app-{UID}.sock")
PANEL_SOCK = os.path.join(RUNTIME, f"panel-{UID}.sock")
PANEL_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dlssnr_panel.py")
SUPPRESS_FILE = os.path.join(RUNTIME, f"suppress-until-{UID}")
RESTART_FLAG = os.path.join(RUNTIME, f"last-restart-{UID}")
BRIDGE_LOG = os.path.join(TMP, "dlssnr-bridge.log")
ROI_LOG = os.path.join(TMP, "dlssnr-roi.log")
CHROME_CDP = "http://127.0.0.1:9222/json/version"
# Video profiles are deliberately separate from the desktop helper's game profiles. Removing or
# renaming a profile from the browser must never touch a game profile.
PROFILE_DIR = os.path.join(HOME_DIR, ".config/dlssnr-video/dlssnr/profiles")
VIDEO_CONFIG = os.path.join(HOME_DIR, ".config/dlssnr-video/dlssnr/config.ini")
# Shared across the Chrome host, the daemon and the native panel process: each has its own
# active_url global, but restart must reopen the page the user was watching.
LAST_URL_FILE = os.path.join(HOME_DIR, ".config/dlssnr-video/dlssnr/last-url")

# Auto-profile-by-site used to live only in background.js. With the in-page panel gone in an
# enhanced browser, the native host owns it: both the browser's content script and the native panel
# send their verbs here, so there is one source of truth.
AUTO_STATE = os.path.join(HOME_DIR, ".config/dlssnr-video/dlssnr/auto-profiles.json")
HOST_NOISE = {"www", "com", "co", "net", "org", "tv", "cn", "ca", "io", "app", "m"}
active_url = ""
_auto_state = None
_last_auto_host = ""
_last_auto_profile = ""

# shmctl setting name -> the set_* key used by dlssnr_gui profile files.
PROFILE_KEYS = {
    "enabled": "set_enabled", "passes": "set_passes",
    "rebuildms": "set_rebuild_settle_ms", "workingscale": "set_model_resolution",
    "downscaler": "set_down_leg_filter", "detail": "set_detail_strength",
    "colour": "set_colour_strength", "guard": "set_highlight_guard",
    "transfer": "set_enlargement", "bypass": "set_composition_enabled",
    "preset": "set_preset", "style": "set_style", "intensity": "set_intensity",
    "localstructure": "set_local_structure", "localtone": "set_local_tone",
    "skinstructure": "set_skin_structure", "automask": "set_auto_mask",
    "sharpness": "set_sharpness", "mvec": "set_motion_enabled",
    "mvecquality": "set_motion_quality", "mvecunits": "set_motion_units",
    "mvecpixels": "set_mvec_pixels", "colourmode": "set_colour_mode",
    "hdrmode": "set_hdr_mode", "sdr16multipass": "set_sdr_16bit_multipass",
    "whitepointsource": "set_white_point_source", "whitepoint": "set_paper_white",
    "whitepointscale": "set_white_point_scale", "whitepointtrim": "set_white_point_trim",
    "applymodel": "set_apply_model", "reversible": "set_proxy",
    "debugview": "set_debug_view", "debugscale": "set_debug_scale",
    "compare": "set_compare", "comparesplit": "set_compare_split",
    "comparezoom": "set_compare_zoom", "compareswap": "set_compare_swap",
    "togglekey": "set_toggle_key", "hold": "set_hold",
}
KEY_TO_SHM = {v: k for k, v in PROFILE_KEYS.items()}


def host_of(url):
    try:
        h = (urlparse(str(url or "")).hostname or "").lower()
        return re.sub(r"^www\.", "", h)
    except Exception:
        return ""


def norm_name(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def load_auto_state():
    global _auto_state
    if _auto_state is None:
        try:
            with open(AUTO_STATE) as f:
                _auto_state = json.load(f)
        except Exception:
            _auto_state = {}
        _auto_state.setdefault("enabled", True)
        _auto_state.setdefault("sites", {})
    return _auto_state


def save_auto_state():
    try:
        os.makedirs(os.path.dirname(AUTO_STATE), exist_ok=True)
        with open(AUTO_STATE, "w") as f:
            json.dump(load_auto_state(), f, indent=2)
    except Exception:
        pass


def list_profile_names():
    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)
        return sorted(f[:-4] for f in os.listdir(PROFILE_DIR)
                      if f.lower().endswith(".ini") and os.path.isfile(os.path.join(PROFILE_DIR, f)))
    except Exception:
        return []


def apply_profile_file(name):
    """Load a video profile into the shared header. Returns (ok, name-or-error)."""
    try:
        path = os.path.basename(str(name)).strip()
        if not path.lower().endswith(".ini"):
            path += ".ini"
        values = {}
        with open(os.path.join(PROFILE_DIR, path)) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                shm = KEY_TO_SHM.get(k.strip())
                if not shm:
                    continue
                raw = v.strip()
                if shm == "bypass":
                    raw = str(0 if int(float(raw)) else 1)   # GUI stores the inverse
                values[shm] = raw
        for shm, value in values.items():
            ok, text = shmctl("set", shm, value)
            if not ok:
                return False, text
        remember_profile(path[:-4])
        return True, path[:-4]
    except Exception as e:
        return False, str(e)


def infer_profile(host, profiles):
    if not host:
        return ""
    tokens = [x for x in host.split(".") if x and x not in HOST_NOISE]
    full = "".join(tokens)
    for p in profiles:
        n = norm_name(p)
        if not n:
            continue
        if full in n or n in full:
            return p
        for x in tokens:
            if len(x) >= 3 and (x in n or n in x):
                return p
    return ""


def apply_auto_profile(force=False):
    global _last_auto_host, _last_auto_profile
    st = load_auto_state()
    if not st.get("enabled"):
        return
    host = host_of(active_url)
    if not host:
        return
    if host == _last_auto_host and not force:
        return
    _last_auto_host = host
    name = st.get("sites", {}).get(host) or infer_profile(host, list_profile_names())
    if not name:
        _last_auto_profile = ""
        return
    ok, _ = apply_profile_file(name)
    if ok:
        _last_auto_profile = name


def read_msg(f=None):
    f = f or sys.stdin.buffer
    raw = f.read(4)
    if len(raw) < 4:
        return None
    n = int.from_bytes(raw, "little")
    data = b""
    while len(data) < n:
        chunk = f.read(n - len(data))
        if not chunk:
            return None
        data += chunk
    return json.loads(data.decode("utf-8"))


def send_msg(obj, f=None):
    f = f or sys.stdout.buffer
    data = json.dumps(obj).encode("utf-8")
    f.write(len(data).to_bytes(4, "little") + data)
    f.flush()


def shmctl(*args):
    try:
        p = subprocess.run([SHMCTL, SHM, *args], capture_output=True, text=True, timeout=5)
        return p.returncode == 0, (p.stdout or p.stderr).strip()
    except Exception as e:
        return False, str(e)


def parse_pairs(text):
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def remember_profile(name):
    try:
        lines = []
        if os.path.isfile(VIDEO_CONFIG):
            lines = [l for l in open(VIDEO_CONFIG).read().splitlines()
                     if not l.startswith("profile=")]
        lines.append("profile=" + name)
        open(VIDEO_CONFIG, "w").write("\n".join(lines) + "\n")
    except Exception:
        pass


def browser_kind():
    """Explicit wrapper hint wins; otherwise infer from the running browser's profile path."""
    hint = os.environ.get("DLSSNR_BROWSER", "").lower()
    return hint if hint in ("chrome", "firefox") else running_browser()


def chrome_ready():
    """Check that the browser presenter is up with our layer loaded and the helper is ready.

    Firefox on Linux normally composites through WebRender/OpenGL and never maps the Vulkan layer
    into its GPU process. Waiting for that would keep auto-NR permanently off, so for Firefox we
    accept the shared-memory layer presenter (the desktop player) as the readiness signal.
    """
    reasons = []
    ok, text = shmctl("status")
    if not ok:
        return False, ["shm unavailable: " + text]
    s = parse_pairs(text)
    if int(s.get("initialised", 0)) != 1 or int(s.get("version", 0)) != 22:
        reasons.append("shm version mismatch")
    if int(s.get("helper_state", 0)) != 4:
        reasons.append("helper not running")
    # model_up and layer_pid are *results* of composing a frame, not prerequisites for enabling the
    # pass. After a helper restart both stay at zero, so requiring them here deadlocked the whole
    # pipeline: the extension refused to set enabled=1, the layer never composed, model_up never
    # became 1. They are still reported by the `status` verb (and shown in the panel); just don't
    # gate readiness on them.

    if browser_kind() == "firefox":
        # Firefox's own compositor is not Vulkan; layer_pid is the desktop player's presenter.
        return not reasons, reasons

    # Chrome: find a GPU process with the video layer mapped before enabling the pass.
    gpu_ok = False
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmd = open(f"/proc/{entry}/cmdline", "rb").read().replace(b"\0", b" ").decode("utf-8", "ignore")
        except Exception:
            continue
        if "--type=gpu-process" not in cmd:
            continue
        try:
            maps = open(f"/proc/{entry}/maps", "rb").read().decode("utf-8", "ignore")
            gpu_ok = "libVkLayer_NV_dlssnr" in maps or "VK_LAYER_NV_dlssnr_video" in maps
        except Exception:
            gpu_ok = False
        if gpu_ok:
            break
    if not gpu_ok:
        reasons.append("GPU process not ready with video layer")
    return not reasons, reasons


def helper_ctl(action, timeout=30):
    if not os.path.isfile(HELPER_CLI):
        return False, "dlssnr-helper not found"
    try:
        p = subprocess.run([HELPER_CLI, action], capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout or p.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as e:
        return False, str(e)


def panel_ctl(action, launch=True):
    """Send show/hide/toggle to the native control panel, starting it on demand.

    The panel is a separate GTK/WebKit window, so it is never part of the browser's Vulkan
    swapchain and the layer cannot enhance it or duplicate it in compare mode.
    """
    def try_connect():
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect(PANEL_SOCK)
            s.sendall((action + "\n").encode("utf-8"))
            s.close()
            return True
        except OSError:
            return False

    if try_connect():
        return True, ""
    if launch and action in ("show", "toggle") and os.path.isfile(PANEL_SCRIPT):
        env = os.environ.copy()
        # Never let the panel toolchain inherit the layer: it must paint its own X11 surface.
        for key in ("VKLayer_DLSS5", "VK_INSTANCE_LAYERS", "VK_LOADER_DEBUG"):
            env.pop(key, None)
        try:
            os.makedirs("/tmp/opencode", exist_ok=True)
            log = open("/tmp/opencode/dlss5-panel.log", "a")
            subprocess.Popen([sys.executable, PANEL_SCRIPT], env=env,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
        except Exception as e:
            return False, str(e)
        for _ in range(20):
            time.sleep(0.25)
            if try_connect():
                return True, ""
    return False, "native panel not running"


def running_browser():
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmd = open(f"/proc/{entry}/cmdline", "rb").read().replace(b"\0", b" ").decode("utf-8", "ignore")
        except Exception:
            continue
        if any(k in cmd for k in ("dlss5-firefox-profile", "firefox-video-profile")):
            return "firefox"
        if "chrome-video-profile" in cmd:
            return "chrome"
    return "chrome"


def restart_browser(url):
    """Full browser restart via the matching wrapper, with a detached kill/relaunch script."""
    try:
        last = float(open(RESTART_FLAG).read().strip())
        if time.time() - last < 5:
            return False, "restart throttled (5s cooldown)"
    except Exception:
        pass
    with open(RESTART_FLAG, "w") as f:
        f.write(str(time.time()))
    browser = browser_kind()
    profile = "dlss5-firefox-profile" if browser == "firefox" else "chrome-video-profile"
    wrapper = ("/home/vthieu/.local/bin/dlss5-firefox" if browser == "firefox"
               else "/home/vthieu/.local/bin/dlss5-chrome")
    os.makedirs(RUNTIME, exist_ok=True)
    script = os.path.join(RUNTIME, f"restart-{UID}.sh")
    safe = str(url).replace("'", "'\\''") if url else "about:blank"
    with open(script, "w") as f:
        f.write("#!/bin/bash\n")
        f.write(f"pkill -TERM -f '{profile}' 2>/dev/null || true\n")
        f.write("sleep 4\n")
        f.write(f"pkill -KILL -f '{profile}' 2>/dev/null || true\n")
        f.write("sleep 1\n")
        f.write(f"nohup {wrapper} '" + safe + "' > /tmp/opencode/dlss5-restart.log 2>&1 &\n")
    os.chmod(script, 0o755)
    subprocess.Popen(["setsid", "bash", script],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return True, f"{browser} restart initiated"


def play_video(url):
    """Send a video URL to the running DLSS5 player, or launch it with that URL."""
    url = str(url or "").strip()
    if not url or url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:"):
        return {"ok": False, "error": "no usable video URL (the native panel is on localhost)"}
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(APP_SOCK)
        s.sendall(url.encode("utf-8"))
        s.close()
        return {"ok": True, "launched": False}
    except OSError:
        pass
    try:
        env = os.environ.copy()
        env.setdefault("DISPLAY", ":0")
        subprocess.Popen([PLAYER, "--video", url], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "launched": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def handle(msg):
    global active_url, _last_auto_host, _last_auto_profile
    cmd = msg.get("cmd", "")
    with open(BRIDGE_LOG, "a") as _log:
        _log.write(json.dumps({"t": round(time.time(), 2), "cmd": cmd,
                               "key": msg.get("key"), "value": msg.get("value")}) + "\n")
    # Auto-profile-by-site. The native panel's own tab_report has a localhost URL; only a real
    # site reported by the browser's content script may become the active host.
    if cmd in ("tab_report", "browser.tab_report"):
        url = str(msg.get("url") or "")
        if host_of(url) in ("127.0.0.1", "localhost"):
            url = ""
        if url and (msg.get("playing") or msg.get("visible")):
            active_url = url
            try:
                with open(LAST_URL_FILE, "w") as f:
                    f.write(url)
            except Exception:
                pass
            apply_auto_profile()
        return {"ok": True}
    if cmd == "auto_profile_get":
        st = load_auto_state()
        host = host_of(active_url)
        return {"ok": True, "enabled": bool(st.get("enabled")), "host": host,
                "profile": st.get("sites", {}).get(host) or _last_auto_profile or ""}
    if cmd == "auto_profile_set":
        st = load_auto_state()
        st["enabled"] = bool(msg.get("enabled"))
        save_auto_state()
        _last_auto_host = ""
        apply_auto_profile()
        return {"ok": True, "enabled": st["enabled"]}
    if cmd == "auto_profile_import":
        # One-time migration of per-site choices stored by the old background-script logic. Only
        # fill gaps: the native panel's state file is authoritative once the user has used it.
        st = load_auto_state()
        for h, n in (msg.get("sites") or {}).items():
            if h and n:
                st.setdefault("sites", {}).setdefault(h, n)
        save_auto_state()
        return {"ok": True}
    if cmd == "profile_manual":
        host = host_of(msg.get("url"))
        if not host or host in ("127.0.0.1", "localhost"):
            host = host_of(active_url)   # the native panel is on localhost
        name = str(msg.get("name") or "")
        if host and name:
            st = load_auto_state()
            st.setdefault("sites", {})[host] = name
            save_auto_state()
            _last_auto_host, _last_auto_profile = host, name
        return {"ok": True, "host": host}
    if cmd == "profile_renamed":
        st = load_auto_state()
        for h, n in list(st.get("sites", {}).items()):
            if n == msg.get("oldName"):
                st["sites"][h] = msg.get("newName")
        if _last_auto_profile == msg.get("oldName"):
            _last_auto_profile = msg.get("newName")
        save_auto_state()
        return {"ok": True}
    if cmd == "profile_removed":
        st = load_auto_state()
        for h, n in list(st.get("sites", {}).items()):
            if n == msg.get("name"):
                del st["sites"][h]
        if _last_auto_profile == msg.get("name"):
            _last_auto_profile = ""
        save_auto_state()
        return {"ok": True}

    if cmd in ("panel.show", "panel.hide", "panel.toggle", "panel.status"):
        action = cmd.split(".", 1)[1]
        ok, text = panel_ctl(action)
        return {"ok": ok, "text": text}
    if cmd == "pause_hold":
        # The content script already sets `hold` itself; accept the verb so an enhanced browser's
        # paused-compare path stays quiet instead of reporting "unknown command".
        return {"ok": True}
    if cmd in ("chrome.restart", "browser.restart"):
        url = str(msg.get("url") or "")
        if host_of(url) in ("127.0.0.1", "localhost"):
            # The native panel lives on localhost, so its location.href is useless. Read the page
            # URL shared by whichever bridge instance last saw the browser tab.
            url = active_url
            if not url:
                try:
                    url = open(LAST_URL_FILE).read().strip()
                except Exception:
                    pass
        ok, text = restart_browser(url)
        return {"ok": ok, "text": text}
    if cmd == "chrome.ready":
        ready, reasons = chrome_ready()
        return {"ok": True, "ready": ready, "reasons": reasons}
    if cmd == "helper.status":
        ok, text = helper_ctl("status", timeout=8)
        return {"ok": ok, "text": text, "running": ok and not text.lower().startswith("helper not running")}
    if cmd == "helper.start":
        ok, text = helper_ctl("start", timeout=60)
        return {"ok": ok, "text": text}
    if cmd == "helper.stop":
        ok, text = helper_ctl("stop", timeout=15)
        return {"ok": ok, "text": text}
    if cmd == "helper.restart":
        ok, text = helper_ctl("restart", timeout=60)
        return {"ok": ok, "text": text}

    if cmd == "ping":
        # VKLayer_DLSS5 is inherited from the (enhanced) browser process, so the content script can
        # tell that its injected DOM panel would be enhanced/duplicated and use the native one.
        return {"ok": True, "version": 1, "shm": SHM,
                "nativePanel": os.path.isfile(PANEL_SCRIPT),
                "enhanced": bool(os.environ.get("VKLayer_DLSS5"))}

    if cmd == "status":
        ok, text = shmctl("status")
        if not ok:
            return {"ok": False, "error": text}
        s = parse_pairs(text)
        layer_pid = int(s.get("layer_pid", 0))
        return {
            "ok": True,
            "helper": int(s.get("helper_state", 0)),
            "modelUp": int(s.get("model_up", 0)),
            "layerPid": layer_pid,
            "layerAlive": bool(layer_pid) and os.path.isdir(f"/proc/{layer_pid}"),
            "layerFrames": int(s.get("layer_frames", 0)),
            "width": int(s.get("width", 0)),
            "height": int(s.get("height", 0)),
        }

    if cmd == "settings":
        ok, text = shmctl("settings")
        if not ok:
            return {"ok": False, "error": text}
        s = parse_pairs(text)
        # Return every setting the extension can show. The old 9-key list left most sliders showing
        # their HTML defaults even after a profile was loaded or changed live.
        return {"ok": True, "settings": {k: v for k, v in s.items() if k in PROFILE_KEYS}}

    if cmd == "suppress":
        try:
            ms = max(0, min(10000, int(msg.get("ms", 3000))))
            with open(SUPPRESS_FILE, "w") as f:
                f.write(str(time.time() + ms / 1000.0))
            ok, text = shmctl("set", "enabled", "0")
            return {"ok": ok, "error": None if ok else text, "untilMs": ms}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if cmd == "set":
        key = str(msg.get("key", ""))
        value = msg.get("value")
        if key not in PROFILE_KEYS:
            return {"ok": False, "error": f"unknown key {key}"}
        if key == "enabled" and int(value) == 1:
            try:
                suppressed = time.time() < float(open(SUPPRESS_FILE).read().strip())
                if suppressed:
                    return {"ok": True, "suppressed": True}
            except Exception:
                pass
        ok, text = shmctl("set", key, str(value))
        return {"ok": ok, "error": None if ok else text}

    if cmd == "capture":
        ok, text = shmctl("capture", "4")
        return {"ok": ok, "error": None if ok else text}

    if cmd == "roi":
        with open(ROI_LOG, "a") as _log:
            _log.write(json.dumps({"t": round(time.time(), 2), **msg}) + "\n")
        rect = msg.get("rect") or {}
        try:
            x = max(0, int(rect.get("x", 0)))
            y = max(0, int(rect.get("y", 0)))
            w = max(0, int(rect.get("w", 0)))
            h = max(0, int(rect.get("h", 0)))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        try:
            swap_w = max(0, int(msg.get("swapW", 0)))
            swap_h = max(0, int(msg.get("swapH", 0)))
        except Exception:
            swap_w = swap_h = 0
        fields = [("roix", x), ("roiy", y), ("roiw", w), ("roih", h),
                  ("roienabled", 1 if w >= 64 and h >= 64 else 0)]
        # A cached background worker can still send roi without the swap size. Never overwrite a
        # known size with zero -- that would let the layer process the wrong swapchain again.
        if swap_w >= 64 and swap_h >= 64:
            fields += [("roiswapw", swap_w), ("roiswaph", swap_h)]
        for key, value in fields:
            ok, text = shmctl("set", key, str(value))
            if not ok:
                return {"ok": False, "error": text}
        return {"ok": True, "rect": {"x": x, "y": y, "w": w, "h": h}}

    if cmd == "profiles.list":
        try:
            os.makedirs(PROFILE_DIR, exist_ok=True)
            names = sorted(f[:-4] for f in os.listdir(PROFILE_DIR)
                           if f.lower().endswith(".ini") and os.path.isfile(os.path.join(PROFILE_DIR, f)))
            return {"ok": True, "profiles": names}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def profile_path(name):
        name = os.path.basename(str(name)).strip()
        if not name:
            raise ValueError("empty profile name")
        if not name.lower().endswith(".ini"):
            name += ".ini"
        return os.path.join(PROFILE_DIR, name)

    if cmd == "profiles.load":
        try:
            path = profile_path(msg.get("name", ""))
            values = {}
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    shm = KEY_TO_SHM.get(k.strip())
                    if shm:
                        raw = v.strip()
                        if shm == "bypass":
                            raw = str(0 if int(float(raw)) else 1)   # GUI stores the inverse
                        values[shm] = raw
            for shm, value in values.items():
                ok, text = shmctl("set", shm, value)
                if not ok:
                    return {"ok": False, "error": text}
            remember_profile(os.path.basename(path)[:-4])
            return {"ok": True, "name": os.path.basename(path)[:-4], "count": len(values)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if cmd == "profiles.save":
        try:
            path = profile_path(msg.get("name", ""))
            ok, text = shmctl("settings")
            if not ok:
                return {"ok": False, "error": text}
            values = parse_pairs(text)
            os.makedirs(PROFILE_DIR, exist_ok=True)
            with open(path, "w") as f:
                f.write("# DLSS5VKLayer settings\n")
                f.write("# Generated by DLSS5 browser extension\n\n")
                for shm, key in PROFILE_KEYS.items():
                    if shm in values:
                        v = values[shm]
                        if key == "set_composition_enabled":
                            v = str(0 if int(float(v)) else 1)   # GUI stores the inverse
                        f.write(f"{key}={v}\n")
            remember_profile(os.path.basename(path)[:-4])
            return {"ok": True, "name": os.path.basename(path)[:-4]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if cmd == "profiles.rename":
        try:
            src = profile_path(msg.get("name", ""))
            dst = profile_path(msg.get("newName", ""))
            if not os.path.isfile(src):
                return {"ok": False, "error": "profile not found"}
            if os.path.exists(dst):
                return {"ok": False, "error": "a profile with that name already exists"}
            os.rename(src, dst)
            return {"ok": True, "name": os.path.basename(dst)[:-4]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if cmd == "profiles.remove":
        try:
            path = profile_path(msg.get("name", ""))
            if not os.path.isfile(path):
                return {"ok": False, "error": "profile not found"}
            os.remove(path)
            return {"ok": True, "name": os.path.basename(path)[:-4]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if cmd == "profiles.default":
        ok, text = shmctl("reset")
        return {"ok": ok, "error": None if ok else text}

    if cmd == "profiles.video_tuning":
        tuned = {
            "enabled": 1, "workingscale": 1, "mvec": 0, "sdr16multipass": 0,
            "reversible": 0, "passes": 1, "style": 2, "intensity": 1.32,
            "localtone": 0.85, "localstructure": 1.8, "sharpness": 1,
            "automask": 1, "applymodel": 1, "compare": 0, "hdrmode": 0,
            "detail": 1.0, "colour": 1.0, "transfer": 1, "guard": 2.0,
        }
        for shm, value in tuned.items():
            ok, text = shmctl("set", shm, str(value))
            if not ok:
                return {"ok": False, "error": text}
        return {"ok": True, "applied": list(tuned)}

    if cmd == "play":
        return play_video(msg.get("url"))

    return {"ok": False, "error": f"unknown command {cmd}"}


def serve_conn(rf, wf=None):
    """Answer native-messaging frames from one client (stdin or a proxy socket)."""
    wf = wf or sys.stdout.buffer
    while True:
        try:
            msg = read_msg(rf)
        except Exception:
            return
        if msg is None:
            return
        try:
            with open(BRIDGE_LOG, "a") as log:
                log.write(json.dumps({"cmd": msg.get("cmd"), "id": msg.get("id")}) + "\n")
        except Exception:
            pass
        try:
            resp = handle(msg)
        except Exception as e:
            resp = {"ok": False, "error": repr(e)}
        if "id" in msg:
            resp["id"] = msg["id"]
        try:
            send_msg(resp, wf)
        except Exception:
            return


def http_server(port=None):
    """Loopback HTTP transport for confined browsers that cannot use native messaging."""
    port = int(port or os.environ.get("DLSSNR_BRIDGE_HTTP", "8765"))

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body):
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self._reply(200, {"ok": True})

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length", "0"))
                msg = json.loads(self.rfile.read(n).decode("utf-8"))
                resp = handle(msg)
                if "id" in msg:
                    resp["id"] = msg["id"]
                self._reply(200, resp)
            except Exception as e:
                self._reply(200, {"ok": False, "error": repr(e)})

        def log_message(self, *args):
            pass

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def server():
    """Run as a normal (unconfined) daemon. A confined browser connects through a proxy or HTTP."""
    threading.Thread(target=http_server, daemon=True).start()
    path = os.environ.get("DLSSNR_BRIDGE_SOCK", os.path.join(RUNTIME, "bridge.sock"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)
    def handle_conn(conn):
        try:
            serve_conn(conn.makefile("rb"), conn.makefile("wb"))
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle_conn, args=(conn,), daemon=True).start()


def main():
    if "--server" in sys.argv:
        server()
        return
    serve_conn(sys.stdin.buffer)


if __name__ == "__main__":
    main()
