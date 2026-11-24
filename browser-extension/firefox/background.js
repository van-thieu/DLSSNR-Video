// Loopback-HTTP bridge plus click-to-activate, active-video gating.
//
// A tab is armed either by the toolbar icon (or Alt+Shift+D) or automatically on pages listed in
// the "auto sites" allowlist (extension options). Reports from any other tab are ignored, so merely
// visiting some other site (Amazon, news, ...) never enables the pass or shows a panel. Once armed,
// the pass is enabled only while that tab has a visible, playing <video>. Manual NR on/off buttons
// take over for 30 seconds, then auto resumes. The layer reads `enabled` live from the shared header.
const BRIDGE_HTTP = 'http://127.0.0.1:8765/';
const reports = new Map();       // tabId -> { visible, playing, auto, rect, ts }
const armedTabs = new Set();     // tabIds currently armed (icon click or auto-site match)
const manualOff = new Map();     // tabId -> URL where the user switched it off (no auto re-arm there)
const tabUrl = new Map();        // tabId -> last seen http(s) URL
const ARMED_KEY = 'armedTabs';
const AUTO_SITES_KEY = 'autoSites';
let autoSites = [];              // host/path rules, e.g. "youtube.com", "*.netflix.com", "site.com/watch"
let manualUntil = 0;
let lastDesired = null;
let offSince = 0;                // debounce: video can briefly report paused while buffering/scrolling
const OFF_DELAY_MS = 6000;
let pauseHold = false;           // compare mode keeps the last frame frozen while the video is paused

if (chrome.storage && chrome.storage.session) {
  chrome.storage.session.get([ARMED_KEY], (d) => {
    if (d && Array.isArray(d[ARMED_KEY])) d[ARMED_KEY].forEach(id => armedTabs.add(id));
    applyGating();
  });
}
function saveArmed() {
  if (chrome.storage && chrome.storage.session)
    chrome.storage.session.set({ [ARMED_KEY]: [...armedTabs] });
}

// ---- auto-site allowlist -------------------------------------------------
// Rule syntax: "example.com" (host, www optional), "*.example.com" (host + subdomains),
// "example.com/path" (host plus URL path prefix), or a full https:// URL. Matching is
// case-insensitive; no rule means no site ever auto-arms (pure click-to-activate).
function matchSite(url, rule) {
  rule = String(rule || '').trim().toLowerCase();
  if (!rule) return false;
  let u;
  try { u = new URL(url); } catch (e) { return false; }
  if (!/^https?:$/.test(u.protocol)) return false;
  let hostPart = rule, pathPart = '';
  const slash = rule.indexOf('/');
  if (slash >= 0) { hostPart = rule.slice(0, slash); pathPart = rule.slice(slash); }
  hostPart = hostPart.replace(/^https?:/, '').replace(/^\/+/, '').replace(/\/+$/, '');
  const host = u.hostname.toLowerCase();
  let hostMatch;
  if (hostPart.startsWith('*.')) {
    const base = hostPart.slice(2);
    hostMatch = host === base || host.endsWith('.' + base);
  } else {
    hostMatch = host === hostPart || host === 'www.' + hostPart || hostPart === 'www.' + host;
  }
  if (!hostMatch) return false;
  if (!pathPart) return true;
  return (u.pathname + u.search).toLowerCase().startsWith(pathPart.toLowerCase());
}
function isAutoSite(url) {
  return autoSites.some(rule => matchSite(url, rule));
}
if (chrome.storage && chrome.storage.local) {
  chrome.storage.local.get([AUTO_SITES_KEY], (d) => {
    if (d && Array.isArray(d[AUTO_SITES_KEY])) autoSites = d[AUTO_SITES_KEY].filter(x => typeof x === 'string' && x.trim());
    reevaluateAutoTabs();
  });
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local' && changes[AUTO_SITES_KEY]) {
      const v = changes[AUTO_SITES_KEY].newValue;
      autoSites = Array.isArray(v) ? v.filter(x => typeof x === 'string' && x.trim()) : [];
      reevaluateAutoTabs();
    }
  });
}

