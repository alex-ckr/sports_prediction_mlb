"""
watch_lineups.py — fires when both teams post a lineup.

Polls today's games. The moment a game has nine confirmed hitters a side,
it rebuilds that game's offense inputs from the players who are actually
playing, re-prices the Kalshi market, runs the model, and emails the result.

Why this matters more than the alert itself: build_slate.py fills
woba_vs_hand from the team's SEASON split, which quietly assumes a
full-strength lineup. This script replaces it with the nine hitters
posted today, weighted by lineup slot. The (vs-hand − overall) delta then
absorbs rest days and injuries, not just handedness.

State lives in data/alerts_sent.json so a game is only mailed once.

Env (all via GitHub Secrets, never in the repo):
    SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASS   sender credentials
    ALERT_FROM                                 from address
    ALERT_RECIPIENTS                           comma-separated
"""
import argparse
import json
import os
import smtplib
import ssl
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import requests

from build_slate import (ABBR, KALSHI, SERIES, STATS, TIMEOUT, UA, W, get,
                         ip_to_float, kalshi_prices, raw_fip, woba_from)

# Lineup slot weights — how often each spot bats. Same table as the model.
SLOT_PA = [4.7, 4.6, 4.5, 4.4, 4.3, 4.2, 4.1, 4.0, 3.9]

# Platoon splits are noisy. Regress a hitter's vs-hand wOBA toward his own
# overall wOBA; 300 PA of ballast is heavy on purpose.
PLATOON_BALLAST = 300

STATE = Path("data/alerts_sent.json")
SLATE = Path("data/slate.json")

LEAGUE_WOBA = 0.315


# ————————————————————————————————————————————————
# Lineup detection
# ————————————————————————————————————————————————
def batting_order(box_side):
    """Nine player ids in order, or [] if the lineup is not posted."""
    order = box_side.get("battingOrder") or []
    if len(order) >= 9:
        return [int(p) for p in order[:9]]
    # Fallback: some feeds only populate per-player battingOrder codes.
    slots = []
    for key, p in (box_side.get("players") or {}).items():
        code = p.get("battingOrder")
        if code and str(code).endswith("00"):        # 100, 200 … starters
            slots.append((int(code), p["person"]["id"]))
    slots.sort()
    return [pid for _, pid in slots[:9]] if len(slots) >= 9 else []


def game_lineups(game_pk):
    """(away_ids, home_ids, away_sp, home_sp) — empty lists if not posted."""
    try:
        box = get(f"{STATS}/game/{game_pk}/boxscore")
    except Exception as e:
        print(f"  boxscore {game_pk} failed: {e}", file=sys.stderr)
        return [], [], None, None
    out = {}
    sp = {}
    for side in ("away", "home"):
        t = box["teams"][side]
        out[side] = batting_order(t)
        pitchers = t.get("pitchers") or []
        sp[side] = pitchers[0] if pitchers else None
    return out["away"], out["home"], sp["away"], sp["home"]


# ————————————————————————————————————————————————
# Per-hitter platoon wOBA, one batched call
# ————————————————————————————————————————————————
def hitter_splits(player_ids, season):
    """{pid: {'overall': wOBA, 'vL': wOBA, 'vR': wOBA, 'paL':, 'paR':}}"""
    out = {}
    ids = [str(p) for p in player_ids if p]
    if not ids:
        return out
    hydrate = (f"stats(group=[hitting],type=[season,statSplits],"
               f"sitCodes=[vl,vr],season={season})")
    try:
        data = get(f"{STATS}/people", personIds=",".join(ids), hydrate=hydrate)
    except Exception as e:
        print(f"  batched hitter splits failed ({e}); falling back per player",
              file=sys.stderr)
        for pid in ids:
            try:
                d = get(f"{STATS}/people/{pid}", hydrate=hydrate)
                data = {"people": d.get("people", [])}
                _absorb(out, data)
            except Exception:
                continue
        return out
    _absorb(out, data)
    return out


