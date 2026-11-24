# DLSS5 Video

A companion to the DLSS5VKLayer Helper: a player for TikTok/Douyin and local video whose final
frame goes through the **same** Vulkan layer, NGX Feature 18 model, helper, settings and composition
that games use. It is not a second enhancement pipeline.

## Launch

- Desktop entry: **DLSS5 Video** (Audio/Video). It opens **two windows**: the settings panel and a
  separate player. Keep the player visible (tick **On top**) while changing settings in the other
  window — the video never hides behind a tab.
- Command line: `dlssnr-gui-video --video '<url or file>'`

`Chrome for DLSS5 Video` is a second desktop entry: it launches Chrome with
`--remote-debugging-port=9222 --remote-allow-origins="*"`, which the player's **Grab browser video**
button needs. Quit your normal Chrome first, or the new flags are ignored by the already-running
instance.

## Browser extension

`browser-extension/chrome/` and `browser-extension/firefox/` contain an unpacked extension for each
browser. Load it with *Developer mode → Load unpacked* (`chrome://extensions` or
`about:debugging#/runtime/this-firefox`). The Chrome build carries a fixed key, so its ID is
`habnnghdbobdldgenhnnghcinaegpiif`; the native host manifest already allows it. The Firefox build
uses `24d3d842-83df-5342-9b6f-dcfd0efb52ba`.

A native messaging host (`browser-extension/host/dlssnr_bridge.py`) is registered for both browsers
under `com.dlssnr.bridge`. It talks to the same shared memory via `dlssnr-shmctl` and starts/stops
the helper through `dlssnr-video-helper`, so the desktop GUI is **not required** for video: the
extension's **Helper → Start / Restart / Stop** buttons manage the model process, and the same
**Auto** toggle can keep NR on only while a visible tab has playing video. The desktop launcher
(`~/.local/bin/dlssnr-gui-video`) pins `DLSSNR_HELPER_CLI` to the same video helper so its buttons
cannot accidentally control the game helper.

`helper_state=4` means the helper process is alive; `model_up=0` is normal until the layer has
composed its first frame. `chrome.ready` therefore only requires a running helper plus a Chrome GPU
process with the video layer mapped — requiring `model_up`/`layer_pid` first deadlocked startup after
a helper restart (the pass was never enabled, so no frame was ever composed). The panel now says
"Helper running — model not ready yet" instead of the misleading "Helper not running".

**Snap Firefox (this machine):** Firefox Release refuses to permanently install an unsigned
add-on, and snap confinement makes native messaging unreliable (private `/tmp`, D-Bus portal). Use:

```bash
~/.local/bin/dlss5-firefox-temp
```

This starts the bridge daemon on `127.0.0.1:8765` and uses `web-ext run` to load
`browser-extension/firefox/` as a **temporary add-on** (it must be reloaded each Firefox restart;
`~/.local/bin/firefox` and the desktop launcher now call this script). The Firefox build sends
commands to the daemon over loopback HTTP instead of native messaging; the Chrome build still uses
`com.dlssnr.bridge` native messaging. Shared memory, sockets and flags live in `~/dlssnr-runtime`
via `TMPDIR`/`DLSSNR_SHM`/`DLSSNR_RUNTIME_DIR` so the confined browser and the unconfined
player/helper agree on paths. A permanent install would require an AMO-signed XPI or a
non-snap Firefox Developer/ESR build.

The same native **DLSS5 Controls** window is used here: the Firefox background forwards
`panel.toggle` and `browser.tab_report` to the loopback daemon, whose `handle()` is the same one
Chrome reaches over native messaging. Auto-profile state is owned by that daemon, so the toggle and
per-site choices work identically in both browsers. An unsigned build for the temporary loader is
kept at `browser-extension/dlss5-firefox.xpi` (files at the zip root, rebuilt from
`browser-extension/firefox/`).

#### Permanent install (AMO-signed XPI)

