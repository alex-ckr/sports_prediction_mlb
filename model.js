/* model.js — v5 engine, ported from baseline.py.
   Runs in a browser or in Node. No dependencies.

   v5 differs from v4 in five ways that all move the number:
     1. Team strength blends a roster rating (tonight's lineup wOBA and
        season staff FIP through a pythagorean) with the W-L record,
        65/35, instead of using the record alone.
     2. Home-field odds dropped 1.19 -> 1.12; the backtest said 1.19
        overpriced it.
     3. Pitching inputs are shrunk by SP_TRUST / BP_TRUST toward league
        average, because crude starter proxies added no standalone signal.
     4. Travel, park fit and matchup history enter the run delta.
     5. A Platt layer (CAL_A, CAL_B) rescales the final logit.
*/

(function (root) {
  "use strict";

  var K = {
    REGRESSION_GAMES: 70,
    REC_WEIGHT: 0.35,          // how much W-L still counts when a roster rating exists
    LG_WOBA: 0.315,
    LG_RPG: 4.5,               // league runs per team-game
    TEAM_PA: 38,               // team plate appearances per game
    PYTH_EXP: 1.83,
    LEAGUE_RATE: 4.10,
    RUNS_PER_WIN: 10.0,
    HFA_ODDS: 1.12,            // backtest-tuned; v4 used 1.19
    LOGODDS_PER_WIN: 4.0,
    DEFAULT_SP_IP: 5.5,
    FATIGUE_FREE_IP3: 9.0,
    FATIGUE_RUNS_PER_IP: 0.12,
    WOBA_TO_RUNS: 1.15,
    SP_TRUST: 0.65,            // fraction of (rate - league) believed for starters
    BP_TRUST: 0.75,            // bullpens: bigger samples, trust a bit more
    CAL_A: -0.0012,            // Platt intercept, fitted on 5,103 walk-forward games
    CAL_B: 0.8777,             // slope <1: the model was overconfident by ~12%
    PA_VS_STARTER: 26,
    PITCHES_VS_STARTER: 90
  };
  var DEFAULT_K = Object.assign({}, K);

  var SLOT_PA = [4.7, 4.6, 4.5, 4.4, 4.3, 4.2, 4.1, 4.0, 3.9];

  function num(v, fallback) {
    var x = typeof v === "number" ? v : parseFloat(v);
    return isFinite(x) ? x : (fallback === undefined ? 0 : fallback);
  }
  function nullable(v) {
    if (v === null || v === undefined || v === "") return null;
    var x = typeof v === "number" ? v : parseFloat(v);
    return isFinite(x) ? x : null;
  }

  function teamDay(o) {
    o = o || {};
    return {
      name: o.name || "",
      wins: num(o.wins, 0),
      losses: num(o.losses, 0),
      starter: o.starter || "",
      sp_rate: nullable(o.sp_rate) === null ? K.LEAGUE_RATE : num(o.sp_rate),
      bp_rate: nullable(o.bp_rate) === null ? K.LEAGUE_RATE : num(o.bp_rate),
      sp_ip: nullable(o.sp_ip) === null ? K.DEFAULT_SP_IP : num(o.sp_ip),
      bp_ip3: nullable(o.bp_ip3),
      woba_vs_hand: nullable(o.woba_vs_hand),
      woba_overall: nullable(o.woba_overall),
      ptype_rv100: nullable(o.ptype_rv100) === null ? 0 : num(o.ptype_rv100),
      travel_runs: nullable(o.travel_runs) === null ? 0 : num(o.travel_runs),
      park_fit_runs: nullable(o.park_fit_runs) === null ? 0 : num(o.park_fit_runs),
      history_runs: nullable(o.history_runs) === null ? 0 : num(o.history_runs),
      lineup_woba: nullable(o.lineup_woba),
      staff_fip: nullable(o.staff_fip)
    };
  }

  function regressedWpct(w, l) {
    return (w + 0.5 * K.REGRESSION_GAMES) / (w + l + K.REGRESSION_GAMES);
  }

  /** Player-level team strength: runs scored implied by the lineup's wOBA,
      runs allowed from season staff quality, through a pythagorean.
      null when the roster inputs aren't there. */
  function rosterWpct(t) {
    if (t.lineup_woba === null) return null;
    var rs = K.LG_RPG + (t.lineup_woba - K.LG_WOBA) / 1.15 * K.TEAM_PA;
    var ra = K.LG_RPG + ((t.staff_fip === null ? K.LEAGUE_RATE : t.staff_fip) - K.LEAGUE_RATE);
    var e = K.PYTH_EXP;
    return Math.pow(rs, e) / (Math.pow(rs, e) + Math.pow(ra, e));
  }

  /** Mostly players, lightly the record. The record carries what rosters
      miss — defense, baserunning, churn — but also luck, hence 35%. */
  function strength(t) {
    var rec = regressedWpct(t.wins, t.losses);
    var ros = rosterWpct(t);
    if (ros === null) return rec;
    return K.REC_WEIGHT * rec + (1 - K.REC_WEIGHT) * ros;
  }

  function log5(a, b) {
    return (a * (1 - b)) / (a * (1 - b) + b * (1 - a));
  }

  function effectiveBpRate(t) {
    var rate = t.bp_rate;
    if (t.bp_ip3 !== null && t.bp_ip3 > K.FATIGUE_FREE_IP3) {
      rate += (t.bp_ip3 - K.FATIGUE_FREE_IP3) * K.FATIGUE_RUNS_PER_IP;
    }
    return rate;
  }

  function pitchingRunsVsAvg(t) {
    var spIp = Math.min(Math.max(t.sp_ip, 0), 9);
    return ((t.sp_rate - K.LEAGUE_RATE) * K.SP_TRUST * spIp +
            (effectiveBpRate(t) - K.LEAGUE_RATE) * K.BP_TRUST * (9 - spIp)) / 9;
  }

  function offenseRunsVsAvg(t) {
    var runs = 0;
    if (t.woba_vs_hand !== null && t.woba_overall !== null) {
      runs += (t.woba_vs_hand - t.woba_overall) / K.WOBA_TO_RUNS * K.PA_VS_STARTER;
    }
    runs += t.ptype_rv100 * K.PITCHES_VS_STARTER / 100;
    return runs;
  }

  function predict(home, away) {
    var sh = strength(home), sa = strength(away);
    var p0 = log5(sh, sa);
    var baseLogit = Math.log(p0 / (1 - p0));
    var hfaLogit = Math.log(K.HFA_ODDS);

    var hPitch = pitchingRunsVsAvg(home), aPitch = pitchingRunsVsAvg(away);
    var hOff = offenseRunsVsAvg(home), aOff = offenseRunsVsAvg(away);
    var pitchDelta = aPitch - hPitch;
    var offDelta = hOff - aOff;
    var travelDelta = away.travel_runs - home.travel_runs;
    var parkDelta = home.park_fit_runs - away.park_fit_runs;
    var histDelta = home.history_runs - away.history_runs;
    var delta = pitchDelta + offDelta + travelDelta + parkDelta + histDelta;
    var deltaLogit = (delta / K.RUNS_PER_WIN) * K.LOGODDS_PER_WIN;

    var rawLogit = baseLogit + hfaLogit + deltaLogit;
    var logit = K.CAL_A + K.CAL_B * rawLogit;
    var p = 1 / (1 + Math.exp(-logit));

    return {
      p: p,
      homeStrength: sh, awayStrength: sa,
      homeRecWpct: regressedWpct(home.wins, home.losses),
      awayRecWpct: regressedWpct(away.wins, away.losses),
      homeRoster: rosterWpct(home), awayRoster: rosterWpct(away),
      p0: p0, baseLogit: baseLogit, hfaLogit: hfaLogit,
      homePitch: hPitch, awayPitch: aPitch,
      homeOff: hOff, awayOff: aOff,
      homeBpEff: effectiveBpRate(home), awayBpEff: effectiveBpRate(away),
      pitchDelta: pitchDelta, offDelta: offDelta,
      travelDelta: travelDelta, parkDelta: parkDelta, histDelta: histDelta,
      runDelta: delta, deltaLogit: deltaLogit,
      rawLogit: rawLogit, logit: logit
    };
  }

  function report(home, away) {
    var p = predict(home, away).p;
    var fav = p >= 0.5 ? home : away;
    var prob = p >= 0.5 ? p : 1 - p;
    return away.name + " @ " + home.name + " -> " + fav.name + " " +
           (prob * 100).toFixed(1) + "% (home " + (p * 100).toFixed(1) + "%)";
  }

  function lineupWoba(vals) {
    var w = SLOT_PA.slice(0, vals.length), top = 0, bot = 0;
    for (var i = 0; i < w.length; i++) { top += vals[i] * w[i]; bot += w[i]; }
    return bot ? top / bot : 0;
  }

  function matchupRv100(mix, rv) {
    var t = 0;
    for (var i = 0; i < mix.length; i++) {
      t += num(mix[i].usage, 0) * num(rv[mix[i].type], 0);
    }
    return t;
  }

  // ——— Slate plumbing ———
  function fromSlate(game) {
    function one(s) {
      return teamDay({
        name: s.name, wins: s.wins, losses: s.losses,
        starter: s.starter || "TBD",
        sp_rate: s.sp_rate, bp_rate: s.bp_rate, sp_ip: s.sp_ip, bp_ip3: s.bp_ip3,
        woba_vs_hand: s.woba_vs_hand, woba_overall: s.woba_overall,
        ptype_rv100: s.ptype_rv100, travel_runs: s.travel_runs,
        park_fit_runs: s.park_fit_runs, history_runs: s.history_runs,
        lineup_woba: s.lineup_woba, staff_fip: s.staff_fip
      });
    }
    return { home: one(game.home), away: one(game.away) };
  }

  /** Which inputs the feed actually supplied. A prediction built on
      records and home field alone is a coin flip wearing a percentage. */
  var INPUT_GROUPS = [
    { key: "record", fields: ["wins", "losses"], label: "Team records",
      why: "Season W-L, regressed toward .500", critical: false },
    { key: "starter", fields: ["sp_rate"], label: "Starting pitchers",
      why: "Tonight's starter — the single biggest input", critical: true },
    { key: "bullpen", fields: ["bp_rate"], label: "Bullpens",
      why: "Relief quality over the innings the starter doesn't cover", critical: true },
    { key: "fatigue", fields: ["bp_ip3"], label: "Bullpen workload",
      why: "Relief innings over the last three days", critical: false },
    { key: "platoon", fields: ["woba_vs_hand", "woba_overall"], label: "Lineup matchup",
      why: "How these hitters do against this arm's handedness", critical: false },
    { key: "roster", fields: ["lineup_woba", "staff_fip"], label: "Roster strength",
      why: "v5 core: player quality rather than record alone", critical: false }
  ];

  function coverage(game) {
    var got = [], missing = [];
    INPUT_GROUPS.forEach(function (g) {
      var ok = g.fields.every(function (f) {
        return game.home[f] !== null && game.home[f] !== undefined &&
               game.away[f] !== null && game.away[f] !== undefined;
      });
      (ok ? got : missing).push(g);
    });
    return {
      have: got.length, total: INPUT_GROUPS.length,
      got: got, missing: missing,
      usable: !missing.some(function (g) { return g.critical; })
    };
  }

  // ——— Market ———
  function kalshiFee(price) {
    return Math.ceil(0.07 * price * (1 - price) * 100) / 100;
  }

  function contractEv(p, ask) {
    if (!(ask > 0 && ask < 1)) return null;
    var fee = kalshiFee(ask);
    var breakEven = ask + fee;
    return { ask: ask, fee: fee, cost: breakEven, breakEven: breakEven,
             ev: p - breakEven, roi: (p - breakEven) / breakEven,
             netEdge: p - breakEven };
  }

  function compareMarket(p, k) {
    if (!k) return null;
    if (k.side !== "home" && k.side !== "away") return { unknownSide: true, market: k };
    var yesIsHome = k.side === "home";
    var pYes = yesIsHome ? p : 1 - p;
    var mid = (k.mid === null || k.mid === undefined) ? null : k.mid;
    var ask = (k.yesAsk === null || k.yesAsk === undefined) ? null : k.yesAsk / 100;
    var bid = (k.yesBid === null || k.yesBid === undefined) ? null : k.yesBid / 100;

    var yes = ask !== null ? contractEv(pYes, ask) : null;
    var no = bid !== null ? contractEv(1 - pYes, 1 - bid) : null;
    var best = (yes && no) ? (yes.netEdge >= no.netEdge ? "yes" : "no")
             : yes ? "yes" : no ? "no" : null;
    var bestLeg = best === "yes" ? yes : best === "no" ? no : null;

    return {
      market: k, yesIsHome: yesIsHome, modelYes: pYes, marketYes: mid,
      edge: mid !== null ? pYes - mid : null,
      hurdle: (bestLeg && mid !== null)
        ? bestLeg.breakEven - (best === "yes" ? mid : 1 - mid) : null,
      yes: yes, no: no, best: best, bestLeg: bestLeg,
      netEdge: bestLeg ? bestLeg.netEdge : null
    };
  }

  function americanOdds(p) {
    if (!(p > 0 && p < 1)) return "—";
    var o = p >= 0.5 ? -100 * p / (1 - p) : 100 * (1 - p) / p;
    return (o > 0 ? "+" : "") + Math.round(o);
  }

  function calibration(rows) {
    var done = rows.filter(function (r) { return r.won === "1" || r.won === "0"; });
    if (!done.length) return null;
    var probs = done.map(function (r) { return parseFloat(r.prob); });
    var outs = done.map(function (r) { return parseInt(r.won, 10); });
    var n = done.length, brier = 0, wins = 0, sump = 0;
    for (var i = 0; i < n; i++) {
      brier += Math.pow(probs[i] - outs[i], 2); wins += outs[i]; sump += probs[i];
    }
    var buckets = [];
    [0.5, 0.6, 0.7, 0.8, 0.9].forEach(function (lo) {
      var b = [];
      for (var i = 0; i < n; i++) {
        if (probs[i] >= lo && probs[i] < lo + 0.1) b.push([probs[i], outs[i]]);
      }
      if (b.length) buckets.push({
        lo: lo,
        predicted: b.reduce(function (s, x) { return s + x[0]; }, 0) / b.length,
        actual: b.reduce(function (s, x) { return s + x[1]; }, 0) / b.length,
        n: b.length
      });
    });
    return { n: n, winRate: wins / n, avgPredicted: sump / n,
             brier: brier / n, buckets: buckets };
  }

  root.Model = {
    K: K, DEFAULT_K: DEFAULT_K, SLOT_PA: SLOT_PA, INPUT_GROUPS: INPUT_GROUPS,
    teamDay: teamDay, regressedWpct: regressedWpct, rosterWpct: rosterWpct,
    strength: strength, log5: log5, effectiveBpRate: effectiveBpRate,
    pitchingRunsVsAvg: pitchingRunsVsAvg, offenseRunsVsAvg: offenseRunsVsAvg,
    predict: predict, report: report,
    lineupWoba: lineupWoba, matchupRv100: matchupRv100,
    fromSlate: fromSlate, coverage: coverage,
    kalshiFee: kalshiFee, contractEv: contractEv, compareMarket: compareMarket,
    americanOdds: americanOdds, calibration: calibration
  };
})(typeof window !== "undefined" ? window : globalThis);