def _absorb(out, data):
    for person in data.get("people", []):
        pid = person.get("id")
        entry = {"name": person.get("fullName"), "overall": None,
                 "vL": None, "vR": None, "paL": 0, "paR": 0}
        for blob in person.get("stats", []):
            disp = (blob.get("type") or {}).get("displayName")
            for sp in blob.get("splits", []):
                st = sp.get("stat", {})
                w = woba_from(st)
                pa = float(st.get("plateAppearances") or 0)
                code = (sp.get("split") or {}).get("code")
                if disp == "season" and code is None:
                    entry["overall"] = w
                elif code == "vl":
                    entry["vL"], entry["paL"] = w, pa
                elif code == "vr":
                    entry["vR"], entry["paR"] = w, pa
        if entry["overall"] is None and (entry["vL"] or entry["vR"]):
            pool = [x for x in (entry["vL"], entry["vR"]) if x]
            entry["overall"] = sum(pool) / len(pool)
        out[pid] = entry


def regressed_split(entry, hand):
    """Hitter's wOBA vs `hand`, pulled toward his own overall line."""
    if not entry:
        return None
    base = entry.get("overall") or LEAGUE_WOBA
    split = entry.get("vL") if hand == "L" else entry.get("vR")
    pa = entry.get("paL" if hand == "L" else "paR") or 0
    if split is None:
        return base
    return (split * pa + base * PLATOON_BALLAST) / (pa + PLATOON_BALLAST)


def lineup_woba(values):
    w = SLOT_PA[:len(values)]
    if not w:
        return None
    return sum(v * x for v, x in zip(values, w)) / sum(w)


# ————————————————————————————————————————————————
# Model — imported, never re-implemented. One engine, one source of truth.
# ————————————————————————————————————————————————
import math

from baseline import TeamDay, predict as _predict


def predict(home, away):
    """home/away are slate dicts. Returns (p_home, run_delta)."""
    def td(s, name):
        return TeamDay(
            name=name, wins=s.get("wins") or 0, losses=s.get("losses") or 0,
            starter=s.get("starter") or "TBD",
            sp_rate=s.get("sp_rate") if s.get("sp_rate") is not None else 4.10,
            bp_rate=s.get("bp_rate") if s.get("bp_rate") is not None else 4.10,
            sp_ip=s.get("sp_ip") if s.get("sp_ip") is not None else 5.5,
            bp_ip3=s.get("bp_ip3"),
            woba_vs_hand=s.get("woba_vs_hand"), woba_overall=s.get("woba_overall"),
            ptype_rv100=s.get("ptype_rv100") or 0.0,
            travel_runs=s.get("travel_runs") or 0.0,
            park_fit_runs=s.get("park_fit_runs") or 0.0,
            history_runs=s.get("history_runs") or 0.0,
            lineup_woba=s.get("lineup_woba"), staff_fip=s.get("staff_fip"),
        )
    H, A = td(home, home.get("name", "Home")), td(away, away.get("name", "Away"))
    p = _predict(H, A)
    return p, 0.0


def kalshi_fee(price):
    return math.ceil(0.07 * price * (1 - price) * 100) / 100


# ————————————————————————————————————————————————
# Market refresh
# ————————————————————————————————————————————————
def refresh_market(ticker):
    if not ticker:
        return None
    try:
        data = get(f"{KALSHI}/markets/{ticker}")
        m = data.get("market") or {}
        bid, ask, last = kalshi_prices(m)
        if bid is not None and ask is not None:
            mid = (bid + ask) / 200
        elif last is not None:
            mid = last / 100
        else:
            mid = None
        return {"ticker": ticker, "yesBid": bid, "yesAsk": ask, "mid": mid,
                "volume": m.get("volume")}
    except Exception as e:
        print(f"  market {ticker} refresh failed: {e}", file=sys.stderr)
        return None


# ————————————————————————————————————————————————
# Email
# ————————————————————————————————————————————————
def send_email(subject, text, html, recipients):
    host = os.environ.get("SMTP_HOST")
    if not host or not recipients:
        print("  email not configured; printing instead\n" + text)
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("ALERT_FROM") or os.environ["SMTP_USER"]
    msg["To"] = ", ".join(recipients)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    port = int(os.environ.get("SMTP_PORT", "587"))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(msg)
    print(f"  emailed {len(recipients)} recipient(s)")
    return True