Firefox Release ignores `xpinstall.signatures.required`, so an unsigned XPI cannot be installed
permanently. Sign it with a free **unlisted** AMO submission:

1. Create API credentials at <https://addons.mozilla.org/en-US/developers/addon/api/key/> and save
   them in `~/.config/dlssnr-video/amo-credentials` (chmod 600):
   ```
   AMO_API_KEY="..."
   AMO_API_SECRET="..."
   ```
   (Exporting the same two variables instead of using the file works too.)
2. Run `./browser-extension/sign-firefox.sh` from the project root. It signs
   `browser-extension/firefox/` with `web-ext sign --channel unlisted` and writes the result to
   `browser-extension/signed/`.
3. Install that signed XPI permanently: `about:addons` → gear → **Install Add-on From File**.
4. Install the daemon once: `./packaging/install-dlss5-bridge.sh`. The temporary-add-on launcher used
   to start the bridge; a permanent add-on needs it running independently. The user service listens
   on `127.0.0.1:8765` with the same Vulkan/runtime environment as the Firefox launcher.
5. Start Firefox normally from its usual desktop entry — add-on, native panel and daemon are all
   there, with no reload on restart.

After a UI change, bump `version` in **both** Firefox manifests before re-signing (AMO rejects a
duplicate version), then repeat steps 2–3. The extension ID is fixed in the manifest, so the native
host manifest needs no change.

Nothing happens on a page until you click the extension's toolbar icon (or press `Alt+Shift+D`),
unless the site is on the **auto-site allowlist**. That click (or a matching auto-site) *arms the
tab*: the panel opens and video gating starts. Clicking again (or pressing the shortcut again,
or using the panel's **×**) disarms the tab: the panel closes, reporting stops and the pass is
switched off. Tabs you never clicked and that do not match the allowlist are inert — no panel, no
reports, no ROI, no auto profile, no enhancement, even when they are playing video (e.g. Amazon).

Open the extension's **Options** (`chrome://extensions` → Details → Extension options, or the ⚙
button in the panel) to manage the allowlist. Rules may be a host (`youtube.com`, `www` optional),
a host plus subdomains (`*.netflix.com`), a host + path prefix (`example.com/watch`), or a full
`https://` URL. Matching tabs arm themselves on load; all others stay click-only. Switching a tab
off manually sticks until you navigate to a different page, so an auto-site will not re-arm itself
behind your back. Arming lasts for the current page: a full navigation, closing the tab or
restarting the browser clears it.

**Profiles are segregated.** The extension reads and writes only
`~/.config/dlssnr/video-profiles/`; the desktop helper keeps its game profiles in
`~/.config/dlssnr/profiles/`. Removing or renaming a profile in the browser can no longer affect a
game profile. Once a tab is armed with the toolbar icon, a floating **DLSS5 Neural Rendering** panel appears with:

- **Enhance this video** — sends the page's `<video>` URL to the running DLSS5 Video player (or
  launches it), via a local socket. The enhanced video plays in that window, not in Chrome.
- Style, Intensity, Show original (A/B), Compare + Split, Capture A/B frames.
- Live status (helper running, frames, resolution).

The panel cannot change Chrome's own video surface: neither Chrome nor Firefox presents through
Vulkan, so no extension can inject the layer into the page. It drives the desktop player and the
shared settings instead. For a `blob:` (MSE) source the panel tells you to use the app's
**Grab browser video** CDP path, which reads the stream URL out of the tab.

## Enhancing Chrome itself (Vulkan presentation)

Chrome can be made to present its final composited frame through Vulkan on this machine:

```
google-chrome-stable --ozone-platform=x11 --use-angle=vulkan \
  --enable-features=Vulkan,SkiaGraphite ... 
```

with `VKLayer_DLSS5=1 VK_INSTANCE_LAYERS=VK_LAYER_NV_dlssnr` in the environment. The
**Chrome DLSS5 (Vulkan)** desktop entry does exactly that (and keeps the DevTools port for the
extension). Quit any running Chrome first, then launch that entry. Verified here:

