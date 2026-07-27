/* app.js — page logic. Reads data/slate.json, renders plain-language cards,
   and refuses to show a confident number when the inputs behind it are thin. */
(function () {
"use strict";
var M = window.Model;

/* storage that degrades to memory when the browser refuses */
var mem = {};
var store = {
  get: function (k) { try { var v = localStorage.getItem(k); return v === null ? (k in mem ? mem[k] : null) : v; }
                      catch (e) { return k in mem ? mem[k] : null; } },
  set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) { mem[k] = v; } }
};

var SLATE = null, CURRENT = null, HIDE_WEAK = false, OPEN = {};

function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
}
function fmtTime(iso) {
  if (!iso) return "time TBD";
  var d = new Date(iso);
  return isNaN(d) ? "time TBD" : d.toLocaleString(undefined,
    { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
function outOf100(p) { return Math.round(p * 100); }

/* ————— tabs ————— */
var TABS = ["games", "learn", "picks", "adv"];
document.querySelectorAll("nav button").forEach(function (b) {
  b.addEventListener("click", function () {
    document.querySelectorAll("nav button").forEach(function (x) {
      x.setAttribute("aria-selected", x === b);
    });
    TABS.forEach(function (t) { $("tab-" + t).hidden = (t !== b.dataset.tab); });
    if (b.dataset.tab === "picks") renderPicks();
    if (b.dataset.tab === "adv") { renderDiag(); renderManual(); }
  });
});

/* ————— load ————— */
function load() {
  $("games-status").textContent = "Loading…";
  fetch("data/slate.json?t=" + Date.now())
    .then(function (r) { if (!r.ok) throw new Error("data/slate.json not found"); return r.json(); })
    .then(function (j) { SLATE = j; renderAll(); })
    .catch(function (err) { SLATE = null; renderEmpty(err.message); });
}

function renderEmpty(msg) {
  $("health").innerHTML =
    '<div class="banner bad"><b>No data yet</b>' +
    '<div class="small">' + esc(msg) + '. The site reads a file that a scheduled job writes. ' +
    'Open the repo on GitHub, go to Actions, and run <b>Refresh slate</b> once. ' +
    'It takes about a minute.</div></div>';
  $("games-status").textContent = "";
  $("cards").innerHTML = "";
}

/* ————— health banner ————— */
function renderHealth() {
  var diag = SLATE.diagnostics || {};
  var keys = Object.keys(diag);
  var bad = keys.filter(function (k) { return !diag[k].ok; });
  var games = SLATE.games || [];
  var priced = games.filter(function (g) { return g.kalshi; }).length;
  var usable = games.filter(function (g) { return M.coverage(g).usable; }).length;

  var cls, title, body;
  if (!keys.length) {
    cls = "warn"; title = "Older data file";
    body = "This slate was built before diagnostics existed. Re-run Refresh slate to see source-by-source status.";
  } else if (!bad.length && priced === games.length) {
    cls = "good"; title = "Everything loaded";
    body = usable + " of " + games.length + " games have full inputs, and all " + priced +
           " are matched to a Kalshi market.";
  } else if (usable === 0) {
    cls = "bad"; title = "Predictions are not usable right now";
    body = "The feed could not supply starting-pitcher or bullpen quality, so the model has " +
           "nothing but records and home field to work with. Percentages are hidden below " +
           "rather than shown, because a number built this way is close to a coin flip.";
  } else {
    cls = "warn"; title = "Some inputs are missing";
    body = usable + " of " + games.length + " games have enough data to trust. " +
           priced + " matched a Kalshi market.";
  }
  if (bad.length) body += " Failing: " + bad.join(", ") + ". See the Advanced tab.";

  $("health").innerHTML = '<div class="banner ' + cls + '"><b>' + esc(title) +
    '</b><div class="small">' + esc(body) + '</div></div>';
}

/* ————— cards ————— */
function verdictFor(cov, cmp) {
  if (!cov.usable) {
    return { cls: "v-blocked", text: "Not enough data to make a real estimate. Missing: " +
      cov.missing.map(function (m) { return m.label.toLowerCase(); }).join(", ") + "." };
  }
  if (!cmp) {
    return { cls: "v-no", text: "No Kalshi market matched this game, so there's nothing to compare against." };
  }
  if (cmp.unknownSide) {
    return { cls: "v-blocked", text: "A market was found but we couldn't tell which team it pays out on. Ignore the price here." };
  }
  var net = cmp.netEdge;
  if (net === null) return { cls: "v-no", text: "No price available for this market yet." };
  var pts = Math.round(net * 100);
  if (net > 0.03) {
    return { cls: "v-look", text: "The model thinks this contract is about " + pts +
      " points cheap, after the price and fee. That's a disagreement worth investigating — " +
      "usually it means an input is stale, occasionally it means the market is slow." };
  }
  if (net > 0) {
    return { cls: "v-thin", text: "Barely cheaper than break-even (" + pts +
      " points). Too thin to distinguish from noise in the model." };
  }
  return { cls: "v-no", text: "The market's price is at or above what the model thinks it's worth. Nothing to do here." };
}

function coverageChips(cov) {
  return cov.missing.length === 0
    ? '<span class="chip ok">All 6 inputs</span>'
    : '<span class="chip ' + (cov.usable ? "" : "no") + '">' + cov.have + " of " + cov.total +
      ' inputs</span> ' + cov.missing.map(function (m) {
        return '<span class="chip' + (m.critical ? " no" : "") + '">no ' +
               esc(m.label.toLowerCase()) + "</span>";
      }).join(" ");
}

function renderCards() {
  var games = (SLATE.games || []).slice().sort(function (a, b) {
    return new Date(a.startsAt || 0) - new Date(b.startsAt || 0);
  });
  var shown = games.filter(function (g) { return !HIDE_WEAK || M.coverage(g).usable; });

  $("games-status").textContent = shown.length + " of " + games.length + " games" +
    (SLATE.generatedAt ? " · data from " + fmtTime(SLATE.generatedAt) : "");

  if (!shown.length) {
    $("cards").innerHTML = '<div class="panel">No games to show.</div>';
    return;
  }

  $("cards").innerHTML = shown.map(function (g) {
    var t = M.fromSlate(g), r = M.predict(t.home, t.away);
    var cov = M.coverage(g);
    var cmp = M.compareMarket(r.p, g.kalshi);
    var homeFav = r.p >= 0.5;
    var favName = homeFav ? (g.home.nick || g.home.name) : (g.away.nick || g.away.name);
    var favP = homeFav ? r.p : 1 - r.p;
    var v = verdictFor(cov, cmp);
    var open = !!OPEN[g.gamePk];

    var pickHtml = cov.usable
      ? '<div class="who">Model favours</div>' +
        '<div class="pct" style="color:' + (homeFav ? "var(--sodium)" : "var(--night)") + '">' +
        outOf100(favP) + '<span style="font-size:18px">%</span></div>' +
        '<div class="who">' + esc(favName) + "</div>"
      : '<div class="who">Model favours</div>' +
        '<div class="pct" style="color:var(--ash)">—</div>' +
        '<div class="who">not enough data</div>';

    var cells = "";
    if (cov.usable) {
      cells += '<div class="cell"><div class="k">Our estimate</div><div class="v">' +
        outOf100(favP) + ' in 100</div></div>';
      if (cmp && !cmp.unknownSide && cmp.marketYes !== null) {
        var sideIsFav = (cmp.best === "yes") === cmp.yesIsHome === homeFav;
        var leg = cmp.bestLeg;
        var mktTeam = (cmp.best === "yes") === cmp.yesIsHome
          ? (g.home.nick || g.home.name) : (g.away.nick || g.away.name);
        cells += '<div class="cell"><div class="k">Kalshi price</div><div class="v">' +
          Math.round(leg.ask * 100) + '¢<span style="font-size:12px;color:var(--ash)"> ' +
          esc(mktTeam) + " " + cmp.best.toUpperCase() + "</span></div></div>";
        cells += '<div class="cell"><div class="k">Break-even</div><div class="v">' +
          outOf100(leg.breakEven) + ' in 100</div></div>';
        var netPts = Math.round(cmp.netEdge * 100);
        cells += '<div class="cell"><div class="k">Difference</div><div class="v ' +
          (cmp.netEdge > 0 ? "up" : "down") + '">' + (netPts >= 0 ? "+" : "−") +
          Math.abs(netPts) + '</div></div>';
      } else {
        cells += '<div class="cell"><div class="k">Kalshi price</div>' +
          '<div class="v" style="color:var(--ash)">—</div></div>';
      }
    }

    return '<article class="card' + (cov.usable ? "" : " dim") + '">' +
      '<div class="card-top"><div>' +
        '<div class="teams">' + esc(g.away.nick || g.away.name) + " at " +
          esc(g.home.nick || g.home.name) +
          (g.lineupConfirmed ? ' <span class="chip ok">lineups in</span>' : "") + "</div>" +
        '<div class="when">' + esc(fmtTime(g.startsAt)) + " · " +
          esc((g.away.starter || "starter TBD")) + " vs " +
          esc((g.home.starter || "starter TBD")) + "</div>" +
      '</div><div class="pickbox">' + pickHtml + "</div></div>" +
      (cov.usable ? '<div class="bar"><i class="a" style="width:' + ((1 - r.p) * 100) +
        '%"></i><i class="h" style="width:' + (r.p * 100) + '%"></i></div>' : "") +
      (cells ? '<div class="card-mid">' + cells + "</div>" : "") +
      '<div class="verdict ' + v.cls + '"><span class="dot"></span><span>' + esc(v.text) + "</span></div>" +
      '<div class="card-foot"><div>' + coverageChips(cov) + "</div>" +
        '<div class="row"><button class="act tiny toggle" data-pk="' + g.gamePk + '">' +
          (open ? "Hide details" : "Show details") + "</button>" +
          (cov.usable ? '<button class="act tiny logpick" data-pk="' + g.gamePk +
            '">Log this pick</button>' : "") + "</div></div>" +
      (open ? detailHtml(g, t, r, cov, cmp) : "");
  }).join("");
}

function detailHtml(g, t, r, cov, cmp) {
  function rowsFor(side, tm, key) {
    var s = g[key];
    return [
      ["Record", s.wins !== null && s.wins !== undefined ? s.wins + "–" + s.losses : "—"],
      ["Starter", (s.starter || "TBD") + (s.starterHand ? " (" + s.starterHand + ")" : "")],
      ["Starter FIP", s.sp_rate !== null && s.sp_rate !== undefined ? s.sp_rate.toFixed(2) : "not supplied"],
      ["Expected innings", s.sp_ip ? s.sp_ip.toFixed(1) : "default 5.5"],
      ["Bullpen FIP", s.bp_rate !== null && s.bp_rate !== undefined
        ? s.bp_rate.toFixed(2) + (s.bp_is_proxy ? " (staff proxy)" : "") : "not supplied"],
      ["Relief innings, 3d", s.bp_ip3 !== null && s.bp_ip3 !== undefined ? s.bp_ip3.toFixed(1) : "—"],
      ["Lineup wOBA vs hand", s.woba_vs_hand ? s.woba_vs_hand.toFixed(4) : "not supplied"],
      ["Team wOBA overall", s.woba_overall ? s.woba_overall.toFixed(4) : "not supplied"],
      ["Staff FIP", s.staff_fip ? s.staff_fip.toFixed(2) : "not supplied"]
    ].map(function (x) {
      return "<tr><td>" + x[0] + '</td><td class="num">' + esc(x[1]) + "</td></tr>";
    }).join("");
  }
  var terms = [
    ["Team strength", r.baseLogit, (r.p0 * 100).toFixed(1) + "% before anything else"],
    ["Home field", r.hfaLogit, "flat ×" + M.K.HFA_ODDS.toFixed(2)],
    ["Pitching", r.pitchDelta / M.K.RUNS_PER_WIN * M.K.LOGODDS_PER_WIN,
      (r.pitchDelta >= 0 ? "+" : "−") + Math.abs(r.pitchDelta).toFixed(2) + " runs to home"],
    ["Lineup matchup", r.offDelta / M.K.RUNS_PER_WIN * M.K.LOGODDS_PER_WIN,
      (r.offDelta >= 0 ? "+" : "−") + Math.abs(r.offDelta).toFixed(2) + " runs to home"],
    ["Travel & park", (r.travelDelta + r.parkDelta + r.histDelta) / M.K.RUNS_PER_WIN * M.K.LOGODDS_PER_WIN,
      (r.travelDelta + r.parkDelta >= 0 ? "+" : "−") +
      Math.abs(r.travelDelta + r.parkDelta).toFixed(3) + " runs to home"]
  ];
  return '<div class="detail">' +
    '<p class="eyebrow" style="margin-top:14px">Where the number comes from</p>' +
    '<table class="grid">' + terms.map(function (x) {
      return "<tr><td>" + x[0] + '</td><td style="color:var(--ash)">' + esc(x[2]) +
        '</td><td class="num" style="color:' + (x[1] >= 0 ? "var(--sodium)" : "var(--night)") +
        '">' + (x[1] >= 0 ? "+" : "−") + Math.abs(x[1]).toFixed(3) + "</td></tr>";
    }).join("") + "</table>" +
    '<div class="two" style="margin-top:16px">' +
      '<div><p class="eyebrow">' + esc(g.away.nick || g.away.name) + " (away)</p>" +
        '<table class="grid">' + rowsFor("away", t.away, "away") + "</table></div>" +
      '<div><p class="eyebrow">' + esc(g.home.nick || g.home.name) + " (home)</p>" +
        '<table class="grid">' + rowsFor("home", t.home, "home") + "</table></div>" +
    "</div>" +
    (cov.missing.length ? '<p class="hint"><b>Missing inputs:</b> ' +
      cov.missing.map(function (m) { return esc(m.label) + " — " + esc(m.why); }).join("; ") +
      "</p>" : "") +
    '<div class="row" style="margin-top:12px">' +
      '<button class="act tiny sendadv" data-pk="' + g.gamePk + '">Open in Advanced</button>' +
    "</div></div>";
}

$("cards").addEventListener("click", function (e) {
  var b = e.target.closest("button"); if (!b) return;
  var pk = parseInt(b.dataset.pk, 10);
  var g = (SLATE.games || []).filter(function (x) { return x.gamePk === pk; })[0];
  if (!g) return;
  if (b.classList.contains("toggle")) { OPEN[pk] = !OPEN[pk]; renderCards(); }
  else if (b.classList.contains("logpick")) { logPick(g); }
  else if (b.classList.contains("sendadv")) { selectGame(g); }
});

$("btn-hide-weak").addEventListener("click", function () {
  HIDE_WEAK = !HIDE_WEAK;
  this.setAttribute("aria-pressed", HIDE_WEAK);
  this.classList.toggle("on", HIDE_WEAK);
  renderCards();
});
$("btn-reload").addEventListener("click", load);

function renderAll() { renderHealth(); renderCards(); renderDiag(); }

/* ————— diagnostics ————— */
function renderDiag() {
  if (!SLATE) { $("diag-table").innerHTML = "<tr><td>No slate loaded.</td></tr>"; return; }
  var d = SLATE.diagnostics || {};
  var keys = Object.keys(d);
  $("diag-table").innerHTML = keys.length
    ? "<tr><th>Source</th><th>Status</th><th>Detail</th></tr>" + keys.map(function (k) {
        return "<tr><td>" + esc(k) + '</td><td class="' + (d[k].ok ? "up" : "down") + '">' +
          (d[k].ok ? "ok" : "FAILED") + "</td><td>" + esc(d[k].detail) + "</td></tr>";
      }).join("")
    : "<tr><td>This slate predates diagnostics. Re-run Refresh slate.</td></tr>";

  var sample = SLATE.kalshiSample || [];
  var unmatched = (SLATE.games || []).filter(function (g) { return !g.kalshi; });
  $("kalshi-sample").innerHTML = sample.length
    ? '<p class="eyebrow" style="margin-top:18px">Raw Kalshi markets (first ' + sample.length + ")</p>" +
      '<p class="hint" style="margin-top:0">' + unmatched.length + " game(s) matched nothing. " +
      "If these tickers don't look like the pattern the matcher expects, adjust " +
      "<span class='mono'>match_kalshi</span> in build_slate.py.</p>" +
      '<table class="grid" style="margin-top:10px"><tr><th>Ticker</th><th>YES side</th>' +
      '<th class="num">Bid</th><th class="num">Ask</th></tr>' +
      sample.map(function (m) {
        return "<tr><td>" + esc(m.ticker) + "</td><td>" + esc(m.subtitle || "—") +
          '</td><td class="num">' + (m.yesBid === null ? "—" : m.yesBid) +
          '</td><td class="num">' + (m.yesAsk === null ? "—" : m.yesAsk) + "</td></tr>";
      }).join("") + "</table>"
    : (SLATE.diagnostics && SLATE.diagnostics.kalshi && !SLATE.diagnostics.kalshi.ok
        ? '<p class="hint">Kalshi returned nothing. Detail above.</p>' : "");
}

/* ————— manual matchup (advanced) ————— */
var FIELDS = [
  { k: "name", label: "Team", type: "text", full: true },
  { k: "wins", label: "Wins", type: "number", step: "1" },
  { k: "losses", label: "Losses", type: "number", step: "1" },
  { k: "starter", label: "Starter", type: "text", full: true },
  { k: "sp_rate", label: "Starter FIP", type: "number", step: "0.01" },
  { k: "sp_ip", label: "Expected IP", type: "number", step: "0.1" },
  { k: "bp_rate", label: "Bullpen FIP", type: "number", step: "0.01" },
  { k: "bp_ip3", label: "Relief IP, 3d", type: "number", step: "0.1" },
  { k: "woba_vs_hand", label: "wOBA vs hand", type: "number", step: "0.001" },
  { k: "woba_overall", label: "wOBA overall", type: "number", step: "0.001" },
  { k: "lineup_woba", label: "Lineup wOBA", type: "number", step: "0.001" },
  { k: "staff_fip", label: "Staff FIP", type: "number", step: "0.01" }
];
var KEYS = [];
["a_", "h_"].forEach(function (p) { FIELDS.forEach(function (f) { KEYS.push(p + f.k); }); });

function card(side, prefix, title) {
  return '<div><p class="eyebrow">' + title + "</p>" +
    '<div class="fields">' + FIELDS.map(function (f) {
      return '<div class="' + (f.full ? "full" : "") + '"><label for="' + prefix + f.k + '">' +
        f.label + '</label><input id="' + prefix + f.k + '" type="' + f.type + '"' +
        (f.step ? ' step="' + f.step + '"' : "") + "></div>";
    }).join("") + "</div></div>";
}
$("slate").innerHTML = card("away", "a_", "Away") + card("home", "h_", "Home");

function val(id) { var e = $(id); return e ? e.value : ""; }
function readTeam(p) {
  var o = { name: val(p + "name") || (p === "h_" ? "Home" : "Away") };
  FIELDS.forEach(function (f) { if (f.k !== "name") o[f.k] = val(p + f.k); });
  return M.teamDay(o);
}
function renderManual() {
  var away = readTeam("a_"), home = readTeam("h_");
  var r = M.predict(home, away);
  var homeFav = r.p >= 0.5;
  $("adv-out").innerHTML =
    '<div class="big" style="color:' + (homeFav ? "var(--sodium)" : "var(--night)") + '">' +
    esc(homeFav ? home.name : away.name) + " " + (Math.max(r.p, 1 - r.p) * 100).toFixed(1) + "%</div>" +
    '<div class="status" style="margin-top:6px">home ' + (r.p * 100).toFixed(1) +
    "% · fair odds " + M.americanOdds(Math.max(r.p, 1 - r.p)) +
    " · net " + (r.runDelta >= 0 ? "+" : "−") + Math.abs(r.runDelta).toFixed(2) +
    " runs to home · strength " + (r.homeStrength * 100).toFixed(1) + " vs " +
    (r.awayStrength * 100).toFixed(1) + "</div>";
}
$("slate").addEventListener("input", renderManual);

function selectGame(g) {
  CURRENT = g;
  [["a_", g.away], ["h_", g.home]].forEach(function (x) {
    FIELDS.forEach(function (f) {
      var el = $(x[0] + f.k);
      var v = f.k === "name" ? (x[1].name || "") : x[1][f.k];
      el.value = (v === null || v === undefined) ? "" : v;
    });
  });
  document.querySelectorAll("nav button").forEach(function (b) {
    b.setAttribute("aria-selected", b.dataset.tab === "adv");
  });
  TABS.forEach(function (t) { $("tab-" + t).hidden = (t !== "adv"); });
  renderManual(); renderDiag();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* ————— constants ————— */
var CONST_META = {
  REGRESSION_GAMES: "Games of ballast toward .500", REC_WEIGHT: "Weight on W-L record",
  LG_WOBA: "League wOBA", LG_RPG: "League runs per game", TEAM_PA: "Team PA per game",
  PYTH_EXP: "Pythagorean exponent", LEAGUE_RATE: "League ERA / FIP",
  RUNS_PER_WIN: "Runs per win", HFA_ODDS: "Home-field odds", LOGODDS_PER_WIN: "Log-odds per win",
  DEFAULT_SP_IP: "Default starter IP", FATIGUE_FREE_IP3: "Free relief IP over 3 days",
  FATIGUE_RUNS_PER_IP: "Runs per extra relief IP", WOBA_TO_RUNS: "wOBA-to-runs scale",
  SP_TRUST: "Trust in starter rates", BP_TRUST: "Trust in bullpen rates",
  CAL_A: "Platt intercept", CAL_B: "Platt slope",
  PA_VS_STARTER: "PA vs starter", PITCHES_VS_STARTER: "Pitches vs starter"
};
function buildConstants() {
  $("const-grid").innerHTML = Object.keys(CONST_META).map(function (k) {
    return '<div><label for="c_' + k + '">' + CONST_META[k] + '</label>' +
      '<input id="c_' + k + '" type="number" step="0.01" value="' + M.K[k] + '"></div>';
  }).join("");
}
buildConstants();
(function () {
  var raw = store.get("v5.constants");
  if (raw) {
    try {
      var o = JSON.parse(raw);
      Object.keys(CONST_META).forEach(function (k) { if (isFinite(o[k])) M.K[k] = o[k]; });
      buildConstants();
    } catch (e) { /* ignore a corrupt blob */ }
  }
})();
$("const-grid").addEventListener("input", function () {
  Object.keys(CONST_META).forEach(function (k) {
    var v = parseFloat(val("c_" + k)); if (isFinite(v)) M.K[k] = v;
  });
  store.set("v5.constants", JSON.stringify(M.K));
  renderManual(); if (SLATE) renderCards();
});
$("btn-const-reset").addEventListener("click", function () {
  Object.keys(M.DEFAULT_K).forEach(function (k) { M.K[k] = M.DEFAULT_K[k]; });
  buildConstants(); store.set("v5.constants", JSON.stringify(M.K));
  renderManual(); if (SLATE) renderCards();
});

/* ————— pick log ————— */
var CSV_FIELDS = ["date", "pick", "opponent", "prob", "won", "notes"];
function rows() { var r = store.get("v5.picks"); try { return r ? JSON.parse(r) : []; } catch (e) { return []; } }
function saveRows(rs) { store.set("v5.picks", JSON.stringify(rs)); }
function today() {
  var d = new Date();
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") +
         "-" + String(d.getDate()).padStart(2, "0");
}

function logPick(g) {
  var t = M.fromSlate(g), r = M.predict(t.home, t.away);
  var homeFav = r.p >= 0.5;
  var cmp = M.compareMarket(r.p, g.kalshi);
  var rs = rows();
  rs.push({
    date: today(),
    pick: homeFav ? g.home.name : g.away.name,
    opponent: homeFav ? g.away.name : g.home.name,
    prob: (homeFav ? r.p : 1 - r.p).toFixed(4),
    won: "",
    notes: (homeFav ? "home" : "away") + " · v5" +
      (cmp && cmp.marketYes !== null && !cmp.unknownSide
        ? " · kalshi " + Math.round(cmp.marketYes * 100) + "¢ · net " +
          Math.round(cmp.netEdge * 100) + "pts" : "") +
      " · " + M.coverage(g).have + "/6 inputs"
  });
  saveRows(rs);
  var btn = document.querySelector('.logpick[data-pk="' + g.gamePk + '"]');
  if (btn) { btn.textContent = "Logged"; setTimeout(function () { renderCards(); }, 1200); }
}

function renderPicks() {
  var rs = rows(), cal = M.calibration(rs);
  $("cal-stats").innerHTML = cal ? [
    ["Settled picks", cal.n],
    ["Won", (cal.winRate * 100).toFixed(0) + "%"],
    ["Model said", (cal.avgPredicted * 100).toFixed(0) + "%"],
    ["Brier", cal.brier.toFixed(3)]
  ].map(function (s) {
    return '<div class="stat"><div class="v">' + s[1] + '</div><div class="k">' + s[0] + "</div></div>";
  }).join("") : '<div class="stat"><div class="v">—</div><div class="k">Nothing settled yet</div></div>';

  $("cal-buckets").innerHTML = (cal && cal.buckets.length)
    ? "<tr><th>When the model said</th><th class='num'>It meant</th><th class='num'>They actually won</th><th class='num'>Games</th></tr>" +
      cal.buckets.map(function (b) {
        var gap = b.actual - b.predicted;
        return "<tr><td>" + (b.lo * 100) + "–" + ((b.lo + 0.1) * 100).toFixed(0) + "%</td>" +
          '<td class="num">' + (b.predicted * 100).toFixed(0) + "%</td>" +
          '<td class="num ' + (gap >= 0 ? "up" : "down") + '">' + (b.actual * 100).toFixed(0) + "%</td>" +
          '<td class="num">' + b.n + "</td></tr>";
      }).join("")
    : "<tr><td>Log some picks and mark them won or lost — the table fills in from there.</td></tr>";

  $("picks-table").innerHTML = rs.length
    ? "<tr><th>Date</th><th>Pick</th><th>Against</th><th class='num'>Said</th><th>Result</th><th>Notes</th><th></th></tr>" +
      rs.map(function (r, i) {
        var res = r.won === "1" ? '<span class="chip ok">Won</span>'
                : r.won === "0" ? '<span class="chip no">Lost</span>'
                : '<button class="act tiny st-w" data-i="' + i + '">Won</button> ' +
                  '<button class="act tiny st-l" data-i="' + i + '">Lost</button>';
        return "<tr><td>" + esc(r.date) + "</td><td>" + esc(r.pick) + "</td><td>" +
          esc(r.opponent) + '</td><td class="num">' +
          Math.round(parseFloat(r.prob) * 100) + "%</td><td>" + res + "</td><td>" +
          esc(r.notes || "") + '</td><td class="num"><button class="act tiny del" data-i="' +
          i + '">×</button></td></tr>';
      }).join("")
    : "<tr><td>No picks logged yet. Use “Log this pick” on any game card.</td></tr>";
}

$("picks-table").addEventListener("click", function (e) {
  var t = e.target, i = parseInt(t.dataset.i, 10);
  if (isNaN(i)) return;
  var rs = rows();
  if (t.classList.contains("st-w")) rs[i].won = "1";
  else if (t.classList.contains("st-l")) rs[i].won = "0";
  else if (t.classList.contains("del")) rs.splice(i, 1);
  else return;
  saveRows(rs); renderPicks();
});

function csvEscape(s) {
  s = String(s === null || s === undefined ? "" : s);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}
$("btn-export").addEventListener("click", function () {
  var text = CSV_FIELDS.join(",") + "\n" + rows().map(function (r) {
    return CSV_FIELDS.map(function (f) { return csvEscape(r[f]); }).join(",");
  }).join("\n") + "\n";
  var a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  a.download = "picks.csv"; a.click(); URL.revokeObjectURL(a.href);
});
function parseCSV(text) {
  var out = [], row = [], cur = "", q = false;
  for (var i = 0; i < text.length; i++) {
    var c = text[i];
    if (q) { if (c === '"') { if (text[i + 1] === '"') { cur += '"'; i++; } else q = false; } else cur += c; }
    else if (c === '"') q = true;
    else if (c === ",") { row.push(cur); cur = ""; }
    else if (c === "\n") { row.push(cur); out.push(row); row = []; cur = ""; }
    else if (c !== "\r") cur += c;
  }
  if (cur !== "" || row.length) { row.push(cur); out.push(row); }
  return out.filter(function (r) { return r.length > 1 || r[0] !== ""; });
}
$("file-import").addEventListener("change", function (e) {
  var f = e.target.files[0]; if (!f) return;
  var fr = new FileReader();
  fr.onload = function () {
    var grid = parseCSV(fr.result);
    if (!grid.length) return;
    var head = grid[0].map(function (x) { return x.trim(); });
    var rs = grid.slice(1).map(function (r) {
      var o = {}; head.forEach(function (h, i) { o[h] = (r[i] || "").trim(); }); return o;
    }).filter(function (o) { return o.pick; });
    saveRows(rs); renderPicks();
  };
  fr.readAsText(f); e.target.value = "";
});

/* ————— go ————— */
renderManual();
renderPicks();
load();
})();