def compose(g, p, market, notes):
    home, away = g["home"], g["away"]
    home_fav = p >= 0.5
    fav = home if home_fav else away
    fav_p = p if home_fav else 1 - p

    lines = [
        f"{away['name']} @ {home['name']}",
        f"First pitch {g.get('startsAt', 'TBD')}",
        "",
        f"MODEL   {fav['name']} {fav_p*100:.1f}%   (home {p*100:.1f}%)",
    ]
    ev_line = ""
    if market and market.get("mid") is not None:
        yes_home = g.get("kalshi", {}).get("side") == "home"
        model_yes = p if yes_home else 1 - p
        mid = market["mid"]
        edge = model_yes - mid
        lines.append(f"KALSHI  {market['ticker']}")
        lines.append(f"        bid {market['yesBid']}c / ask {market['yesAsk']}c"
                     f"  mid {mid*100:.1f}%")
        lines.append(f"        model on that side {model_yes*100:.1f}%"
                     f"  -> {edge*100:+.1f} pts")
        if market.get("yesAsk"):
            ask = market["yesAsk"] / 100
            fee = kalshi_fee(ask)
            ev = model_yes - ask - fee
            ev_line = f"        EV buying YES at ask: {ev:+.3f} per contract (fee {fee:.2f})"
            lines.append(ev_line)
    else:
        lines.append("KALSHI  no market matched")

    lines += ["", "WHAT THE LINEUPS CHANGED"] + ["  " + n for n in notes]
    lines += ["", "Model disagreement is a hypothesis, not a signal. Check the",
              "inputs above before treating a gap as an edge."]
    text = "\n".join(lines)

    rows = "".join(f"<tr><td style='padding:2px 10px 2px 0;color:#666'>{n}</td></tr>"
                   for n in notes)
    html = f"""<div style="font-family:ui-monospace,Menlo,monospace;font-size:14px;line-height:1.6">
<div style="font-size:16px;font-weight:600">{away['name']} @ {home['name']}</div>
<div style="color:#666">First pitch {g.get('startsAt','TBD')}</div>
<hr style="border:none;border-top:1px solid #ddd;margin:12px 0">
<div style="font-size:22px;font-weight:600">{fav['name']} {fav_p*100:.1f}%</div>
<div style="color:#666">home {p*100:.1f}%</div>
<pre style="background:#f6f6f4;padding:12px;border-radius:4px;white-space:pre-wrap">{
  chr(10).join(lines[5:len(lines)-4])}</pre>
<div style="font-weight:600;margin-top:14px">What the lineups changed</div>
<table>{rows}</table>
<p style="color:#888;font-size:12px;margin-top:16px">Model disagreement is a hypothesis,
not a signal. Check the inputs before treating a gap as an edge.</p>
</div>"""

    subject = (f"{away.get('abbr') or away['name']} @ {home.get('abbr') or home['name']}"
               f" — {fav.get('abbr') or fav['name']} {fav_p*100:.0f}%")
    if market and market.get("mid") is not None:
        yes_home = g.get("kalshi", {}).get("side") == "home"
        model_yes = p if yes_home else 1 - p
        subject += f" vs Kalshi {market['mid']*100:.0f}% ({model_yes-market['mid']:+.1%})"
    return subject, text, html


# ————————————————————————————————————————————————
# Main
# ————————————————————————————————————————————————
def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {}


