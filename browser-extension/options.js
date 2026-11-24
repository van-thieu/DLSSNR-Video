// Auto-site allowlist editor. The service worker reads chrome.storage.local["autoSites"] and
// arms matching tabs on load; this page is just the management UI.
const KEY = 'autoSites';
const $ = (id) => document.getElementById(id);
let sites = [];

function show(text) {
  const el = $('status');
  el.textContent = text || '';
  if (text) setTimeout(() => { if (el.textContent === text) el.textContent = ''; }, 2500);
}

function render() {
  const list = $('sites');
  list.innerHTML = '';
  if (!sites.length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = 'No auto sites — the extension only activates when you click its icon.';
    list.appendChild(li);
    return;
  }
  sites.forEach((site, i) => {
    const li = document.createElement('li');
    const code = document.createElement('code');
    code.textContent = site;
    const del = document.createElement('button');
    del.type = 'button';
    del.textContent = 'Remove';
    del.addEventListener('click', () => { sites.splice(i, 1); save(); });
    li.appendChild(code);
    li.appendChild(del);
    list.appendChild(li);
  });
}

function save() {
  chrome.storage.local.set({ [KEY]: sites }, () => {
    render();
    show('Saved. Matching tabs are now armed automatically.');
  });
}

$('add-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const value = $('site').value.trim().replace(/^https?:\/\//i, '').replace(/\/+$/, '').toLowerCase();
  if (!value) return;
  if (!sites.includes(value)) sites.push(value);
  $('site').value = '';
  save();
});

chrome.storage.local.get([KEY], (d) => {
  sites = Array.isArray(d[KEY])
    ? d[KEY].map(x => String(x).trim().toLowerCase()).filter(Boolean)
    : [];
  render();
});