// Auto-profile-by-site: remember a profile per hostname and apply it when that site's video starts.
let autoProfileEnabled = true;
let siteProfiles = {};           // host -> profile name
let lastAutoHost = '';
let lastAutoProfile = '';
let profilesCache = null;
const HOST_NOISE = new Set(['www', 'com', 'co', 'net', 'org', 'tv', 'cn', 'ca', 'io', 'app', 'm']);

const norm = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
function hostOf(url) {
  try { return new URL(url || '').hostname.toLowerCase().replace(/^www\./, ''); }
  catch (e) { return ''; }
}
function activeReport() {
  const now = Date.now();
  return [...reports.values()].find(r => r.auto && r.visible && r.playing && now - r.ts < 8000) || null;
}
function visibleReport() {
  const now = Date.now();
  const fresh = [...reports.values()].filter(r => r.auto && r.visible && now - r.ts < 8000);
  return fresh.find(r => r.playing) || fresh[0] || null;
}
async function getProfiles() {
  if (profilesCache) return profilesCache;
  const r = await requestToNative({ cmd: 'profiles.list' });
  if (r && r.ok) profilesCache = r.profiles || [];
  return profilesCache || [];
}
function inferProfile(host, profiles) {
  if (!host) return '';
  const tokens = host.split('.').filter(t => t && !HOST_NOISE.has(t));
  const full = tokens.join('');
  for (const p of profiles) {
    const n = norm(p);
    if (!n) continue;
    if (full.includes(n) || n.includes(full)) return p;
    for (const t of tokens) {
      if (t.length >= 3 && (n.includes(t) || t.includes(n))) return p;
    }
  }
  return '';
}
function saveAutoSettings() {
  if (chrome.storage && chrome.storage.local)
    chrome.storage.local.set({ autoProfileEnabled, siteProfiles });
}
if (chrome.storage && chrome.storage.local) {
  chrome.storage.local.get(['autoProfileEnabled', 'siteProfiles'], (d) => {
    if (d && typeof d.autoProfileEnabled === 'boolean') autoProfileEnabled = d.autoProfileEnabled;
    if (d && d.siteProfiles && typeof d.siteProfiles === 'object') siteProfiles = d.siteProfiles;
    // Migrate remembered per-site choices into the native host, which now owns auto-profile.
    requestToNative({ cmd: 'auto_profile_import', sites: siteProfiles }).catch(() => {});
  });
}

async function applyAutoProfile() {
  if (!autoProfileEnabled) return;
  const active = visibleReport();
  if (!active) return;
  const host = hostOf(active.url);
  if (!host || lastAutoHost === host) return;
  lastAutoHost = host;
  const profiles = await getProfiles();
  const name = siteProfiles[host] || inferProfile(host, profiles);
  if (!name) { lastAutoProfile = ''; return; }
  const r = await requestToNative({ cmd: 'profiles.load', name });
  if (r && r.ok) lastAutoProfile = name;
}

async function requestToNative(msg) {
  const id = Math.random().toString(36).slice(2);
  try {
    const r = await fetch(BRIDGE_HTTP, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...msg, id }),
      cache: 'no-store',
    });
    return await r.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

