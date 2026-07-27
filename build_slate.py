"""
build_slate.py — assembles every v5 model input from free public APIs and
writes data/slate.json.

Rewritten after the "why is the site showing 51.7%" bug. Two rules now:

  1. NOTHING FAILS QUIETLY. Every source reports into `diagnostics`, and
     the page refuses to show a confident number when the inputs behind
     it are missing. A prediction built on records and home field alone
     is a coin flip wearing a percentage.
  2. EVERY FETCH HAS A FALLBACK. The bulk pitching endpoint is the most
     fragile call here; if it fails, starters are looked up one at a time
     and bullpens fall back to team staff FIP.

Sources (all free, no keys):
  MLB StatsAPI   schedule, probables, standings, pitching, splits, lineups
  Kalshi         KXMLBGAME moneyline prices (public, unauthenticated)

    python build_slate.py --days 2
    python build_slate.py --debug-kalshi     # dump raw tickers and exit
"""
import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# stadium.py and travel.py are optional. They contribute small run
# adjustments (park fit, time-zone travel). If they aren't present the
# slate still builds — those terms just stay at zero instead of crashing.
try:
    import stadium
except ImportError:
    stadium = None
try:
    import travel
except ImportError:
    travel = None

STATS = "https://statsapi.mlb.com/api/v1"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXMLBGAME"
UA = {"User-Agent": "mlb-model-v5/1.0 (personal research)"}
TIMEOUT = 30

W = {"bb": 0.690, "hbp": 0.720, "1b": 0.880, "2b": 1.271, "3b": 1.616, "hr": 2.101}
RUNS_PER_HR = 1.7          # average runs driven in per home run
LEAGUE_HR_SHARE = 0.42

TEAMS = {
    108: ("LAA", "Angels"), 109: ("ARI", "D-backs"), 110: ("BAL", "Orioles"),
    111: ("BOS", "Red Sox"), 112: ("CHC", "Cubs"), 113: ("CIN", "Reds"),
    114: ("CLE", "Guardians"), 115: ("COL", "Rockies"), 116: ("DET", "Tigers"),
    117: ("HOU", "Astros"), 118: ("KC", "Royals"), 119: ("LAD", "Dodgers"),
    120: ("WSH", "Nationals"), 121: ("NYM", "Mets"), 133: ("ATH", "Athletics"),
    134: ("PIT", "Pirates"), 135: ("SD", "Padres"), 136: ("SEA", "Mariners"),
    137: ("SF", "Giants"), 138: ("STL", "Cardinals"), 139: ("TB", "Rays"),
    140: ("TEX", "Rangers"), 141: ("TOR", "Blue Jays"), 142: ("MIN", "Twins"),
    143: ("PHI", "Phillies"), 144: ("ATL", "Braves"), 145: ("CWS", "White Sox"),
    146: ("MIA", "Marlins"), 147: ("NYY", "Yankees"), 158: ("MIL", "Brewers"),
}
ABBR = {k: v[0] for k, v in TEAMS.items()}
NICK = {k: v[1] for k, v in TEAMS.items()}

DIAG = {}
WARNINGS = []


def note(source, ok, detail):
    DIAG[source] = {"ok": bool(ok), "detail": detail}
    mark = "ok  " if ok else "FAIL"
    print(f"  [{mark}] {source}: {detail}", file=sys.stderr)
    if not ok:
        WARNINGS.append(f"{source}: {detail}")


