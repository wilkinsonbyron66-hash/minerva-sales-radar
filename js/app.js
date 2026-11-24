/* Minerva Sales Radar — front end. User state lives in localStorage on this device. */
(function () {
  "use strict";

  var DAY = 86400000, PAGE = 30;
  var KEY = { calls: "msr.calls", snooze: "msr.snooze", hidden: "msr.hidden", filters: "msr.filters", tab: "msr.tab" };
  var state = { items: [], events: [], generated: null, shown: PAGE };

  // ---------- storage ----------
  function load(k, d) { try { var v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch (e) { return d; } }
  function save(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
  var calls = load(KEY.calls, []), snooze = load(KEY.snooze, {}), hidden = load(KEY.hidden, {});

  // ---------- utils ----------
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  var TZ = "Asia/Dubai";
  function fmtDate(iso, withYear) {
    var o = { day: "numeric", month: "short", timeZone: TZ };
    if (withYear) o.year = "numeric";
    return new Date(iso).toLocaleDateString("en-GB", o);
  }
  function dubaiDay(t) { return new Date(t).toLocaleDateString("en-CA", { timeZone: TZ }); } // YYYY-MM-DD
  function daysAgo(iso) {
    var a = new Date(dubaiDay(Date.now())), b = new Date(dubaiDay(iso));
    return Math.round((a - b) / DAY);
  }
  function ago(iso) {
    var d = daysAgo(iso);
    return d <= 0 ? "today" : d === 1 ? "yesterday" : d + " days ago";
  }
  function isHidden(it) {
    if (hidden[it.id]) return "Not relevant";
    if (snooze[it.id] && snooze[it.id] > Date.now()) return "Snoozed to " + fmtDate(new Date(snooze[it.id]).toISOString());
    return "";
  }
  function isNew(it) {
    return state.generated && (new Date(state.generated) - new Date(it.first_seen || it.published)) < 26 * 3600000;
  }
  function inCalls(id) { return calls.some(function (c) { return c.id === id; }); }
  var RANK = { high: 3, medium: 2, low: 1 };
  var SECTION = { tenders: "Live tender", awards: "Award / project" };
  var ICON = {
    phone: "<svg viewBox='0 0 24 24'><path d='M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.5 2.1L8 9.8a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.7.7a2 2 0 0 1 1.7 2z'/></svg>",
    plus: "<svg viewBox='0 0 24 24'><path d='M12 5v14M5 12h14'/></svg>",
    check: "<svg viewBox='0 0 24 24'><path d='M20 6 9 17l-5-5'/></svg>",
    clock: "<svg viewBox='0 0 24 24'><circle cx='12' cy='12' r='9'/><path d='M12 7v5l3 2'/></svg>"
  };

  var toastT;
  function toast(msg) {
    var t = $("toast"); t.textContent = msg; t.classList.add("show");
    clearTimeout(toastT); toastT = setTimeout(function () { t.classList.remove("show"); }, 2200);
  }

  // ---------- data ----------
  function getJSON(path) {
    return fetch(path + "?v=" + Date.now(), { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error(path + " " + r.status); return r.json();
    });
  }
  Promise.all([
    getJSON("data/items.json").catch(function () { return { items: [] }; }),
    getJSON("data/events.json").catch(function () { return { events: [] }; })
  ]).then(function (res) {
    state.items = res[0].items || [];
    state.generated = res[0].generated;
    state.events = res[1].events || [];
    $("updated").textContent = state.generated
      ? "Updated " + new Date(state.generated).toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: TZ })
      : "No data yet — run the workflow once";
    Object.keys(snooze).forEach(function (k) { if (snooze[k] <= Date.now()) delete snooze[k]; });
    save(KEY.snooze, snooze);
    renderAll();
  });

  // ---------- filters ----------
  var F = load(KEY.filters, {});
  F = { section: F.section || "", country: F.country || "", score: F.score || "", sort: F.sort || "newest", q: "", hidden: false, fresh: false };
  $("f-section").value = F.section; $("f-country").value = F.country; $("f-score").value = F.score; $("f-sort").value = F.sort;

  function readFilters() {
    F.section = $("f-section").value; F.country = $("f-country").value; F.score = $("f-score").value;
    F.sort = $("f-sort").value; F.q = $("f-q").value.trim(); F.hidden = $("f-hidden").checked;
    save(KEY.filters, { section: F.section, country: F.country, score: F.score, sort: F.sort });
    state.shown = PAGE;
    renderSignals();
  }
  ["f-section", "f-country", "f-score", "f-sort", "f-hidden"].forEach(function (id) { $(id).addEventListener("change", readFilters); });
  var qT; $("f-q").addEventListener("input", function () { clearTimeout(qT); qT = setTimeout(readFilters, 150); });

  // ---------- tabs ----------
  var tabs = document.querySelectorAll(".tabs button"), ink = document.querySelector(".tab-ink");
  function moveInk() {
    var b = document.querySelector(".tabs button[aria-selected='true']");
    if (!b) return;
    ink.style.width = b.offsetWidth + "px";
    ink.style.transform = "translateX(" + b.offsetLeft + "px)";
  }
  function showTab(name) {
    tabs.forEach(function (b) { b.setAttribute("aria-selected", b.dataset.tab === name ? "true" : "false"); });
    ["signals", "events", "calls"].forEach(function (t) { $("tab-" + t).hidden = t !== name; });
    save(KEY.tab, name); moveInk();
  }
  tabs.forEach(function (b) { b.addEventListener("click", function () { showTab(b.dataset.tab); window.scrollTo({ top: 0, behavior: "smooth" }); }); });
  window.addEventListener("resize", moveInk);
  showTab(load(KEY.tab, "signals"));
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(moveInk);

  // ---------- stats strip ----------
  function renderStats(visible) {
    var hi = 0, ten = 0, aw = 0, nw = 0;
    visible.forEach(function (it) {
      if (it.score === "high") hi++;
      if (it.section === "tenders") ten++; else aw++;
      if (isNew(it)) nw++;
    });
    var tiles = [
      { k: "new", cls: "new", n: nw, l: "New today", on: F.fresh },
      { k: "high", cls: "hi", n: hi, l: "High score", on: F.score === "high" },
      { k: "tenders", cls: "", n: ten, l: "Live tenders", on: F.section === "tenders" },
      { k: "awards", cls: "", n: aw, l: "Awards & projects", on: F.section === "awards" }
    ];
    $("stats").innerHTML = tiles.map(function (t) {
      return "<button class='stat " + t.cls + "' data-stat='" + t.k + "' aria-pressed='" + t.on + "'><span class='n'>" + t.n + "</span><span class='l'>" + t.l + "</span></button>";
    }).join("");
  }
  $("stats").addEventListener("click", function (e) {
    var b = e.target.closest("[data-stat]"); if (!b) return;
    var k = b.dataset.stat;
    if (k === "new") F.fresh = !F.fresh;
    if (k === "high") $("f-score").value = F.score === "high" ? "" : "high";
    if (k === "tenders" || k === "awards") $("f-section").value = F.section === k ? "" : k;
    readFilters();
  });

  // ---------- signals ----------
  function card(it, hideReason, i) {
    var added = inCalls(it.id);
    var who = it.lead
      ? "<b>" + esc(it.lead) + "</b>" + (it.client ? "<span class='client'>client: " + esc(it.client) + "</span>" : "")
      : (it.client ? "<b>" + esc(it.client) + "</b>" : "<span class='none'>Not in headline — open the article</span>");
    var comp = it.compliance && it.compliance.length
      ? "<span class='badge comp' title='Matched: " + esc(it.compliance.join(", ")) + "'>⚠ Check before quoting</span>" : "";
    var sc = it.score.charAt(0).toUpperCase() + it.score.slice(1);
    return "<article class='card item s-" + it.score + (hideReason ? " dim" : "") + "' data-id='" + esc(it.id) + "' style='--i:" + Math.min(i, 12) + "'>" +
      "<div class='badges'>" +
        (isNew(it) && !hideReason ? "<span class='badge new'>New</span>" : "") +
        "<span class='badge " + it.score + "'><span class='dot'></span>" + sc + "</span>" +
        "<span class='badge sec'>" + SECTION[it.section] + "</span>" +
        "<span class='badge'>" + esc(it.country === "Other" ? "Country unclear" : it.country) + "</span>" + comp +
        (hideReason ? "<span class='badge'>" + esc(hideReason) + "</span>" : "") +
      "</div>" +
      "<h3><a href='" + esc(it.link) + "' target='_blank' rel='noopener'>" + esc(it.title) + "</a></h3>" +
      "<div class='meta'>" + esc(it.source) + " · " + ago(it.published) + "</div>" +
      "<p class='why'>" + esc(it.why) + "</p>" +
      "<p class='who'>" + ICON.phone + "<span class='k'>Call</span>" + who + "</p>" +
      "<div class='actions'>" +
        "<button class='btn" + (added ? " added" : "") + "' data-act='call'>" + (added ? ICON.check + "In call list" : ICON.plus + "Call list") + "</button>" +
        (hideReason
          ? "<button class='btn quiet' data-act='restore'>Restore</button>"
          : "<button class='btn ghost' data-act='snooze'>" + ICON.clock + "Snooze 7d</button><button class='btn quiet' data-act='hide'>Not relevant</button>") +
      "</div></article>";
  }

  function groupLabel(iso) {
    var d = daysAgo(iso);
    return d <= 0 ? "Today" : d === 1 ? "Yesterday" : d <= 7 ? "This week" : "Earlier";
  }

  function filtered() {
    var q = F.q.toLowerCase(), hiddenCount = 0, base = [];
    var rows = state.items.filter(function (it) {
      var hr = isHidden(it);
      if (hr) hiddenCount++; else base.push(it);
      if (hr && !F.hidden) return false;
      if (F.fresh && !isNew(it)) return false;
      if (F.section && it.section !== F.section) return false;
      if (F.country && it.country !== F.country) return false;
      if (F.score === "high" && it.score !== "high") return false;
      if (F.score === "medium" && it.score === "low") return false;
      if (q) {
        var hay = [it.title, it.lead, it.client, it.source, it.why, it.country, (it.sectors || []).join(" "),
                   (it.matched && it.matched.product || []).join(" ")].join(" ").toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    });
    rows.sort(function (a, b) {
      if (F.sort === "score" && RANK[b.score] !== RANK[a.score]) return RANK[b.score] - RANK[a.score];
      return b.published < a.published ? -1 : 1;
    });
    return { rows: rows, hiddenCount: hiddenCount, base: base };
  }

  function renderSignals() {
    var r = filtered(), rows = r.rows;
    renderStats(r.base);
    $("c-hidden").textContent = r.hiddenCount ? "(" + r.hiddenCount + ")" : "";
    $("c-signals").textContent = r.base.length || "";
    var highs = rows.filter(function (x) { return x.score === "high"; }).length;
    $("resultline").innerHTML = "<b>" + rows.length + "</b> signal" + (rows.length === 1 ? "" : "s") + (highs ? " · <b>" + highs + "</b> high" : "");
    if (!rows.length) {
      $("list").innerHTML = "<div class='card empty'>" + (state.items.length ? "Nothing matches these filters.<br><br><button class='btn ghost' id='clearf'>Clear filters</button>" : "No signals yet. The daily run fills this at about 06:00 Dubai.") + "</div>";
      $("more").hidden = true; return;
    }
    var page = rows.slice(0, state.shown), html = "", last = "";
    page.forEach(function (it, i) {
      if (F.sort === "newest") {
        var g = groupLabel(it.published);
        if (g !== last) { html += "<h2 class='group-h'>" + g + "</h2>"; last = g; }
      }
      html += card(it, isHidden(it), i);
    });
    $("list").innerHTML = html;
    $("more").hidden = rows.length <= state.shown;
    $("more").textContent = "Show more (" + (rows.length - state.shown) + " left)";
  }
  $("more").addEventListener("click", function () { state.shown += PAGE; renderSignals(); });
  $("list").addEventListener("click", function (e) {
    if (e.target.id !== "clearf") return;
    ["f-section", "f-country", "f-score", "f-q"].forEach(function (id) { $(id).value = ""; });
    F.fresh = false; readFilters();
  });

  function removeCard(el, done) {
    el.style.maxHeight = el.offsetHeight + "px";
    requestAnimationFrame(function () {
      el.classList.add("leaving");
      setTimeout(done, 280);
    });
  }

  $("list").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-act]"); if (!b) return;
    var el = b.closest("[data-id]"), id = el.dataset.id;
    var it = state.items.find(function (x) { return x.id === id; }); if (!it) return;
    var act = b.dataset.act;
    if (act === "call") {
      if (inCalls(id)) { calls = calls.filter(function (c) { return c.id !== id; }); toast("Removed from call list"); }
      else {
        calls.unshift({ id: id, company: it.lead || it.client || "", title: it.title, link: it.link,
                        country: it.country, date: it.published.slice(0, 10), added: new Date().toISOString() });
        toast("Added " + (it.lead || "item") + " to call list");
      }
      save(KEY.calls, calls);
      var added = inCalls(id);
      b.classList.toggle("added", added);
      b.innerHTML = added ? ICON.check + "In call list" : ICON.plus + "Call list";
      renderCalls(); return;
    }
    if (act === "snooze") { snooze[id] = Date.now() + 7 * DAY; save(KEY.snooze, snooze); toast("Snoozed for 7 days"); }
    if (act === "hide") { hidden[id] = true; save(KEY.hidden, hidden); toast("Hidden — tick Show hidden to undo"); }
    if (act === "restore") { delete hidden[id]; delete snooze[id]; save(KEY.hidden, hidden); save(KEY.snooze, snooze); toast("Restored"); renderSignals(); return; }
    if (F.hidden) renderSignals(); else removeCard(el, renderSignals);
  });

  // ---------- events ----------
  function renderEvents() {
    var today = new Date(dubaiDay(Date.now()));
    var evs = state.events.filter(function (e) { return new Date(e.end) >= today; })
      .sort(function (a, b) { return a.start < b.start ? -1 : 1; });
    $("c-events").textContent = evs.length || "";
    $("events").innerHTML = evs.length ? evs.map(function (e, i) {
      var start = new Date(e.start), end = new Date(e.end);
      var days = Math.round((start - today) / DAY);
      var when = days <= 0 ? "On now" : days === 1 ? "Tomorrow" : "In " + days + " days";
      var bdays = Math.round((new Date(e.book_by) - today) / DAY);
      var bcls = bdays < 0 ? "overdue" : bdays <= 14 ? "soon" : "";
      var btxt = fmtDate(e.book_by + "T08:00:00Z", true) + (bdays < 0 ? " · passed" : bdays <= 14 ? " · " + bdays + " days" : "");
      var range = start.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" }) + " – " +
                  end.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
      return "<article class='card item' style='--i:" + i + "'>" +
        "<div class='ev-head'><h3>" + (e.url ? "<a href='" + esc(e.url) + "' target='_blank' rel='noopener'>" + esc(e.name) + "</a>" : esc(e.name)) + "</h3>" +
        "<span class='ev-date'>" + range + "</span></div>" +
        "<div class='badges'><span class='badge " + (days <= 0 ? "new" : "sec") + "'>" + when + "</span>" +
          (e.confirmed ? "<span class='badge high'>Dates confirmed</span>" : "<span class='badge medium'>Unconfirmed</span>") +
          (e.sectors || []).map(function (s) { return "<span class='badge'>" + esc(s) + "</span>"; }).join("") + "</div>" +
        "<p class='why'>" + esc(e.why) + "</p>" +
        "<div class='ev-facts'>" +
          "<div class='fact'><div class='k'>Location</div><div class='v'>" + esc(e.location) + "</div></div>" +
          "<div class='fact'><div class='k'>Venue</div><div class='v'>" + esc(e.venue) + "</div></div>" +
          "<div class='fact " + bcls + "'><div class='k'>Book meetings by</div><div class='v'>" + btxt + "</div></div>" +
        "</div>" +
        (e.note ? "<p class='ev-note'>" + esc(e.note) + "</p>" : "") +
      "</article>";
    }).join("") : "<div class='card empty'>No upcoming events. Add some to data/events.json.</div>";
  }

  // ---------- call list ----------
  function renderCalls() {
    $("c-calls").textContent = calls.length || "";
    $("calls").innerHTML = calls.length ? calls.map(function (c, i) {
      return "<article class='card item call-row' data-cid='" + esc(c.id) + "' style='--i:" + Math.min(i, 12) + "'>" +
        "<div class='top'><b>" + esc(c.company || "Company not detected") + "</b><span class='meta'>" + esc(c.country) + " · " + fmtDate(c.date + "T08:00:00Z", true) + "</span></div>" +
        "<a href='" + esc(c.link) + "' target='_blank' rel='noopener'>" + esc(c.title) + "</a>" +
        "<div class='actions'><button class='btn quiet' data-rm='1'>Remove</button></div></article>";
    }).join("") : "<div class='card empty'>No calls yet. Tap <b>Call list</b> on any signal.</div>";
  }
  $("calls").addEventListener("click", function (e) {
    if (!e.target.closest("[data-rm]")) return;
    var el = e.target.closest("[data-cid]"), id = el.dataset.cid;
    calls = calls.filter(function (c) { return c.id !== id; });
    save(KEY.calls, calls);
    removeCard(el, function () { renderCalls(); renderSignals(); });
  });

  function csvCell(v) { v = String(v == null ? "" : v); return /[",\n\r]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; }
  $("export").addEventListener("click", function () {
    if (!calls.length) { toast("Call list is empty"); return; }
    var lines = [["company", "title", "link", "country", "date"].join(",")].concat(calls.map(function (c) {
      return [c.company, c.title, c.link, c.country, c.date].map(csvCell).join(",");
    }));
    var blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "minerva-call-list-" + dubaiDay(Date.now()) + ".csv";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
    toast("Exported " + calls.length + " rows");
  });
  var clearArmed = false;
  $("clearcalls").addEventListener("click", function () {
    if (!calls.length) return;
    if (!clearArmed) { clearArmed = true; $("clearcalls").textContent = "Tap again to clear"; setTimeout(function () { clearArmed = false; $("clearcalls").textContent = "Clear"; }, 3000); return; }
    calls = []; save(KEY.calls, calls); clearArmed = false; $("clearcalls").textContent = "Clear";
    renderCalls(); renderSignals(); toast("Call list cleared");
  });

  function renderAll() { renderSignals(); renderEvents(); renderCalls(); }

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () { navigator.serviceWorker.register("sw.js").catch(function () {}); });
  }
})();