function applyGating() {
  if (pauseHold) return;          // frozen frame: do not turn the pass off
  if (Date.now() < manualUntil) return;
  const now = Date.now();
  // Only reports from armed tabs count. With nothing armed, switch the pass off immediately: a
  // video profile restored at browser startup can otherwise leave `enabled=1` in the SHM header.
  const anyArmed = armedTabs.size > 0;
  const entry = [...reports.entries()].find(([id, r]) =>
    armedTabs.has(id) && r.auto && r.visible && r.playing && now - r.ts < 8000);
  const active = entry ? entry[1] : null;
  const desired = active ? 1 : 0;
  if (desired === 0 && lastDesired !== 0) {
    // Debounce only while an armed tab is around. A video can briefly report paused while
    // buffering/scrolling; with no armed tab there is nothing to wait for.
    if (anyArmed && !offSince) offSince = now;
    if (anyArmed && now - offSince < OFF_DELAY_MS) return;
  } else {
    offSince = 0;   // video is playing again — re-enable immediately
  }
  if (desired === lastDesired) {
    if (desired && active) requestToNative({ cmd: 'roi', rect: active.rect, swapW: active.swapW, swapH: active.swapH }).catch(() => {});
    return;
  }
  if (desired && active) {
    // Do not fire the pass until the helper, layer presenter and Chrome GPU process are all up.
    requestToNative({ cmd: 'roi', rect: active.rect, swapW: active.swapW, swapH: active.swapH })
      .then(() => requestToNative({ cmd: 'chrome.ready' }))
      .then((r) => {
        if (!r || !r.ready) return;        // leave lastDesired at 0 so we retry next poll
        lastDesired = 1;
        return requestToNative({ cmd: 'set', key: 'enabled', value: 1 });
      })
      .catch(() => {});
  } else {
    lastDesired = 0;
    requestToNative({ cmd: 'set', key: 'enabled', value: 0 }).catch(() => {});
  }
}

// The toolbar icon (or shortcut) arms a tab: until then the content script sends nothing and no
// panel is shown. Clicking again disarms it. This is the extension-side "On click" behaviour.
async function messageTab(tabId, msg) {
  try {
    await chrome.tabs.sendMessage(tabId, msg);
  } catch (e) {
    // No content script yet (fresh navigation, chrome:// page, etc.). Inject then retry.
    try {
      await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
      await chrome.tabs.sendMessage(tabId, msg);
    } catch (e2) { /* page not injectable */ }
  }
}

async function setArmed(tabId, on, source = 'user') {
  reports.delete(tabId);          // discard a stale pre-activation report
  if (on) {
    armedTabs.add(tabId);
    manualOff.delete(tabId);
  } else {
    armedTabs.delete(tabId);
    // If the user switched it off, do not auto-arm again until this page actually navigates away.
    if (source === 'user') manualOff.set(tabId, tabUrl.get(tabId) || '');
    // A manual NR toggle or paused compare from this tab must not keep the pass on once it is gone.
    if (armedTabs.size === 0) {
      manualUntil = 0;
      pauseHold = false;
      offSince = 0;
    }
  }
  saveArmed();
  await messageTab(tabId, { cmd: on ? 'activate' : 'deactivate' });
  applyGating();
}

async function toggleArmed(tabId) {
  await setArmed(tabId, !armedTabs.has(tabId), 'user');
}

// Arm every open tab whose URL matches the allowlist, unless the user explicitly switched that tab
// off on this page. Used on startup, when the allowlist is edited in options, and on navigation.
async function reevaluateAutoTabs() {
  if (!autoSites.length) return;
  try {
    const tabs = await chrome.tabs.query({});
    for (const t of tabs) {
      if (!t.id || !/^https?:/.test(t.url || '')) continue;
      if (armedTabs.has(t.id) || manualOff.has(t.id)) continue;
      if (isAutoSite(t.url)) await setArmed(t.id, true, 'auto');
    }
  } catch (e) { /* tabs API unavailable */ }
}

function maybeAutoArm(tabId, url) {
  if (!url || !isAutoSite(url)) return;
  if (armedTabs.has(tabId) || manualOff.has(tabId)) return;
  setArmed(tabId, true, 'auto');
}

// Navigation (changeInfo.url) and the end of the initial load are the only moments we auto-arm,
// so clicking the icon to switch a page off is not immediately undone by another page event.
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (!tab || !/^https?:/.test(tab.url || '')) return;
  tabUrl.set(tabId, tab.url);
  // Only a real navigation away from the page where the user switched the extension off cancels
  // that choice. A repeated load event for the same URL must not re-arm it behind their back.
  if (changeInfo.url && manualOff.get(tabId) && manualOff.get(tabId) !== tab.url)
    manualOff.delete(tabId);
  if (changeInfo.url || changeInfo.status === 'complete') maybeAutoArm(tabId, tab.url);
});
chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  if (armedTabs.has(tabId) || manualOff.has(tabId)) return;
  try {
    const tab = await chrome.tabs.get(tabId);
    if (tab && tab.url) {
      tabUrl.set(tabId, tab.url);
      maybeAutoArm(tabId, tab.url);
    }
  } catch (e) { /* tab closed */ }
});

