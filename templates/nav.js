// Daily Read — prev/next report buttons and the "Reports" table of contents. Needs catalog.js (window.DR_CATALOG,
// newest first) and the #report-meta JSON; without them the bar stays hidden. Everything is built with textContent.
(function () {
  "use strict";
  if (typeof document === "undefined") return;
  document.addEventListener("DOMContentLoaded", function () {
    var nav = document.getElementById("day-nav");
    var cat = window.DR_CATALOG;
    var metaEl = document.getElementById("report-meta");
    var meta = {};
    try { meta = metaEl ? JSON.parse(metaEl.textContent) : {}; } catch (e) {}
    if (!nav || !Array.isArray(cat) || !cat.length) return;
    var at = -1;
    cat.forEach(function (e, i) { if (e.id === meta.stem) at = i; });
    if (at < 0) return;
    var root = meta.root || "";

    function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
    function step(entry, dir) {
      var a = el(entry ? "a" : "span", "nav-btn " + dir + (entry ? "" : " off"));
      var label = entry ? entry.label : (dir === "prev" ? "Oldest" : "Latest");
      a.textContent = dir === "prev" ? "‹ " + label : label + " ›";
      if (entry) { a.href = root + entry.href; a.title = (dir === "prev" ? "Previous report: " : "Next report: ") + entry.label; }
      else a.setAttribute("aria-disabled", "true");
      return a;
    }
    var count = function (n) { return n == null ? "–" : String(n); };

    // A native popover (Baseline 2025): the browser provides top layer, Escape, click-outside, and focus return.
    var btn = el("button", "nav-btn reports-btn");
    var icon = el("span", null, "📅"), caret = el("span", null, "▾");
    icon.setAttribute("aria-hidden", "true"); caret.setAttribute("aria-hidden", "true");
    btn.append(icon, " Reports ", caret);
    btn.type = "button"; btn.setAttribute("popovertarget", "reports-pop");
    var pop = el("div", "reports-pop"); pop.id = "reports-pop"; pop.setAttribute("popover", "auto");
    pop.setAttribute("role", "group"); pop.setAttribute("aria-label", "All reports");
    var head = el("div", "pop-head");
    head.appendChild(el("strong", null, cat.length + " report" + (cat.length === 1 ? "" : "s")));
    var hist = el("a", "pop-hist", "Usage history ↗"); hist.href = root + "index.html"; head.appendChild(hist);
    pop.appendChild(head);
    var table = el("table", "pop-table");
    var thead = el("thead"), hr = el("tr");
    ["Report", "Items", "Must-reads", "Window"].forEach(function (h, i) { var th = el("th", i === 1 || i === 2 ? "num" : null, h); th.scope = "col"; hr.appendChild(th); });
    thead.appendChild(hr); table.appendChild(thead);
    var tbody = el("tbody");
    cat.forEach(function (e, i) {
      var tr = el("tr", i === at ? "current" : "");
      var c1 = el("td"), a = el("a", null, e.label);
      a.href = root + e.href; if (i === at) a.setAttribute("aria-current", "page");
      c1.appendChild(a);
      tr.appendChild(c1);
      tr.appendChild(el("td", "num", count(e.items)));
      tr.appendChild(el("td", "num" + (e.read ? " has-read" : ""), e.read ? "✅ " + e.read : count(e.read)));
      tr.appendChild(el("td", "win", e.window || ""));
      tr.addEventListener("click", function (ev) { if (ev.target !== a) location.href = a.href; });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody); pop.appendChild(table);

    pop.addEventListener("toggle", function (e) {
      if (e.newState !== "open") return;
      var r = btn.getBoundingClientRect();           // under the button, kept inside the window
      pop.style.top = Math.round(r.bottom + 8) + "px";
      pop.style.left = Math.max(12, Math.min(Math.round(r.left), window.innerWidth - pop.offsetWidth - 12)) + "px";
      var cur = pop.querySelector("tr.current a") || pop.querySelector("a");
      if (cur) { cur.focus(); cur.scrollIntoView({ block: "nearest" }); }
    });

    nav.appendChild(step(cat[at + 1], "prev"));
    nav.appendChild(btn);
    nav.appendChild(step(cat[at - 1], "next"));
    nav.appendChild(pop);
    nav.hidden = false;
  });
})();