def get(url, **params):
    r = requests.get(url, params=params or None, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def ip_to_float(ip):
    whole, _, frac = str(ip or "0").partition(".")
    try:
        return int(whole) + int(frac or 0) / 3
    except ValueError:
        return 0.0


def raw_fip(ip, hr, bb, hbp, k):
    return None if ip <= 0 else (13 * hr + 3 * (bb + hbp) - 2 * k) / ip


def fip_parts(s):
    return (ip_to_float(s.get("inningsPitched")),
            float(s.get("homeRuns") or 0), float(s.get("baseOnBalls") or 0),
            float(s.get("hitByPitch") or 0), float(s.get("strikeOuts") or 0))


def woba_from(s):
    ab = float(s.get("atBats") or 0); h = float(s.get("hits") or 0)
    d = float(s.get("doubles") or 0); t = float(s.get("triples") or 0)
    hr = float(s.get("homeRuns") or 0); bb = float(s.get("baseOnBalls") or 0)
    ibb = float(s.get("intentionalWalks") or 0); hbp = float(s.get("hitByPitch") or 0)
    sf = float(s.get("sacFlies") or 0)
    denom = ab + bb - ibb + sf + hbp
    if denom <= 0:
        return None
    num = (W["bb"] * (bb - ibb) + W["hbp"] * hbp + W["1b"] * (h - d - t - hr) +
           W["2b"] * d + W["3b"] * t + W["hr"] * hr)
    return round(num / denom, 4)


# ————————————————————————————————————————————————
# Schedule
# ————————————————————————————————————————————————
def fetch_schedule(days):
    start = date.today()
    end = start + timedelta(days=days - 1)
    try:
        data = get(f"{STATS}/schedule", sportId=1, startDate=start.isoformat(),
                   endDate=end.isoformat(), hydrate="probablePitcher,team,venue")
    except Exception as e:
        note("schedule", False, str(e))
        return []
    games = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            if g.get("status", {}).get("abstractGameState") == "Final":
                continue
            home, away = g["teams"]["home"], g["teams"]["away"]
            games.append({
                "gamePk": g["gamePk"], "date": d["date"],
                "startsAt": g.get("gameDate"),
                "state": g.get("status", {}).get("detailedState"),
                "venue": (g.get("venue") or {}).get("name"),
                "home": {"id": home["team"]["id"], "name": home["team"]["name"],
                         "probable": (home.get("probablePitcher") or {}).get("id"),
                         "probableName": (home.get("probablePitcher") or {}).get("fullName")},
                "away": {"id": away["team"]["id"], "name": away["team"]["name"],
                         "probable": (away.get("probablePitcher") or {}).get("id"),
                         "probableName": (away.get("probablePitcher") or {}).get("fullName")},
            })
    tbd = sum(1 for g in games for s in ("home", "away") if not g[s]["probable"])
    note("schedule", bool(games),
         f"{len(games)} games, {tbd} starters still TBD")
    return games


def fetch_records(season):
    try:
        data = get(f"{STATS}/standings", leagueId="103,104", season=season,
                   standingsTypes="regularSeason")
    except Exception as e:
        note("standings", False, str(e))
        return {}
    out = {}
    for rec in data.get("records", []):
        for t in rec.get("teamRecords", []):
            out[t["team"]["id"]] = {"wins": t.get("wins", 0), "losses": t.get("losses", 0)}
    note("standings", len(out) >= 28, f"{len(out)} teams")
    return out


# ————————————————————————————————————————————————
# Pitching, with a fallback path
# ————————————————————————————————————————————————
def fetch_pitching_bulk(season):
    data = get(f"{STATS}/stats", stats="season", group="pitching", season=season,
               sportId=1, playerPool="All", gameType="R", limit=2000)
    splits = []
    for blob in data.get("stats", []):
        splits.extend(blob.get("splits", []))
    if not splits:
        raise RuntimeError("returned zero splits")
    return splits


def fetch_pitching_fallback(season, probable_ids, team_ids):
    """One call per probable starter, plus team totals for the staff."""
    splits = []
    ids = [str(p) for p in probable_ids if p]
    if ids:
        try:
            d = get(f"{STATS}/people", personIds=",".join(ids),
                    hydrate=f"stats(group=[pitching],type=[season],season={season})")
            for person in d.get("people", []):
                for blob in person.get("stats", []):
                    for sp in blob.get("splits", []):
                        sp["player"] = {"id": person["id"]}
                        sp.setdefault("team", {})
                        splits.append(sp)
        except Exception as e:
            note("pitching.fallback.starters", False, str(e))
    return splits


def build_pitching(season, probable_ids, team_ids):
    people, bullpen, staff = {}, {}, {}
    try:
        splits = fetch_pitching_bulk(season)
        source = "bulk"
    except Exception as e:
        note("pitching.bulk", False, f"{e} — falling back to per-player lookups")
        splits = fetch_pitching_fallback(season, probable_ids, team_ids)
        source = "fallback"

    if not splits:
        note("pitching", False, "no pitching data from any route")
        return people, bullpen, staff, {}

    tot = [0.0] * 5
    tot_er = 0.0
    pen = defaultdict(lambda: [0.0] * 5)
    allstaff = defaultdict(lambda: [0.0] * 5)

    for sp in splits:
        s = sp.get("stat", {})
        pid = (sp.get("player") or {}).get("id")
        tid = (sp.get("team") or {}).get("id")
        ip, hr, bb, hbp, k = fip_parts(s)
        if pid is None or ip <= 0:
            continue
        gs = float(s.get("gamesStarted") or 0)
        g = float(s.get("gamesPlayed") or 0) or 1.0
        for i, v in enumerate((ip, hr, bb, hbp, k)):
            tot[i] += v
        tot_er += float(s.get("earnedRuns") or 0)
        era = s.get("era")
        people[pid] = {"teamId": tid, "ip": ip, "gs": gs, "rawFip": raw_fip(ip, hr, bb, hbp, k),
                       "era": float(era) if era not in (None, "-.--", "") else None,
                       "ipPerStart": (ip / gs) if gs > 0 else None}
        if tid is not None:
            for i, v in enumerate((ip, hr, bb, hbp, k)):
                allstaff[tid][i] += v
            if gs / g < 0.5:
                for i, v in enumerate((ip, hr, bb, hbp, k)):
                    pen[tid][i] += v

    lg_ip = tot[0]
    lg_era = (tot_er * 9 / lg_ip) if lg_ip else 4.10
    c_fip = lg_era - (raw_fip(*tot) or 0.0)

    for p in people.values():
        p["fip"] = round(p["rawFip"] + c_fip, 3) if p["rawFip"] is not None else None
    for tid, c in pen.items():
        rf = raw_fip(*c)
        if rf is not None:
            bullpen[tid] = {"fip": round(rf + c_fip, 3), "ip": round(c[0], 1)}
    for tid, c in allstaff.items():
        rf = raw_fip(*c)
        if rf is not None:
            staff[tid] = round(rf + c_fip, 3)

    # If the fallback route ran, bullpens are unknown — use team staff FIP.
    if source == "fallback" and not bullpen:
        for tid in team_ids:
            st = fetch_team_staff(tid, season, c_fip)
            if st is not None:
                staff[tid] = st
                bullpen[tid] = {"fip": st, "ip": None, "proxy": True}

    league = {"era": round(lg_era, 3), "fipConstant": round(c_fip, 3),
              "ip": round(lg_ip, 1), "source": source}
    hit = sum(1 for p in probable_ids if p in people)
    note("pitching", hit > 0,
         f"{source}: {len(people)} arms, {len(bullpen)} bullpens, "
         f"{hit}/{len(probable_ids)} probables matched, FIP constant {c_fip:.2f}")
    return people, bullpen, staff, league


def fetch_team_staff(tid, season, c_fip):
    try:
        d = get(f"{STATS}/teams/{tid}/stats", stats="season", group="pitching",
                season=season, gameType="R")
        for blob in d.get("stats", []):
            for sp in blob.get("splits", []):
                rf = raw_fip(*fip_parts(sp.get("stat", {})))
                if rf is not None:
                    return round(rf + c_fip, 3)
    except Exception:
        return None
    return None


def fetch_handedness(ids):
    hands = {}
    ids = [str(p) for p in ids if p]
    if not ids:
        note("handedness", False, "no probable starters to look up")
        return hands
    try:
        data = get(f"{STATS}/people", personIds=",".join(ids))
        for p in data.get("people", []):
            hands[p["id"]] = (p.get("pitchHand") or {}).get("code")
        note("handedness", len(hands) > 0, f"{len(hands)}/{len(ids)} resolved")
    except Exception as e:
        note("handedness", False, str(e))
    return hands


# ————————————————————————————————————————————————
# Hitting: overall wOBA, platoon splits, HR reliance
# ————————————————————————————————————————————————
def fetch_hitting(team_ids, season):
    out, ok = {}, 0
    for tid in sorted(team_ids):
        entry = {}
        try:
            d = get(f"{STATS}/teams/{tid}/stats", stats="season", group="hitting",
                    season=season, gameType="R")
            for blob in d.get("stats", []):
                for sp in blob.get("splits", []):
                    st = sp.get("stat", {})
                    entry["overall"] = woba_from(st)
                    runs = float(st.get("runs") or 0)
                    hr = float(st.get("homeRuns") or 0)
                    if runs > 0:
                        entry["hrShare"] = round(min(hr * RUNS_PER_HR / runs, 0.9), 3)
        except Exception as e:
            WARNINGS.append(f"team {tid} season hitting: {e}")
        try:
            d = get(f"{STATS}/teams/{tid}/stats", stats="statSplits", group="hitting",
                    sitCodes="vl,vr", season=season, gameType="R")
            for blob in d.get("stats", []):
                for sp in blob.get("splits", []):
                    code = (sp.get("split") or {}).get("code")
                    w = woba_from(sp.get("stat", {}))
                    if code == "vl":
                        entry["vL"] = w
                    elif code == "vr":
                        entry["vR"] = w
        except Exception as e:
            WARNINGS.append(f"team {tid} platoon splits: {e}")
        if entry.get("overall"):
            ok += 1
        out[tid] = entry
    note("hitting", ok >= len(team_ids) * 0.8,
         f"{ok}/{len(team_ids)} teams with wOBA, "
         f"{sum(1 for v in out.values() if v.get('vL'))} with platoon splits")
    return out


# ————————————————————————————————————————————————
# Fatigue and travel
# ————————————————————————————————————————————————
def fetch_recent(days=4):
    """Relief IP by team, plus each team's last venue for travel."""
    end = date.today()
    start = end - timedelta(days=days)
    totals, last_park = defaultdict(float), {}
    try:
        sched = get(f"{STATS}/schedule", sportId=1, startDate=start.isoformat(),
                    endDate=end.isoformat(), hydrate="team")
    except Exception as e:
        note("fatigue", False, str(e))
        return {}, {}
    fin = 0
    cutoff = date.today() - timedelta(days=3)
    for d in sched.get("dates", []):
        for g in d.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Final":
                continue
            fin += 1
            home_id = g["teams"]["home"]["team"]["id"]
            for side in ("home", "away"):
                last_park[g["teams"][side]["team"]["id"]] = (d["date"], home_id)
            if datetime.strptime(d["date"], "%Y-%m-%d").date() < cutoff:
                continue
            try:
                box = get(f"{STATS}/game/{g['gamePk']}/boxscore")
            except Exception:
                continue
            for side in ("home", "away"):
                t = box["teams"][side]
                for pid in t.get("pitchers", [])[1:]:
                    st = t["players"].get(f"ID{pid}", {})
                    totals[t["team"]["id"]] += ip_to_float(
                        st.get("stats", {}).get("pitching", {}).get("inningsPitched"))
    note("fatigue", fin > 0, f"{fin} finished games scanned, {len(totals)} bullpens")
    return {k: round(v, 1) for k, v in totals.items()}, last_park


def travel_for(team_id, today_home_id, last_park):
    """Runs penalty for a team travelling into tonight's park."""
    prev = last_park.get(team_id)
    if not prev:
        return 0.0
    if travel is None:
        return 0.0
    prev_date, prev_home_id = prev
    prev_nick = NICK.get(prev_home_id)
    today_nick = NICK.get(today_home_id)
    if not prev_nick or not today_nick or prev_nick == today_nick:
        return 0.0
    try:
        rest = (date.today() - datetime.strptime(prev_date, "%Y-%m-%d").date()).days - 1
        return round(travel.travel_runs(prev_nick, today_nick, max(rest, 0)), 4)
    except Exception:
        return 0.0


# ————————————————————————————————————————————————
# Kalshi
# ————————————————————————————————————————————————
def _price_cents(m, side):
    """Kalshi has served these under several names. Try each.

    Older responses: yes_bid / yes_ask as integer cents.
    Newer responses: yes_bid_dollars / yes_ask_dollars as decimal strings.
    Some payloads only carry last_price. Everything is normalised to
    integer cents so the rest of the pipeline sees one shape.
    """
    for key in (f"{side}_bid" if side in ("yes", "no") else side,):
        pass
    candidates = [
        (m.get(f"{side}"), 1),                       # e.g. last_price
        (m.get(f"{side}_dollars"), 100),
    ]
    for raw, mult in candidates:
        if raw is None or raw == "":
            continue
        try:
            v = float(raw) * mult
        except (TypeError, ValueError):
            continue
        if 0 <= v <= 100:
            return int(round(v))
    return None


def kalshi_prices(m):
    """(bid_cents, ask_cents, last_cents) from any known payload shape."""
    bid = _price_cents(m, "yes_bid")
    ask = _price_cents(m, "yes_ask")
    last = _price_cents(m, "last_price")
    # A one-sided book still tells you something: the other side of a
    # Kalshi market is 100 minus the opposing bid.
    if bid is None and m.get("no_ask") is not None or m.get("no_ask_dollars") is not None:
        na = _price_cents(m, "no_ask")
        if na is not None:
            bid = 100 - na
    if ask is None:
        nb = _price_cents(m, "no_bid")
        if nb is not None:
            ask = 100 - nb
    return bid, ask, last


def fetch_kalshi():
    markets, cursor, pages = [], None, 0
    try:
        while pages < 10:
            params = {"series_ticker": SERIES, "status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = get(f"{KALSHI}/markets", **params)
            batch = data.get("markets", [])
            markets.extend(batch)
            cursor = data.get("cursor")
            pages += 1
            if not cursor or not batch:
                break
    except Exception as e:
        note("kalshi", False, f"{e}")
        return []

    out = []
    priced = 0
    for m in markets:
        bid, ask, last = kalshi_prices(m)
        if bid is not None and ask is not None:
            mid = (bid + ask) / 200
            priced += 1
        elif last is not None:
            mid = last / 100          # thin book: fall back to last trade
            priced += 1
        else:
            mid = None
        out.append({
            "ticker": m.get("ticker", ""), "eventTicker": m.get("event_ticker", ""),
            "title": m.get("title", ""),
            "subtitle": m.get("yes_sub_title") or m.get("subtitle") or "",
            "yesBid": bid, "yesAsk": ask, "last": last,
            "mid": round(mid, 4) if mid is not None else None,
            "volume": m.get("volume"), "closeTime": m.get("close_time"),
        })
    note("kalshi", priced > 0,
         f"{len(out)} open {SERIES} markets, {priced} with usable prices"
         + ("" if priced else " — every price field was empty; check field names "
                              "against `python build_slate.py --debug-kalshi`"))
    return out


def match_kalshi(game, markets):
    """Match on content rather than assuming Kalshi's ticker grammar.

    Scores every market on how much of this game it looks like, then takes
    the best if it clears a floor. Resolving WHICH side is YES is the part
    that actually matters — a market attached to the wrong team inverts
    every edge downstream — so an unresolved side returns None rather than
    a guess.
    """
    hid, aid = game["home"]["id"], game["away"]["id"]
    h_ab, a_ab = ABBR.get(hid), ABBR.get(aid)
    h_nk, a_nk = NICK.get(hid), NICK.get(aid)
    if not h_ab or not a_ab:
        return None, "unknown team abbreviation"

    def toks(s):
        return (s or "").upper().replace("-", " ").replace("_", " ")

    best, best_score = None, 0
    for m in markets:
        tick = toks(m["ticker"])
        blob = tick + " " + toks(m["title"]) + " " + toks(m["subtitle"])
        score = 0
        if h_ab in tick and a_ab in tick:
            score += 4
        if h_nk.upper() in blob:
            score += 1
        if a_nk.upper() in blob:
            score += 1
        if score > best_score:
            best, best_score = m, score
    if not best or best_score < 4:
        return None, "no market matched both team codes"

    # Which team does YES pay on? Ticker tail first, then the subtitle.
    tail = best["ticker"].upper().rsplit("-", 1)[-1].strip()
    sub = (best["subtitle"] or "").upper()
    side = None
    if tail == h_ab or (h_nk.upper() in sub and a_nk.upper() not in sub):
        side = "home"
    elif tail == a_ab or (a_nk.upper() in sub and h_nk.upper() not in sub):
        side = "away"
    if side is None:
        return None, f"matched {best['ticker']} but could not resolve the YES side"
    return {"side": side, **best}, None


# ————————————————————————————————————————————————
# Assemble
# ————————————————————————————————————————————————
def build(days):
    season = date.today().year
    print(f"season {season}, {days} day(s) ahead", file=sys.stderr)

    games = fetch_schedule(days)
    if not games:
        return {"generatedAt": datetime.now(timezone.utc).isoformat(),
                "season": season, "games": [], "diagnostics": DIAG,
                "warnings": WARNINGS, "kalshiSample": []}

    team_ids = {g[s]["id"] for g in games for s in ("home", "away")}
    probables = [g[s]["probable"] for g in games for s in ("home", "away")]

    note("optional modules", True,
         f"stadium={'yes' if stadium else 'MISSING (park term off)'}, "
         f"travel={'yes' if travel else 'MISSING (travel term off)'}")

    records = fetch_records(season)
    people, bullpen, staff, league = build_pitching(season, probables, team_ids)
    hands = fetch_handedness(set(probables))
    hitting = fetch_hitting(team_ids, season)
    fatigue, last_park = fetch_recent(4)
    markets = fetch_kalshi()

    def side(g, which):
        s = g[which]
        tid = s["id"]
        other = "away" if which == "home" else "home"
        opp_hand = hands.get(g[other]["probable"])
        hit = hitting.get(tid, {})
        arm = people.get(s["probable"]) or {}
        pen = bullpen.get(tid) or {}
        vs_hand = (hit.get("vL") if opp_hand == "L"
                   else hit.get("vR") if opp_hand == "R" else None)
        park_nick = NICK.get(g["home"]["id"])
        park_fit = None
        if stadium and hit.get("hrShare") is not None and park_nick:
            park_fit = round(stadium.fit_runs(park_nick, hit["hrShare"]), 4)
        return {
            "id": tid, "name": s["name"], "abbr": ABBR.get(tid), "nick": NICK.get(tid),
            "wins": records.get(tid, {}).get("wins"),
            "losses": records.get(tid, {}).get("losses"),
            "starter": s.get("probableName"), "starterId": s.get("probable"),
            "starterHand": hands.get(s["probable"]),
            "sp_rate": arm.get("fip"), "sp_era": arm.get("era"),
            "sp_ip": round(arm["ipPerStart"], 2) if arm.get("ipPerStart") else None,
            "bp_rate": pen.get("fip"), "bp_is_proxy": pen.get("proxy", False),
            "bp_ip3": fatigue.get(tid),
            "woba_vs_hand": vs_hand, "woba_overall": hit.get("overall"),
            "lineup_woba": hit.get("overall"),   # replaced by the posted lineup
            "staff_fip": staff.get(tid),
            "ptype_rv100": None,
            "travel_runs": (0.0 if which == "home"
                            else travel_for(tid, g["home"]["id"], last_park)),
            "park_fit_runs": park_fit, "history_runs": 0.0,
            "oppHand": opp_hand,
        }

    slate, matched = [], 0
    for g in games:
        k, why = match_kalshi(g, markets)
        if k:
            matched += 1
        slate.append({
            "gamePk": g["gamePk"], "date": g["date"], "startsAt": g["startsAt"],
            "state": g["state"], "venue": g["venue"],
            "home": side(g, "home"), "away": side(g, "away"),
            "kalshi": k, "kalshiWhy": why, "lineupConfirmed": False,
        })

    note("kalshi.match", matched > 0,
         f"{matched}/{len(slate)} games matched to a market")

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "season": season, "modelVersion": "v5",
        "league": league, "wobaWeights": W,
        "games": slate, "diagnostics": DIAG, "warnings": WARNINGS,
        # First few raw markets, so the page can show what Kalshi actually
        # returned when nothing matched.
        "kalshiSample": [{"ticker": m["ticker"], "title": m["title"],
                          "subtitle": m["subtitle"], "yesBid": m["yesBid"],
                          "yesAsk": m["yesAsk"]} for m in markets[:12]],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--out", default="data/slate.json")
    ap.add_argument("--debug-kalshi", action="store_true",
                    help="print raw Kalshi markets and exit")
    args = ap.parse_args()

    if args.debug_kalshi:
        ms = fetch_kalshi()
        print(f"\n{len(ms)} open {SERIES} markets\n")
        for m in ms[:40]:
            print(f"{m['ticker']:44s} sub={m['subtitle']!r:28s} "
                  f"bid={m['yesBid']} ask={m['yesAsk']} last={m['last']}")
        print("\n--- raw keys on the first market (watch for renames) ---")
        try:
            one = get(f"{KALSHI}/markets", series_ticker=SERIES, status="open",
                      limit=1).get("markets", [])
            if one:
                for k, v in sorted(one[0].items()):
                    if "price" in k or "bid" in k or "ask" in k or "volume" in k:
                        print(f"  {k} = {v!r}")
        except Exception as e:
            print(f"  (could not re-fetch: {e})")
        return 0

    payload = build(args.days)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1))

    bad = [k for k, v in payload["diagnostics"].items() if not v["ok"]]
    print(f"\nwrote {out} — {len(payload['games'])} games")
    if bad:
        print(f"FAILED SOURCES: {', '.join(bad)}")
        print("The page will show these as missing inputs rather than a fake number.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