chrome.action.onClicked.addListener(async (tab) => {
  await toggleArmed(tab.id);
});

// PWA/app windows have no extension toolbar icon, so a global shortcut arms/disarms the active tab.
chrome.commands.onCommand.addListener(async (command) => {
  if (command !== '_execute_action') return;
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) return;
    await toggleArmed(tab.id);
  } catch (e) { /* no page / chrome:// */ }
});

// After an extension reload, an armed tab keeps a stale content script. Re-inject only into tabs
// the user actually activated, and re-send activate so the panel reconnects without a refresh.
// Unarmed pages are left completely alone.
async function reinjectAll() {
  try {
    const tabs = await chrome.tabs.query({});
    for (const t of tabs) {
      if (!t.id || !armedTabs.has(t.id)) continue;
      try {
        await chrome.scripting.executeScript({ target: { tabId: t.id }, files: ['content.js'] });
        await chrome.tabs.sendMessage(t.id, { cmd: 'activate' });
      } catch (e) { /* page not injectable */ }
    }
  } catch (e) { /* tabs API unavailable */ }
}
chrome.runtime.onInstalled.addListener(() => reinjectAll());
chrome.runtime.onStartup.addListener(() => reinjectAll());

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.cmd === 'pause_hold') {
    const id = sender.tab && sender.tab.id;
    // Ignore UI chatter from a page the user never armed.
    if (id != null && !armedTabs.has(id)) {
      sendResponse({ ok: true, active: pauseHold, ignored: true });
      return false;
    }
    const was = pauseHold;
    pauseHold = !!msg.active;
    if (pauseHold) {
      offSince = 0; lastDesired = 1;
      requestToNative({ cmd: 'set', key: 'enabled', value: 1 }).catch(() => {});
    } else if (was) {
      offSince = 0;
      applyGating();
    }
    sendResponse({ ok: true, active: pauseHold });
    return false;
  }
  if (msg && msg.cmd === 'tab_deactivated') {
    const id = sender.tab && sender.tab.id;
    if (id != null) setArmed(id, false, 'user');
    sendResponse({ ok: true });
    return false;
  }
  // auto_profile_* and profile_manual/renamed/removed fall through to the native host, which owns
  // that state for both the in-page panel and the native panel (Chrome native messaging, Firefox
  // loopback HTTP). A second copy here let the two panels overwrite each other's choices.
  if (msg && msg.cmd === 'profiles.save') {
    profilesCache = null;
    // Let requestToNative handle it, but invalidate our cache first.
  }
  if (msg && msg.cmd === 'tab_report') {
    const id = sender.tab && sender.tab.id;
    if (id != null && armedTabs.has(id)) {
      reports.set(id, {
        visible: !!msg.visible,
        playing: !!msg.playing,
        auto: !!msg.auto,
        rect: msg.rect || null,
        swapW: msg.swapW || 0,
        swapH: msg.swapH || 0,
        url: msg.url || '',
        ts: Date.now(),
      });
      if (msg.manual) manualUntil = Date.now() + 30000;
      applyGating();
    }
    sendResponse({ ok: true, ignored: id == null || !armedTabs.has(id) });
    return false;
  }
  requestToNative(msg).then(sendResponse);
  return true;
});

chrome.tabs.onRemoved.addListener((tabId) => {
  reports.delete(tabId);
  manualOff.delete(tabId);
  tabUrl.delete(tabId);
  if (armedTabs.delete(tabId)) {
    if (armedTabs.size === 0) { manualUntil = 0; pauseHold = false; offSince = 0; }
    saveArmed();
  }
  applyGating();
});
chrome.tabs.onActivated.addListener(() => { applyGating(); });