```text
[dlssnr-layer] swapchain 945x1028 fmt=44
[dlssnr-layer] [comp] building 945x1028, model 1266x1378
[dlssnr] [ngx] STATUS: Feature=18 created=true ... size=1266x1378
```

The layer then enhances the whole Chrome window in place, exactly as it does a game. Practical
notes:

- The layer has no UI mask. Use **F11 / Fullscreen video** in the extension so the window contains
  only the video; otherwise page text and browser chrome are enhanced too.
- The launcher applies a Chrome-specific cost profile on every start: model resolution 100%,
  motion vectors off, 16-bit intermediates off, reversible proxy 0. With the game profile's 134%
  and quality optical flow the helper runs a 2573×1447 model with 14 MB crossing each way per frame,
  which makes Chrome visibly lag. Chrome hardware video decode is also disabled in the launcher;
  it caused a GPU-process `SIGTRAP` (`Bad file descriptor`) with the layer.
- The extension's **NR on (this page)** / **NR off** buttons write `enabled` in the shared header,
  so you can keep the pass off while browsing and on only while watching.
- For a lower-overhead Chrome window, run `dlss5-chrome --app="<url>"`: app mode gives Chrome one
  tab and one main swapchain instead of the many swapchains a full browser session creates.
- An occasional "present fence still pending; swapchain passes through" is the layer's fail-open
  path; it is not fatal and Chrome keeps presenting.
- The normal `google-chrome.desktop` (ANGLE GL) does not load the layer. This is a separate entry.
- `DLSSNR_SHM` must name a **real** file, not a symlink: the layer opens it with `O_NOFOLLOW`.
  The canonical file is `~/dlssnr-runtime/shm.bin` (real file, 0600); `TMPDIR` points at the same
  permanent directory for sockets and logs, so a `/tmp` wipe cannot break the pipeline. Passing a
  symlink makes every layer load log `[shm] open … failed`, so no presenter attaches. The helper's
  `~/.config/dlssnr-video/dlssnr/config.ini` must pin the same path.

### Where the control panel lives in Vulkan mode

Because the layer captures Chrome's final composited swapchain, an in-page control panel is just
more pixels in that frame: side-by-side compare samples the captured picture into both halves (two
copies of the panel), and the composed result is copied back over the browser's pixels, covering the
real control with a frozen raster of itself. CSS z-index cannot help — the rewrite happens below the
DOM compositor.

In a Vulkan-enhanced browser the extension therefore does **not** attach its panel to the page. The
toolbar icon (or `Alt+Shift+D`) opens **DLSS5 Controls**, a small always-on-top GTK window that
hosts the same panel in its own X11 surface, outside Chrome's swapchain, where the layer cannot
enhance, duplicate or cover it. The content script still reports video state and ROI for the pass;
only the UI moves.

The native host starts `browser-extension/host/dlssnr_panel.py` on the first toolbar toggle. It
serves the extension's own `panel.css` and `content.js` to a WebKit2 view over localhost, so there
is no second copy of the control definitions to keep in sync; the page is pinned to fill the window
and the GTK title bar handles dragging. Auto-profile-by-site is owned by the native host in this
mode (state in `~/.config/dlssnr-video/dlssnr/auto-profiles.json`), so its toggle and per-site
profile choices work here too. Closing the window exits the panel process; the next toolbar click
reopens it. Launch Chrome without `VKLayer_DLSS5` (the ordinary desktop entry) to get the old
in-page panel back.

## Browser video (CDP grab)

**Grab browser video** asks Chrome's DevTools which tab is playing a `<video>`, reads its URL, and
loads it into the player together with the page's Referer/User-Agent/Cookie headers. If the site
uses Media Source Extensions (a `blob:` src), the player watches the tab's network requests for
`.m3u8`/`.mp4` and uses the first master playlist it sees. This is how to watch sites yt-dlp does not
support (e.g. anime streaming sites) through the layer. The video is then played by mpv in our own
window; the browser tab is only the source of the URL.

