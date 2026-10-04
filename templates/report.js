// Daily Read — theme (dark default), filters, section highlighting. Works without storage (private mode etc.).
(function () {
  var root = document.documentElement;
  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  };
  var modes = ["dark", "light", "auto"];
  var labels = { dark: "☾ Dark", light: "☀ Light", auto: "◐ Auto" };
  var mode = store.get("dr-theme");
  if (modes.indexOf(mode) < 0) mode = "dark";
  function applyTheme() {
    root.classList.add("no-anim");                       // switch themes instantly: no half-faded buttons
    root.setAttribute("data-theme", mode);
    if (window.requestAnimationFrame) requestAnimationFrame(function () { requestAnimationFrame(function () { root.classList.remove("no-anim"); }); });
    else root.classList.remove("no-anim");
    var btn = document.getElementById("theme-btn");
    if (btn) { btn.textContent = labels[mode]; btn.setAttribute("aria-label", "Theme: " + mode + ". Click to change."); }
  }
  applyTheme();   // runs in <head>: no flash of the wrong theme

  document.addEventListener("DOMContentLoaded", function () {
    applyTheme();
    var btn = document.getElementById("theme-btn");
    if (btn) btn.addEventListener("click", function () {
      mode = modes[(modes.indexOf(mode) + 1) % modes.length];
      store.set("dr-theme", mode);
      applyTheme();
    });

    var only = document.getElementById("only-read");
    var q = document.getElementById("q");
    var status = document.getElementById("filter-status");
    var rows = Array.prototype.slice.call(document.querySelectorAll("tr.item"));

    function refresh() {
      var term = q ? q.value.trim().toLowerCase() : "";
      var onlyRead = !!(only && only.checked);
      document.body.classList.toggle("only-read", onlyRead);
      rows.forEach(function (tr) {
        tr.classList.toggle("hidden-by-search", !!term && tr.textContent.toLowerCase().indexOf(term) < 0);
      });
      var visible = 0;
      document.querySelectorAll(".section:not(.empty)").forEach(function (sec) {
        var n = sec.querySelectorAll("tr.item:not(.hidden-by-search)" + (onlyRead ? ".is-read" : "")).length;
        sec.classList.toggle("no-match", !!term && n === 0);
        if (term && n) { var d = sec.querySelector("details"); if (d) d.open = true; }
        visible += n;
      });
      document.querySelectorAll(".section.empty").forEach(function (sec) { sec.classList.toggle("no-match", !!term || onlyRead); });
      document.body.classList.toggle("nothing-visible", (term || onlyRead) && visible === 0);
      if (status) {
        if (term || onlyRead) {
          status.hidden = false;
          status.innerHTML = "Showing " + visible + " of " + rows.length + ' <button type="button" id="clear-filters">Show all</button>';
          document.getElementById("clear-filters").addEventListener("click", clearFilters);
        } else { status.hidden = true; status.textContent = ""; }
      }
    }
    function clearFilters() {
      if (only) only.checked = false;
      if (q) q.value = "";
      refresh();
    }
    if (only) only.addEventListener("change", refresh);
    if (q) q.addEventListener("input", refresh);

    // Hash targets: rows open their section, clear filters that hide them, and center; sections align to top.
    function openTarget() {
      var el = location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1)));
      if (!el) return;
      var d = el.closest("details");
      if (d) d.open = true;
      if (el.tagName === "TR") {
        if (el.offsetParent === null) clearFilters();
        el.scrollIntoView({ block: "center" });
      } else {
        el.scrollIntoView({ block: "start" });
      }
    }
    window.addEventListener("hashchange", openTarget);
    openTarget();

    // Sidebar: highlight the section currently in view.
    var links = {};
    document.querySelectorAll(".side-nav a[data-sec]").forEach(function (a) { links[a.getAttribute("data-sec")] = a; });
    if ("IntersectionObserver" in window) {
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (e.isIntersecting && links[e.target.id]) {
            Object.keys(links).forEach(function (k) { links[k].classList.remove("active"); });
            links[e.target.id].classList.add("active");
          }
        });
      }, { rootMargin: "-20% 0px -70% 0px" });
      document.querySelectorAll(".section[id]").forEach(function (s) { io.observe(s); });
    }

    // Printing: expand everything.
    window.addEventListener("beforeprint", function () { document.querySelectorAll("details").forEach(function (d) { d.open = true; }); });
  });
})();
