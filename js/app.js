/* Minerva Sales Radar - front end. All user state lives in localStorage on this device. */
(function () {
  "use strict";

  var DAY = 86400000;
  var KEY = { calls: "msr.calls", snooze: "msr.snooze", hidden: "msr.hidden", filters: "msr.filters", tab: "msr.tab" };
  var state = { items: [], events: [], generated: null };

  // ---------- storage helpers ----------
  function load(k, dflt) {
    try { var v = localStorage.getItem(k); return v ? JSON.parse(v) : dflt; } catch (e) { return dflt; }
  }
  function save(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }

  var calls = load(KEY.calls, []);      // [{id, company, title, link, country, date}]
  var snooze = load(KEY.snooze, {});    // {id: untilTimestamp}
  var hidden = load(KEY.hidden, {});    // {id: true}

  // ---------- utils ----------
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtDate(iso) {
    var d = new Date(iso);
    return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Dubai" });
  }
  function ago(iso) {
    var days = Math.floor((Date.now() - new Date(iso).getTime()) / DAY);
    if (days <= 0) return "today";
    if (days === 1) return "yesterday";
    return days + " days ago";
  }
  function isHidden(it) {
    if (hidden[it.id]) return "Not relevant";
    if (snooze[it.id] && snooze[it.id] > Date.now()) return "Snoozed until " + fmtDate(new Date(snooze[it.id]).toISOString());
    return "";
  }
  function inCalls(id) { return calls.some(function (c) { return c.id === id; }); }
  var RANK = { high: 3, medium: 2, low: 1 };
  var SECTION = { tenders: "Live tender", awards: "Award / new project" };

  // ---------- data ----------
  function getJSON(path) {
    return fetch(path + "?v=" + Date.now(), { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error(path + " " + r.status);
      return r.json();
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
      ? "Updated " + new Date(state.generated).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Dubai" }) + " Dubai"
      : "No data yet - run the workflow once";
    // clear expired snoozes
    Object.keys(snooze).forEach(function (k) { if (snooze[k] <= Date.now()) delete snooze[k]; });
    save(KEY.snooze, snooze);
    renderAll();
  });

  // ---------- filters ----------
  var F = load(KEY.filters, { section: "", country: "", score: "", sort: "newest", q: "", hidden: false });
  $("f-section").value = F.section; $("f-country").value = F.country; $("f-score").value = F.score;
  $("f-sort").value = F.sort; $("f-q").value = F.q; $("f-hidden").checked = !!F.hidden;

  ["f-section", "f-country", "f-score", "f-sort", "f-hidden"].forEach(function (id) {
    $(id).addEventListener("change", readFilters);
  });
  $("f-q").addEventListener("input", readFilters);
  function readFilters() {
    F = { section: $("f-section").value, country: $("f-country").value, score: $("f-score").value,
          sort: $("f-sort").value, q: $("f-q").value.trim(), hidden: $("f-hidden").checked };
    save(KEY.filters, F);
    renderSignals();
  }

  // ---------- tabs ----------
  var tabs = document.querySelectorAll(".tabs button");
  function showTab(name) {
    tabs.forEach(function (b) { b.setAttribute("aria-selected", b.dataset.tab === name ? "true" : "false"); });
    ["signals", "events", "calls"].forEach(function (t) { $("tab-" + t).hidden = t !== name; });
    save(KEY.tab, name);
  }
  tabs.forEach(function (b) { b.addEventListener("click", function () { showTab(b.dataset.tab); }); });
  showTab(load(KEY.tab, "signals"));

  // ---------- render: signals ----------
  function card(it, hideReason) {
    var added = inCalls(it.id);
    var who = it.lead
      ? "<b>" + esc(it.lead) + "</b>" + (it.client ? " <span class='meta'>client: " + esc(it.client) + "</span>" : "")
      : (it.client ? "<b>" + esc(it.client) + "</b>" : "<span class='none'>Not detected - open the article</span>");
    var comp = it.compliance && it.compliance.length
      ? "<span class='badge comp' title='Matched: " + esc(it.compliance.join(", ")) + "'>⚠ Check before quoting</span>" : "";
    return "<article class='card item" + (hideReason ? " dim" : "") + "' data-id='" + esc(it.id) + "'>" +
      "<div class='badges'>" +
        "<span class='badge " + it.score + "'><span class='dot'></span>" + it.score.charAt(0).toUpperCase() + it.score.slice(1) + "</span>" +
        "<span class='badge sec'>" + SECTION[it.section] + "</span>" +
        "<span class='badge'>" + esc(it.country === "Other" ? "Country unclear" : it.country) + "</span>" + comp +
        (hideReason ? "<span class='badge'>" + esc(hideReason) + "</span>" : "") +
      "</div>" +
      "<h3><a href='" + esc(it.link) + "' target='_blank' rel='noopener'>" + esc(it.title) + "</a></h3>" +
      "<div class='meta'>" + esc(it.source) + " · " + fmtDate(it.published) + " (" + ago(it.published) + ")</div>" +
      "<p class='why'>" + esc(it.why) + "</p>" +
      "<p class='who'><span class='meta'>Who to call:</span> " + who + "</p>" +
      "<div class='actions'>" +
        "<button class='btn" + (added ? " added" : "") + "' data-act='call'>" + (added ? "✓ In call list" : "Add to call list") + "</button>" +
        (hideReason
          ? "<button class='btn quiet' data-act='restore'>Restore</button>"
          : "<button class='btn ghost' data-act='snooze'>Snooze 7 days</button><button class='btn quiet' data-act='hide'>Not relevant</button>") +
      "</div></article>";
  }

  function renderSignals() {
    var q = F.q.toLowerCase();
    var hiddenCount = 0;
    var rows = state.items.filter(function (it) {
      var hr = isHidden(it);
      if (hr) hiddenCount++;
      if (hr && !F.hidden) return false;
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
    $("c-hidden").textContent = hiddenCount ? "(" + hiddenCount + ")" : "";
    $("c-signals").textContent = state.items.length - hiddenCount || "";
    var highs = rows.filter(function (r) { return r.score === "high"; }).length;
    $("resultline").textContent = rows.length + " signal" + (rows.length === 1 ? "" : "s") + (highs ? " · " + highs + " high" : "");
    $("list").innerHTML = rows.length
      ? rows.map(function (it) { return card(it, isHidden(it)); }).join("")
      : "<div class='card empty'>" + (state.items.length ? "Nothing matches these filters." : "No signals yet. The daily run fills this at 06:00 Dubai, or run it now from the Actions tab.") + "</div>";
  }

  $("list").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-act]");
    if (!b) return;
    var id = b.closest("[data-id]").dataset.id;
    var it = state.items.find(function (x) { return x.id === id; });
    if (!it) return;
    var act = b.dataset.act;
    if (act === "call") {
      if (inCalls(id)) calls = calls.filter(function (c) { return c.id !== id; });
      else calls.unshift({ id: id, company: it.lead || it.client || "", title: it.title, link: it.link,
                           country: it.country, date: it.published.slice(0, 10), added: new Date().toISOString() });
      save(KEY.calls, calls);
    } else if (act === "snooze") {
      snooze[id] = Date.now() + 7 * DAY; save(KEY.snooze, snooze);
    } else if (act === "hide") {
      hidden[id] = true; save(KEY.hidden, hidden);
    } else if (act === "restore") {
      delete hidden[id]; delete snooze[id]; save(KEY.hidden, hidden); save(KEY.snooze, snooze);
    }
    renderSignals(); renderCalls();
  });

  // ---------- render: events ----------
  function renderEvents() {
    var today = new Date(); today.setHours(0, 0, 0, 0);
    var evs = state.events.filter(function (e) { return new Date(e.end + "T23:59:59") >= today; })
      .sort(function (a, b) { return a.start < b.start ? -1 : 1; });
    $("c-events").textContent = evs.length || "";
    $("events").innerHTML = evs.length ? evs.map(function (e) {
      var start = new Date(e.start + "T00:00:00"), end = new Date(e.end + "T00:00:00");
      var days = Math.round((start - today) / DAY);
      var when = days <= 0 ? "On now" : days === 1 ? "Tomorrow" : "In " + days + " days";
      var book = new Date(e.book_by + "T00:00:00");
      var bdays = Math.round((book - today) / DAY);
      var bcls = bdays < 0 ? "overdue" : bdays <= 14 ? "soon" : "";
      var btxt = fmtDate(e.book_by + "T08:00:00Z") + (bdays < 0 ? " (passed)" : bdays <= 14 ? " (" + bdays + " days)" : "");
      var range = start.toLocaleDateString("en-GB", { day: "numeric", month: "short" }) + " – " +
                  end.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
      return "<article class='card item'>" +
        "<div class='ev-head'><h3>" + (e.url ? "<a href='" + esc(e.url) + "' target='_blank' rel='noopener'>" + esc(e.name) + "</a>" : esc(e.name)) + "</h3>" +
        "<span class='ev-date'>" + range + "</span></div>" +
        "<div class='badges'><span class='badge sec'>" + when + "</span>" +
          (e.confirmed ? "<span class='badge high'>Dates confirmed</span>" : "<span class='badge medium'>Unconfirmed</span>") +
          (e.sectors || []).map(function (s) { return "<span class='badge'>" + esc(s) + "</span>"; }).join("") + "</div>" +
        "<p class='why'>" + esc(e.why) + "</p>" +
        "<div class='ev-facts'>" +
          "<div class='fact'><div class='k'>Location</div><div class='v'>" + esc(e.location) + "</div></div>" +
          "<div class='fact'><div class='k'>Venue</div><div class='v'>" + esc(e.venue) + "</div></div>" +
          "<div class='fact " + bcls + "'><div class='k'>Book meetings by</div><div class='v'>" + btxt + "</div></div>" +
        "</div>" +
        (e.note ? "<p class='note'>" + esc(e.note) + "</p>" : "") +
      "</article>";
    }).join("") : "<div class='card empty'>No upcoming events. Add some to data/events.json.</div>";
  }

  // ---------- render: call list ----------
  function renderCalls() {
    $("c-calls").textContent = calls.length || "";
    $("calls").innerHTML = calls.length ? calls.map(function (c) {
      return "<article class='card item' data-cid='" + esc(c.id) + "'>" +
        "<p class='who'><b>" + esc(c.company || "Company not detected") + "</b><span class='meta'>" + esc(c.country) + " · " + esc(c.date) + "</span></p>" +
        "<h3><a href='" + esc(c.link) + "' target='_blank' rel='noopener'>" + esc(c.title) + "</a></h3>" +
        "<div class='actions'><button class='btn quiet' data-rm='1'>Remove</button></div></article>";
    }).join("") : "<div class='card empty'>No calls yet. Tap “Add to call list” on any signal.</div>";
  }
  $("calls").addEventListener("click", function (e) {
    if (!e.target.closest("[data-rm]")) return;
    var id = e.target.closest("[data-cid]").dataset.cid;
    calls = calls.filter(function (c) { return c.id !== id; });
    save(KEY.calls, calls); renderCalls(); renderSignals();
  });

  function csvCell(v) {
    v = String(v == null ? "" : v);
    return /[",\n\r]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
  }
  function toast(msg) { $("toast").textContent = msg; setTimeout(function () { $("toast").textContent = ""; }, 2500); }

  $("export").addEventListener("click", function () {
    if (!calls.length) { toast("Call list is empty"); return; }
    var lines = [["company", "title", "link", "country", "date"].join(",")].concat(calls.map(function (c) {
      return [c.company, c.title, c.link, c.country, c.date].map(csvCell).join(",");
    }));
    var blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "minerva-call-list-" + new Date().toISOString().slice(0, 10) + ".csv";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
    toast("Exported " + calls.length + " rows");
  });
  $("clearcalls").addEventListener("click", function () {
    if (!calls.length) return;
    calls = []; save(KEY.calls, calls); renderCalls(); renderSignals(); toast("List cleared");
  });

  function renderAll() { renderSignals(); renderEvents(); renderCalls(); }

  // ---------- installable app ----------
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () { navigator.serviceWorker.register("sw.js").catch(function () {}); });
  }
})();