Chrome cannot be enhanced in place like a game: its compositor presents through GL, not Vulkan, so
the layer never sees those frames. The grab path is the reliable equivalent.

The app starts mpv embedded in its own preview area with `--wid`, under X11 (XWayland on a Wayland
session) and `VKLayer_DLSS5=1`. mpv's Vulkan output creates the swapchain the layer intercepts, so
the frame path is identical to a game: capture -> proxy encode -> shared memory/dma-buf -> helper ->
Feature 18 -> composition -> present. Video is enhanced at the **displayed** resolution; there is no
output upscaling and no deblocking/denoising pass.

Requirements: the implicit layer installed (`~/.local/share/vulkan/implicit_layer.d/`), the helper
running with `nvngx_dlssnr.dll` imported, and `mpv` + `yt-dlp` (both from Ubuntu):
`sudo apt install mpv yt-dlp`.

## Using it

- Paste a TikTok/Douyin link (share text is fine; the first URL is extracted) or a local file, then
  **Play**. `Browse...` and `Paste` are shortcuts. The audio comes from mpv, so sync is mpv's.
- Transport: Pause/Play, seek, volume, and Loop are wired to mpv over its JSON IPC.
- **Portrait 9:16** caps the preview to a phone-shaped area for short-form video.
- The other tabs are the game settings, unchanged. Changes apply live; the helper rebuilds the
  feature when a latched setting changes. Style/Intensity are also in the quick panel.
- **Bypass NR (show original)** is an instant A/B: the whole pass still runs, only the picture
  differs. **Compare** (off / side-by-side / wipe) shows edited vs original in the same frame; use
  the wipe and move **Split**.
- **Capture A/B** writes four matched `before_NN.png` / `after_NN.png` pairs plus a manifest to
  `~/.local/state/dlssnr/captures`; **Open folder** opens it. That is the generated NR output you
  can inspect side by side.
- **Player log** shows mpv and layer output if a source will not play.

### Douyin

Douyin rejects anonymous yt-dlp requests. Log into Douyin in Chrome, then tick **Use Chrome
cookies (Douyin)** (toggling restarts the player), or export a `cookies.txt` and use **Cookies
file...**. mpv passes these to yt-dlp via `--ytdl-raw-options`. TikTok works without either.

### The two links that failed

- `anikototv.bz/...`: yt-dlp has no extractor for that site (`Unsupported URL`). Use **Grab browser
  video** from a Chrome tab playing it.
- YouTube: the extractor works (a JS runtime is now passed, `js-runtimes=node`, and a current
  standalone yt-dlp is installed in `~/.local/bin`), but Googlevideo returned `403 Forbidden` for
  this machine's network when fetching the media URL. That is upstream/account/network-side; a
  different network or Chrome cookies may be needed. The player reports the real HTTP error instead
  of failing silently.

### Tuning notes

- Model resolution 100% is the strict same-resolution behaviour. The shipped game profile uses
  134%, which is *internal* supersampling followed by a downscale; it costs more and is optional.
- Motion vectors are estimated by the helper with `VK_NV_optical_flow`; reset on scene cuts is
  bounded to the host transport, so if cuts ghost, set `DLSSNR_DMABUF=0` for this player.
- DRM content is not supported. Netflix/Disney+ and other EME streams stay in the browser, which
  does not present through the layer.
- Closing the video app leaves the helper running; the settings GUI keeps its stop-on-close habit.

## Source

`gui/videopage.{h,cpp}` (player, transport, A/B, capture), `gui/mainwindow.{h,cpp}` (inserts the
Video tab; `--video` mode leaves the helper running), `gui/main.cpp` (args + X11/layer env),
`gui/meson.build`, `gui/dlssnr_gui.pro`. Built with `ninja -C build/native gui/dlssnr_gui` and
installed to `~/.local/bin/dlssnr-gui-video`.