def save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=float, default=6.0,
                    help="only consider games starting within N hours")
    ap.add_argument("--min-edge", type=float, default=0.0,
                    help="skip games whose |model − market| is under this (0-1)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not SLATE.exists():
        print("no data/slate.json — run build_slate.py first", file=sys.stderr)
        return 1
    slate = json.loads(SLATE.read_text())
    season = slate.get("season", date.today().year)
    state = load_state()
    recipients = [r.strip() for r in
                  os.environ.get("ALERT_RECIPIENTS", "").split(",") if r.strip()]

    now = datetime.now(timezone.utc)
    sent_any = False

    for g in slate.get("games", []):
        pk = str(g["gamePk"])
        if pk in state:
            continue
        try:
            starts = datetime.fromisoformat(g["startsAt"].replace("Z", "+00:00"))
        except Exception:
            continue
        hours = (starts - now).total_seconds() / 3600
        if hours < -1 or hours > args.window:
            continue

        print(f"{g['away']['name']} @ {g['home']['name']} — {hours:.1f}h out")
        away_ids, home_ids, away_sp, home_sp = game_lineups(g["gamePk"])
        if len(away_ids) < 9 or len(home_ids) < 9:
            print("  lineups not posted yet")
            continue
        print("  lineups confirmed")

        notes = []

        # Confirmed starters can differ from the probables in the slate.
        hands = {}
        try:
            d = get(f"{STATS}/people", personIds=f"{away_sp},{home_sp}")
            for p in d.get("people", []):
                hands[p["id"]] = ((p.get("pitchHand") or {}).get("code"),
                                  p.get("fullName"))
        except Exception as e:
            print(f"  handedness lookup failed: {e}", file=sys.stderr)

        for side, sp_id in (("home", home_sp), ("away", away_sp)):
            listed = g[side].get("starterId")
            name = hands.get(sp_id, (None, None))[1]
            if listed and sp_id and listed != sp_id:
                notes.append(f"{g[side]['name']} starter changed: "
                             f"{g[side].get('starter')} -> {name or sp_id}")
                g[side]["starter"] = name
                g[side]["starterId"] = sp_id
                g[side]["sp_rate"] = None   # unknown arm falls back to league
                g[side]["sp_ip"] = None

        # Rebuild offense from the hitters actually playing.
        splits = hitter_splits(away_ids + home_ids, season)
        for side, ids, opp_sp in (("home", home_ids, away_sp),
                                  ("away", away_ids, home_sp)):
            hand = hands.get(opp_sp, (None, None))[0]
            if not hand:
                notes.append(f"{g[side]['name']}: opposing hand unknown, "
                             f"platoon term left at the season split")
                continue
            vals = [regressed_split(splits.get(pid), hand) for pid in ids]
            vals = [v for v in vals if v is not None]
            if len(vals) < 9:
                notes.append(f"{g[side]['name']}: only {len(vals)}/9 hitters had "
                             f"splits, keeping the season number")
                continue
            lw = lineup_woba(vals)
            was = g[side].get("woba_vs_hand")
            g[side]["woba_vs_hand"] = round(lw, 4)
            # v5: the roster rating should also reflect who is actually playing.
            base_vals = [ (splits.get(pid) or {}).get("overall") for pid in ids ]
            base_vals = [v for v in base_vals if v is not None]
            if len(base_vals) >= 9:
                g[side]["lineup_woba"] = round(lineup_woba(base_vals), 4)
            g[side]["lineup"] = [splits.get(p, {}).get("name") or p for p in ids]
            if was:
                notes.append(f"{g[side]['name']} lineup wOBA vs {hand}HP "
                             f"{lw:.4f} (team season split was {was:.4f}, "
                             f"{(lw-was)*1000:+.0f} points)")
            else:
                notes.append(f"{g[side]['name']} lineup wOBA vs {hand}HP {lw:.4f}")

        g["lineupConfirmed"] = True
        p, run_delta = predict(g["home"], g["away"])
        market = refresh_market((g.get("kalshi") or {}).get("ticker"))

        if args.min_edge > 0 and market and market.get("mid") is not None:
            yes_home = (g.get("kalshi") or {}).get("side") == "home"
            model_yes = p if yes_home else 1 - p
            if abs(model_yes - market["mid"]) < args.min_edge:
                print(f"  edge under threshold, skipping")
                state[pk] = {"sentAt": now.isoformat(), "skipped": "below min-edge"}
                continue

        subject, text, html = compose(g, p, market, notes)
        if args.dry_run:
            print("--- DRY RUN ---\n" + subject + "\n" + text + "\n")
        else:
            send_email(subject, text, html, recipients)
        state[pk] = {"sentAt": now.isoformat(), "modelHome": round(p, 4),
                     "marketMid": (market or {}).get("mid"),
                     "runDelta": round(run_delta, 3)}
        sent_any = True

    if not args.dry_run:
        save_state(state)
        SLATE.write_text(json.dumps(slate, indent=1))
    print("done" + ("" if sent_any else " — nothing to send"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
