// Daily Read — Read Later & Favorites.
// Clicks save instantly in browser storage (shared by every report opened from disk in Chrome). Where the browser
// supports it (Chrome/Edge), one "Link library file" click also keeps state/library.json in sync, which Python reads
// when it renders the next report. Elsewhere there is Export / Import. Everything rendered from saved data uses
// textContent and http(s)-only links: titles and summaries come from untrusted feeds.
(function () {
  "use strict";
  var KEY = "dailyread.library.v1";
  var EPOCH = "1970-01-01T00:00:00.000Z";
  var ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/;
  var DAY = /^\d{4}-\d{2}-\d{2}$/;
  var STEM = /^\d{4}-\d{2}-\d{2}(_\d{4})?$/;
  var PRUNE_MS = 180 * 86400000;

  // ---------- pure logic (also unit-tested with node) ----------
  // Truncate by code points, never UTF-16 units: cutting an emoji in half leaves a lone surrogate that cannot be saved.
  function str(v, max) {
    if (typeof v !== "string") return "";
    // collapse whitespace, then drop control and bidi-override characters (they can disguise text), keep emoji joiners
    var clean = v.replace(/\s+/g, " ").replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, "").trim();
    return Array.from(clean).slice(0, max).join("");
  }
  // A valid ISO-8601 UTC time, else EPOCH; a time in the future is clamped so a bad clock cannot pin a record.
  function stamp(v) {
    if (typeof v !== "string" || !ISO.test(v) || isNaN(Date.parse(v))) return EPOCH;
    var limit = Date.now() + 5 * 60000;
    return Date.parse(v) <= limit ? v : new Date(limit).toISOString();
  }
  function httpUrl(v) { var u = str(v, 2000); return /^https?:\/\//i.test(u) ? u : ""; }

  // Mirrors dailyread/library.py clean_record().
  function cleanRecord(r) {
    if (!r || typeof r !== "object" || Array.isArray(r)) return null;
    var id = str(r.id, 200), title = str(r.title, 300);
    if (!id || !title) return null;
    var stars = r.stars;
    return {
      id: id, title: title, url: httpUrl(r.url), source: str(r.source, 80),
      published: typeof r.published === "string" && DAY.test(r.published) ? r.published : "",
      stars: typeof stars === "number" && stars % 1 === 0 && stars >= 0 && stars <= 5 ? stars : 0,
      verdict: r.verdict === "read" || r.verdict === "skip" ? r.verdict : "",
      report: typeof r.report === "string" && STEM.test(r.report) ? r.report : "",
      summary: str(r.summary, 800), reason: str(r.reason, 300),
      minutes: typeof r.minutes === "number" && r.minutes % 1 === 0 && r.minutes >= 0 && r.minutes <= 999 ? r.minutes : 0,
      minutes_est: r.minutes_est === true,
      read_later: r.read_later === true, favorite: r.favorite === true, read: r.read === true, manual: r.manual === true,
      added_at: stamp(r.added_at), updated_at: stamp(r.updated_at)
    };
  }

  // Text of a library document -> {id: record}. Returns null when the text is not a library at all.
  function parseDoc(text) {
    var data;
    try { data = JSON.parse(text); } catch (e) { return null; }
    if (!data || typeof data !== "object" || !data.items || typeof data.items !== "object" || Array.isArray(data.items)) return null;
    var out = {};
    Object.keys(data.items).slice(0, 5000).forEach(function (k) {
      var rec = cleanRecord(data.items[k]);
      if (rec) out[rec.id] = rec;
    });
    return out;
  }

  // Newest change wins per item; on a tie the second argument wins.
  function mergeMaps(a, b) {
    var out = {};
    [a || {}, b || {}].forEach(function (m) {
      Object.keys(m).forEach(function (id) {
        if (!out[id] || m[id].updated_at >= out[id].updated_at) out[id] = m[id];
      });
    });
    return out;
  }

  // Tombstones (in neither list) are kept for a while so a removal can win a merge, then dropped.
  function pruneMap(map, nowMs) {
    var out = {};
    Object.keys(map).forEach(function (id) {
      var r = map[id];
      var stale = !r.read_later && !r.favorite && nowMs - Date.parse(r.updated_at) > PRUNE_MS;
      if (!stale) out[id] = r;
    });
    return out;
  }

  function toDoc(map, nowIso) { return { version: 1, updated_at: nowIso, items: map }; }

  // flag: "read_later" | "favorite" | "read". snap: snapshot used when the item is not in the library yet.
  function setFlag(map, id, snap, flag, value, nowIso) {
    var rec = map[id] ? Object.assign({}, map[id]) : cleanRecord(snap);
    if (!rec) return null;
    var wasSaved = rec.read_later || rec.favorite;
    rec[flag] = value;
    if (flag === "read_later" && !value) rec.read = false;
    if (flag === "read" && value) rec.read_later = true;
    if (!wasSaved && (rec.read_later || rec.favorite)) rec.added_at = nowIso;
    rec.updated_at = nowIso > rec.updated_at ? nowIso : rec.updated_at;
    var next = Object.assign({}, map);
    next[id] = rec;
    return next;
  }

  // Newest article first. In Read Later, finished items sink to the bottom.
  function listOf(map, kind) {
    var flag = kind === "later" ? "read_later" : "favorite";
    return Object.keys(map).map(function (id) { return map[id]; }).filter(function (r) { return r[flag]; })
      .sort(function (a, b) {
        if (kind === "later" && a.read !== b.read) return a.read ? 1 : -1;
        if (a.published !== b.published) return a.published < b.published ? 1 : -1;
        return a.added_at < b.added_at ? 1 : a.added_at > b.added_at ? -1 : 0;
      });
  }

  function matches(rec, term) {
    if (!term) return true;
    return (rec.title + " " + rec.summary + " " + rec.source).toLowerCase().indexOf(term.toLowerCase()) >= 0;
  }

  var core = { cleanRecord: cleanRecord, parseDoc: parseDoc, mergeMaps: mergeMaps, pruneMap: pruneMap, toDoc: toDoc,
               setFlag: setFlag, listOf: listOf, matches: matches };
  if (typeof module === "object" && module && module.exports) { module.exports = core; return; }
  if (typeof document === "undefined") return;
  document.addEventListener("DOMContentLoaded", init);

  // ---------- browser part ----------
  var HEART = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>';
  var CLOCK = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="6.2"/><path d="M8 4.8V8l2.2 1.4"/></svg>';
  var BOOK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>';

  function readJson(id) {
    var el = document.getElementById(id);
    try { return el ? JSON.parse(el.textContent) : null; } catch (e) { return null; }
  }
  function lsGet() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function lsSet(v) { try { localStorage.setItem(KEY, v); return true; } catch (e) { return false; } }
  function nowIso() { return new Date().toISOString(); }
  function all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function init() {
    var snaps = readJson("report-items") || {};
    var seed = readJson("library-seed");
    var lib = mergeMaps(seed ? parseDoc(JSON.stringify(seed)) : {}, parseDoc(lsGet() || "") || {});
    var handle = null, fileState = "none";      // none | synced | paused | error
    var fileNote = "", queue = Promise.resolve(), timer = null;
    var helper = readJson("library-helper");         // {url, token} when the auto-save helper is installed
    var viaHelper = false, helperNote = "";
    var canLink = !helper && typeof window.showSaveFilePicker === "function";   // the helper replaces the file link
    var pageMeta = readJson("report-meta") || {};
    var catalog = {};
    (window.DR_CATALOG || []).forEach(function (e) { catalog[e.id] = e; });
    var panels = { later: document.getElementById("panel-later"), favorites: document.getElementById("panel-favorites") };
    var terms = { later: "", favorites: "" };
    var justAdded = "";

    // ----- tabs -----
    var tabs = all(".tab[data-tab]");
    function showTab(name) {
      tabs.forEach(function (t) {
        var on = t.getAttribute("data-tab") === name;
        t.classList.toggle("active", on);
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
        var p = document.getElementById("panel-" + t.getAttribute("data-tab"));
        if (p) p.hidden = !on;
      });
    }
    tabs.forEach(function (t, i) {
      t.addEventListener("click", function () { showTab(t.getAttribute("data-tab")); });
      t.addEventListener("keydown", function (e) {
        var n = null;
        if (e.key === "ArrowRight") n = tabs[(i + 1) % tabs.length];
        else if (e.key === "ArrowLeft") n = tabs[(i - 1 + tabs.length) % tabs.length];
        else if (e.key === "Home") n = tabs[0];
        else if (e.key === "End") n = tabs[tabs.length - 1];
        if (!n) return;
        n.focus(); showTab(n.getAttribute("data-tab")); e.preventDefault();
      });
    });
    window.addEventListener("hashchange", function () {      // a link to a row jumps back to Today
      if (/^#i-/.test(location.hash)) showTab("today");
    });
    if (/^#i-/.test(location.hash)) showTab("today");

    // ----- persistence -----
    function saveLocal() {
      lib = pruneMap(lib, Date.now());
      lsSet(JSON.stringify(toDoc(lib, nowIso())));
    }
    function writeFile() {
      if (!handle || fileState !== "synced") return Promise.resolve();
      var h = handle;
      queue = queue.then(function () {
        return h.createWritable().then(function (w) {
          return w.write(JSON.stringify(toDoc(lib, nowIso()), null, 1)).then(function () { return w.close(); });
        });
      }).catch(function (e) { fileState = "error"; fileNote = String(e && e.message || e); renderStatus(); });
      return queue;
    }
    function scheduleWrite() {
      clearTimeout(timer);
      timer = setTimeout(helper ? helperPush : writeFile, helper ? 150 : 250);
    }

    // ----- auto-save helper (launchd starts it on the first request, it exits when idle) -----
    function helperCall(method, doc) {
      var ctl = new AbortController();
      var t = setTimeout(function () { ctl.abort(); }, 8000);     // the first call may have to start the helper
      var opts = { method: method, headers: { "X-DailyRead-Token": helper.token }, signal: ctl.signal, cache: "no-store" };
      if (doc) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(doc); }
      return fetch(helper.url + "/library", opts).then(function (r) {
        clearTimeout(t);
        if (!r.ok) throw new Error("helper answered " + r.status);
        return r.text();
      }, function (e) { clearTimeout(t); throw e; });
    }
    function helperOk() { viaHelper = true; helperNote = ""; fileState = "synced"; fileNote = "state/library.json"; }
    function helperFailed(e) {
      viaHelper = false; fileState = "none";
      helperNote = (e && e.name === "AbortError") ? "it did not answer in time" : String(e && e.message || e);
      renderStatus();
    }
    function takeFromHelper(text) {
      var remote = parseDoc(text);
      if (remote === null) throw new Error("it sent something that is not a library");
      helperOk();
      var before = JSON.stringify(lib);
      lib = mergeMaps(lib, remote);
      if (JSON.stringify(lib) !== before) { saveLocal(); render(); } else renderStatus();
    }
    function helperSync() {          // on open: take what is in the file, then send what only this browser has
      return helperCall("GET").then(takeFromHelper).then(helperPush).catch(helperFailed);
    }
    function helperPush() {          // the helper merges the whole library into the file and answers with the result
      return helperCall("POST", toDoc(lib, nowIso())).then(takeFromHelper).catch(helperFailed);
    }
    function persist() { saveLocal(); render(); scheduleWrite(); }

    function syncFromFile(h) {
      return h.getFile().then(function (f) { return f.text(); }).then(function (text) {
        var remote = text.trim() ? parseDoc(text) : {};
        if (remote === null) throw new Error("that file is not a Daily Read library");
        handle = h; fileState = "synced"; fileNote = h.name || "library file";
        lib = mergeMaps(remote, lib);
        saveLocal(); render();
        return writeFile();
      }).catch(function (e) { fileState = "error"; fileNote = String(e && e.message || e); renderStatus(); });
    }

    // The stored file handle survives restarts in IndexedDB (best effort: some browsers can't store it).
    function idb() {
      return new Promise(function (res, rej) {
        var r = indexedDB.open("dailyread", 1);
        r.onupgradeneeded = function () { r.result.createObjectStore("h"); };
        r.onsuccess = function () { res(r.result); };
        r.onerror = function () { rej(r.error); };
      });
    }
    function idbGet() {
      return idb().then(function (d) { return new Promise(function (res, rej) {
        var q = d.transaction("h").objectStore("h").get("file");
        q.onsuccess = function () { res(q.result || null); }; q.onerror = function () { rej(q.error); };
      }); });
    }
    function idbPut(h) {
      return idb().then(function (d) { return new Promise(function (res, rej) {
        var t = d.transaction("h", "readwrite"); t.objectStore("h").put(h, "file");
        t.oncomplete = res; t.onerror = function () { rej(t.error); };
      }); });
    }

    function linkFile() {
      window.showSaveFilePicker({ suggestedName: "library.json", id: "dailyread-library", startIn: "documents", types: [{ description: "Daily Read library", accept: { "application/json": [".json"] } }] })
        .then(function (h) { return idbPut(h).catch(function () {}).then(function () { return syncFromFile(h); }); })
        .catch(function (e) { if (e && e.name !== "AbortError") { fileState = "error"; fileNote = String(e.message || e); renderStatus(); } });
    }
    function reconnect() {
      if (!handle) return linkFile();
      return handle.requestPermission({ mode: "readwrite" }).then(function (p) {
        if (p === "granted") return syncFromFile(handle);
        fileState = "paused"; renderStatus();
      }).catch(function (e) { fileState = "error"; fileNote = String(e && e.message || e); renderStatus(); });
    }
    if (canLink) {
      idbGet().then(function (h) {
        if (!h) return;
        handle = h;
        return h.queryPermission({ mode: "readwrite" }).then(function (p) {
          if (p === "granted") return syncFromFile(h);
          fileState = "paused"; renderStatus();
        });
      }).catch(function () {});
    }

    function exportFile() {
      var blob = new Blob([JSON.stringify(toDoc(lib, nowIso()), null, 1)], { type: "application/json" });
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "daily-read-library-" + nowIso().slice(0, 10) + ".json";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
    }
    var picker = document.createElement("input");
    picker.type = "file"; picker.accept = ".json,application/json"; picker.hidden = true;
    picker.addEventListener("change", function () {
      var f = picker.files && picker.files[0];
      picker.value = "";
      if (!f) return;
      f.text().then(function (text) {
        var remote = parseDoc(text);
        if (remote === null) { fileNote = "that file is not a Daily Read library"; fileState = "error"; renderStatus(); return; }
        var before = Object.keys(lib).length;
        lib = mergeMaps(remote, lib);
        fileNote = "imported " + Object.keys(remote).length + " item(s), " + (Object.keys(lib).length - before) + " new";
        persist();
        renderStatus(true);
      });
    });
    document.body.appendChild(picker);

    // ----- rendering -----
    var announceEl = document.getElementById("announce"), spoken = [], speakTimer = null;
    function say(text) {      // spoken by screen readers; messages that arrive together are read as one announcement
      if (!announceEl || !text) return;
      spoken.push(text);
      clearTimeout(speakTimer);
      speakTimer = setTimeout(function () {
        announceEl.textContent = "";
        var all = spoken.join(". ");
        spoken = [];
        setTimeout(function () { announceEl.textContent = all; }, 30);
      }, 60);
    }
    function el(tag, cls, text) {
      var e = document.createElement(tag);
      if (cls) e.className = cls;
      if (text != null) e.textContent = text;
      return e;
    }
    // A toggle keeps ONE label ("Favorite: <title>") and reports its state with aria-pressed (ARIA APG button pattern).
    function actBtn(kind, id, on, title) {
      var b = el("button", "act" + (on ? " on" : ""));
      var name = kind === "fav" ? "Favorite" : "Read Later";
      b.type = "button"; b.setAttribute("data-act", kind); b.setAttribute("data-id", id);
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.setAttribute("aria-label", name + ": " + title); b.title = name;
      b.innerHTML = kind === "fav" ? HEART : BOOK;     // constant markup, no data in it
      return b;
    }
    function fmtDay(d) {
      if (!DAY.test(d || "")) return "—";
      var p = d.split("-").map(Number);
      return new Date(Date.UTC(p[0], p[1] - 1, p[2])).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" }).replace(",", "");
    }
    function starsEl(n) {
      var s = el("span", "stars s" + n);
      s.setAttribute("role", "img"); s.setAttribute("aria-label", n + " of 5 stars");
      s.textContent = "★".repeat(n);
      s.appendChild(el("span", "off", "★".repeat(5 - n)));
      return s;
    }
    // Same markup as render.read_time(): "7 min" counted from the article, "~7 min" estimated by the review.
    function readTimeEl(rec) {
      var n = rec.minutes;
      var s = el("span", "rt");
      s.title = "About " + n + " minute" + (n === 1 ? "" : "s") + " to read" + (rec.minutes_est ? " (estimated)" : "");
      s.innerHTML = CLOCK;                       // constant markup, no data in it
      s.appendChild(document.createTextNode((rec.minutes_est ? "~" : "") + n + " min"));
      return s;
    }
    function pillBtn(act, id, text, cls, label) {
      var b = el("button", "pill-btn " + cls, text);
      b.type = "button"; b.setAttribute("data-act", act); b.setAttribute("data-id", id); b.setAttribute("aria-label", label);
      return b;
    }

    function row(rec, kind) {
      var tr = el("tr", "lib-row" + (kind === "later" && rec.read ? " is-done" : "") + (rec.id === justAdded ? " just-added" : ""));
      tr.setAttribute("data-id", rec.id);
      var date = el("td", "c-date", fmtDay(rec.published));
      if (rec.manual) date.appendChild(el("span", "added-tag", "added"));
      var main = el("td", "c-main");
      var link = el("a", "t-title", rec.title);
      var safe = httpUrl(rec.url);
      if (safe) { link.href = safe; link.target = "_blank"; link.rel = "noopener noreferrer"; }
      main.appendChild(link);
      if (rec.summary) main.appendChild(el("div", "lib-sum", rec.summary));
      var meta = el("div", "meta");
      meta.appendChild(starsEl(rec.stars));
      if (rec.minutes) meta.appendChild(readTimeEl(rec));
      if (rec.verdict === "read") meta.appendChild(el("span", "v-pill read", "✅ Read fully"));
      main.appendChild(meta);
      var src = el("td", "c-source");
      src.appendChild(el("span", "src-chip", rec.source || "—"));
      var origin = rec.report && catalog[rec.report];
      if (origin) {                // open that day's report at this very row (link built from our own catalog, not from saved text)
        var from = el("a", "from-report", origin.label + " report ");
        var arrow = el("span", null, "↗");
        arrow.setAttribute("aria-hidden", "true");
        from.appendChild(arrow);
        from.setAttribute("aria-label", "Open “" + rec.title + "” in the " + origin.label + " report");
        from.href = (rec.report === pageMeta.stem ? "" : (pageMeta.root || "") + origin.href) + "#i-" + encodeURIComponent(rec.id);
        src.appendChild(from);
      }
      var acts = el("td", "c-acts");
      var box = el("div", "acts");
      if (kind === "later") {
        box.appendChild(actBtn("fav", rec.id, rec.favorite, rec.title));
        box.appendChild(pillBtn("read", rec.id, rec.read ? "↺ Unread" : "✓ Read", "done", rec.read ? "Mark as unread: " + rec.title : "Mark as read: " + rec.title));
        box.appendChild(pillBtn("remove", rec.id, "✕ Remove", "remove", "Remove from Read Later: " + rec.title));
      } else {
        box.appendChild(actBtn("later", rec.id, rec.read_later, rec.title));
        box.appendChild(actBtn("fav", rec.id, true, rec.title));
      }
      acts.appendChild(box);
      [date, main, src, acts].forEach(function (td) { tr.appendChild(td); });
      return tr;
    }

    function renderList(kind) {
      var panel = panels[kind];
      if (!panel) return;
      var body = panel.querySelector("tbody");
      var items = listOf(lib, kind);
      var shown = items.filter(function (r) { return matches(r, terms[kind]); });
      body.textContent = "";
      shown.forEach(function (r) { body.appendChild(row(r, kind)); });
      var empty = panel.querySelector(".lib-empty");
      var none = panel.querySelector(".lib-nomatch");
      empty.hidden = items.length > 0;
      none.hidden = !(items.length > 0 && shown.length === 0);
      panel.querySelector(".table-wrap").hidden = shown.length === 0;
      var sum = panel.querySelector(".lib-count");
      if (kind === "later") {
        var unread = items.filter(function (r) { return !r.read; }).length;
        sum.textContent = items.length ? items.length + " saved · " + unread + " unread · " + (items.length - unread) + " read" : "";
      } else {
        sum.textContent = items.length ? items.length + " favorite" + (items.length > 1 ? "s" : "") : "";
      }
    }

    function updateButtons() {
      all("#panel-today .act[data-id]").forEach(function (b) {
        var rec = lib[b.getAttribute("data-id")];
        var later = b.getAttribute("data-act") === "later";
        var on = !!(rec && (later ? rec.read_later : rec.favorite));
        b.classList.toggle("on", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
      var recs = Object.keys(lib).map(function (k) { return lib[k]; });
      var nl = document.getElementById("n-later"), nf = document.getElementById("n-favorites");
      if (nl) nl.textContent = recs.filter(function (r) { return r.read_later; }).length;
      if (nf) nf.textContent = recs.filter(function (r) { return r.favorite; }).length;
    }

    function renderStatus(flash) {
      var text, btn = null;
      if (helper && viaHelper) text = "✓ Saved automatically to state/library.json";
      else if (helper) { text = "Auto-save helper not reachable (" + (helperNote || "not contacted yet") + "). Your lists are safe in this browser and are sent to the file on the next change."; btn = "Retry"; }
      else if (fileState === "synced") text = "✓ Synced to " + fileNote;
      else if (fileState === "paused") { text = "File sync paused. Chrome needs a click to continue."; btn = "Reconnect file"; }
      else if (fileState === "error") { text = "File sync problem: " + fileNote; btn = canLink ? "Link library file" : null; }
      else if (canLink) { text = "Saved in this browser. Link a file to keep a copy in your project."; btn = "Link library file"; }
      else text = "Saved in this browser. This browser can't link a file: use Export to keep a copy.";
      if (flash && fileNote && fileState !== "synced") text = fileNote;
      all(".lib-status").forEach(function (n) { n.textContent = text; n.classList.toggle("bad", fileState === "error"); });
      all(".btn-link").forEach(function (b) {
        b.hidden = !btn; if (btn) b.textContent = btn;
      });
      if (typeof renderBanner === "function") renderBanner();
    }

    // A loud reminder while saved items exist only in this browser (not yet in the file next to your state data).
    var bannerEl = document.getElementById("lib-banner");
    var BANNER_KEY = "dailyread.banner.until";
    function bannerDismissed() { try { return Number(localStorage.getItem(BANNER_KEY) || 0) > Date.now(); } catch (e) { return false; } }
    function renderBanner() {
      if (!bannerEl) return;
      var saved = Object.keys(lib).some(function (k) { return lib[k].read_later || lib[k].favorite; });
      var act = bannerEl.querySelector(".banner-act");
      if (!saved || fileState === "synced" || bannerDismissed()) { bannerEl.hidden = true; return; }
      var text, label = null, warn = false;
      if (helper) {
        text = "The auto-save helper did not answer (" + (helperNote || "not contacted yet") + "). Your lists are safe in this browser and are written to state/library.json as soon as it answers.";
        label = "Retry"; warn = true;
      } else if (fileState === "paused") {
        text = "Your lists are safe in this browser. File sync is paused, so changes are written to the file once you reconnect.";
        label = "Reconnect file";
      } else if (fileState === "error") {
        text = "Your lists are saved in this browser only. File sync problem: " + fileNote; warn = true;
        label = canLink ? "Link library file" : null;
      } else if (canLink) {
        text = "Your lists are only saved in this browser. Link library.json (in this project's state folder) to keep them next to your state data, safe from a browser reset or a new laptop.";
        label = "Link library file";
      } else {
        text = "Your lists are only saved in this browser, which can't write a file. Use Export now and then to keep a backup copy.";
        label = "Export now";
      }
      bannerEl.querySelector(".lib-banner-text").textContent = text;
      bannerEl.classList.toggle("warn", warn);
      act.hidden = !label; act.textContent = label || "";
      act.setAttribute("data-mode", label === "Export now" ? "export" : label === "Retry" ? "retry" : "link");
      var wasHidden = bannerEl.hidden;
      bannerEl.hidden = false;
      if (wasHidden) say(text);        // a region that appears with its text is not announced reliably: say it explicitly
    }
    if (bannerEl) {
      bannerEl.querySelector(".banner-act").addEventListener("click", function (e) {
        var mode = e.currentTarget.getAttribute("data-mode");
        if (mode === "export") exportFile(); else if (mode === "retry") helperSync(); else reconnect();
      });
      bannerEl.querySelector(".banner-x").addEventListener("click", function () {
        try { localStorage.setItem(BANNER_KEY, String(Date.now() + 7 * 86400000)); } catch (e) {}
        bannerEl.hidden = true;
      });
    }

    function render() { updateButtons(); renderList("later"); renderList("favorites"); renderStatus(); renderBanner(); }

    // ----- add a post from elsewhere (a dialog; the helper reads the page, asks Claude to summarize and rate it, and saves it) -----
    var addDialog = document.getElementById("add-dialog");
    if (addDialog) (function () {
      var $ = function (id) { return document.getElementById(id); };
      var box = $("add-form"), openBtn = $("add-open"), closeBtn = $("add-close"), pasteBtn = $("add-paste"), input = $("add-url"), goBtn = $("add-go");
      var statusEl = $("add-status"), barEl = $("add-bar"), resultEl = $("add-result"), actionsEl = $("add-actions");
      var state = "idle", ticker = null, lastAdded = "";
      var URL_RE = /https?:\/\/[^\s<>"']+/i;
      function urlFrom(text) {         // a share message is often "Title https://…": take the link
        var m = URL_RE.exec(text || "");
        return m ? m[0].replace(/[.,;)]+$/, "") : (text || "").trim();
      }
      function show(kind, text) { statusEl.className = "add-status" + (kind ? " " + kind : ""); statusEl.textContent = text || ""; }
      function setState(next) {
        state = next;
        var busy = next === "busy", done = next === "done";
        box.classList.toggle("busy", busy); box.classList.toggle("done", done);
        barEl.hidden = !busy; resultEl.hidden = !done; actionsEl.hidden = !done;
        pasteBtn.disabled = busy; goBtn.disabled = busy; closeBtn.disabled = busy; input.readOnly = busy;
        pasteBtn.setAttribute("aria-busy", busy ? "true" : "false");
        pasteBtn.querySelector(".add-paste-label").textContent = busy ? "Reading…" : "Paste link & add";
        clearInterval(ticker);
        if (busy) {
          var t0 = Date.now();
          show("", "Reading the page, summarizing and rating it… 0 s");
          ticker = setInterval(function () { show("", "Reading the page, summarizing and rating it… " + Math.round((Date.now() - t0) / 1000) + " s"); }, 1000);
        }
      }
      function openDialog() {
        if (!addDialog.open) addDialog.showModal();
        if (state !== "busy") { setState("idle"); show("", ""); input.value = ""; }
        if (!helper) {
          pasteBtn.disabled = true; goBtn.disabled = true; input.disabled = true;
          show("", "Adding posts needs the auto-save helper: run bin/dailyread schedule install, then bin/dailyread render --all.");
          closeBtn.focus();
        } else pasteBtn.focus();
      }
      function closeDialog() { if (state !== "busy" && addDialog.open) addDialog.close(); }
      function fail(text) { setState("idle"); show("err", "⚠ " + text); input.focus(); say(text); }
      function stars(n) { return "★".repeat(n) + "☆".repeat(5 - n); }
      function run(raw) {
        var url = urlFrom(raw);
        if (!/^https?:\/\//i.test(url) && url.indexOf(".") < 0) { show("err", "Paste a web link first."); input.focus(); return; }
        input.value = url; setState("busy");
        var ctl = new AbortController(), timer = setTimeout(function () { ctl.abort(); }, 150000);
        fetch(helper.url + "/add", { method: "POST", headers: { "X-DailyRead-Token": helper.token, "Content-Type": "application/json" },
                                      body: JSON.stringify({ url: url }), signal: ctl.signal, cache: "no-store" })
          .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { return { ok: r.ok, data: d }; }); })
          .then(function (res) {
            clearTimeout(timer);
            if (!res.ok) { fail(res.data.error || "The helper could not add that link."); return; }
            var rec = cleanRecord(res.data.record), merged = parseDoc(JSON.stringify(res.data.doc));
            if (merged) lib = mergeMaps(lib, merged);
            lastAdded = rec ? rec.id : "";
            saveLocal(); render();
            $("add-result-source").textContent = rec ? rec.source + (rec.minutes ? " · " + (rec.minutes_est ? "~" : "") + rec.minutes + " min read" : "") : "";
            $("add-result-stars").textContent = rec ? stars(rec.stars) : "";
            $("add-result-stars").setAttribute("aria-label", rec ? rec.stars + " of 5 stars" : "");
            var verdict = $("add-result-verdict"); verdict.textContent = rec && rec.verdict === "read" ? "✅ Read fully" : "Summary is enough";
            verdict.className = "add-verdict" + (rec && rec.verdict === "read" ? " read" : "");
            $("add-result-title").textContent = rec ? rec.title : "";
            $("add-result-summary").textContent = rec ? rec.summary : "";
            setState("done");
            show("ok", res.data.existing ? "Already in your Read Later list." : "✓ Added to Read Later, dated today.");
            $("add-done").focus();
            say((res.data.existing ? "Already in Read Later: " : "Added to Read Later: ") + (rec ? rec.title + ", " + rec.source + ", " + rec.stars + " of 5 stars" : "link"));
          })
          .catch(function (err) {
            clearTimeout(timer);
            fail(err && err.name === "AbortError" ? "That took too long, so it was stopped." : "The helper did not answer. Is it installed? (bin/dailyread schedule install)");
          });
      }
      function clipboardFallback(why) { show("err", "⚠ " + why + " Press ⌘V (Ctrl+V) in the box below instead."); input.focus(); }
      openBtn.addEventListener("click", openDialog);
      closeBtn.addEventListener("click", closeDialog);
      $("add-done").addEventListener("click", closeDialog);
      $("add-again").addEventListener("click", function () { setState("idle"); show("", ""); input.value = ""; pasteBtn.focus(); });
      addDialog.addEventListener("cancel", function (e) { if (state === "busy") { e.preventDefault(); show("", "Still reading the page, please wait a moment…"); } });
      addDialog.addEventListener("click", function (e) { if (e.target === addDialog) closeDialog(); });            // a click on the dimmed backdrop
      addDialog.addEventListener("close", function () {                                                           // show where it landed
        if (!lastAdded) return;
        justAdded = lastAdded; showTab("later"); renderList("later");
        var row = document.querySelector('#panel-later tr.lib-row[data-id="' + (window.CSS && CSS.escape ? CSS.escape(justAdded) : justAdded) + '"]');
        if (row && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
        var mine = lastAdded; lastAdded = "";
        setTimeout(function () { if (justAdded === mine) { justAdded = ""; } }, 3500);
      });
      pasteBtn.addEventListener("click", function () {
        if (!navigator.clipboard || !navigator.clipboard.readText) { clipboardFallback("This browser cannot read the clipboard."); return; }
        navigator.clipboard.readText().then(function (text) {
          var url = urlFrom(text);
          if (!/^https?:\/\//i.test(url)) { show("err", "⚠ There is no link on your clipboard. Copy the post's link first, then click again."); input.focus(); return; }
          run(url);
        }, function () { clipboardFallback("The browser did not allow reading the clipboard."); });
      });
      addDialog.addEventListener("paste", function (e) {          // ⌘V anywhere in the dialog (outside the box) adds the copied link at once
        if (state !== "idle" || e.target === input) return;
        var url = urlFrom((e.clipboardData && e.clipboardData.getData("text")) || "");
        if (/^https?:\/\//i.test(url)) { e.preventDefault(); run(url); }
      });
      box.addEventListener("submit", function (e) { e.preventDefault(); if (state === "idle") run(input.value); });
    })();

    // ----- events -----
    document.addEventListener("click", function (e) {
      var b = e.target.closest ? e.target.closest("[data-act][data-id]") : null;
      if (!b) return;
      var id = b.getAttribute("data-id"), act = b.getAttribute("data-act");
      var rec = lib[id];
      var next = null, t = nowIso(), message = "";
      var title = (rec && rec.title) || (snaps[id] && snaps[id].title) || "item";
      if (act === "later") { var on = !(rec && rec.read_later); next = setFlag(lib, id, snaps[id], "read_later", on, t); message = (on ? "Saved to Read Later: " : "Removed from Read Later: ") + title; }
      else if (act === "fav") { var fav = !(rec && rec.favorite); next = setFlag(lib, id, snaps[id], "favorite", fav, t); message = (fav ? "Added to Favorites: " : "Removed from Favorites: ") + title; }
      else if (act === "read") { var done = !(rec && rec.read); next = setFlag(lib, id, snaps[id], "read", done, t); message = (done ? "Marked as read: " : "Marked as unread: ") + title; }
      else if (act === "remove") { next = setFlag(lib, id, snaps[id], "read_later", false, t); message = "Removed from Read Later: " + title; }
      if (!next) return;
      // The list is rebuilt, so keep the keyboard where it was: on the same button, or on the neighbouring row.
      var panel = b.closest(".lib-panel"), row = b.closest("tr.lib-row");
      var neighbour = row && (row.nextElementSibling || row.previousElementSibling);
      var neighbourId = neighbour && neighbour.getAttribute("data-id");
      lib = next; say(message); persist();           // the action first, then anything persist() has to add (the reminder)
      if (panel) {
        var target = panel.querySelector('tr.lib-row[data-id="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"] [data-act="' + act + '"]');
        if (!target && neighbourId) target = panel.querySelector('tr.lib-row[data-id="' + (window.CSS && CSS.escape ? CSS.escape(neighbourId) : neighbourId) + '"] [data-act]');
        (target || panel.querySelector(".lib-search")).focus();
      }
    });
    all(".lib-search").forEach(function (inp) {
      inp.addEventListener("input", function () {
        var kind = inp.getAttribute("data-kind");
        terms[kind] = inp.value.trim();
        renderList(kind);
      });
    });
    all(".btn-link").forEach(function (b) { b.addEventListener("click", helper ? helperSync : reconnect); });
    all(".btn-export").forEach(function (b) { b.addEventListener("click", exportFile); });
    all(".btn-import").forEach(function (b) { b.addEventListener("click", function () { picker.click(); }); });
    window.addEventListener("storage", function (e) {        // another report tab changed the lists
      if (e.key !== KEY) return;
      lib = mergeMaps(lib, parseDoc(e.newValue || "") || {});
      render();
    });

    saveLocal();     // first visit: store the baked-in lists so other reports see them too
    render();
    if (helper) helperSync();
  }
})();
