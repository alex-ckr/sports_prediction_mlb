"""
fetch_statsapi.py — per-game results and pitching lines from MLB StatsAPI.

WHY THIS EXISTS
The model's starter input is currently "runs allowed in prior starts" —
all Retrosheet carries. It cannot tell a dominant-but-unlucky start from
a lucky-but-fraudulent one. FIP (K, BB, HBP, HR) can. This pulls the raw
ingredients per start, walk-forward computable, plus 2026 games/results,
which Retrosheet won't publish until winter.

WHAT IT WRITES (per season)
  data/statsapi_games_{season}.csv
      game_pk,date,home,away,home_score,away_score,home_sp,away_sp,status
  data/statsapi_pitching_{season}.csv
      game_pk,date,team,pitcher_id,pitcher_name,is_starter,
      outs,k,bb,hbp,hr,r,er
Team codes are RETROSHEET codes (mapped by immutable team id), so these
files join directly onto GL*.TXT and kalshi_history.csv.

USAGE (via the fetch-statsapi workflow, or locally):
  python fetch_statsapi.py --season 2025 --dump 2     # inspect payloads first
  python fetch_statsapi.py --season 2025              # full pull (~30 min)
Resume-safe: already-fetched game_pks are skipped on rerun.
"""
import argparse
import csv
import sys
import time
from pathlib import Path

import requests

API = "https://statsapi.mlb.com/api/v1"
UA = {"User-Agent": "mlb-model-research/1.0"}
TIMEOUT = 40

# Immutable StatsAPI team id -> Retrosheet code (aliases pre-applied:
# Athletics -> ATH regardless of season).
ID2R = {
    108: "ANA", 109: "ARI", 110: "BAL", 111: "BOS", 112: "CHN", 113: "CIN",
    114: "CLE", 115: "COL", 116: "DET", 117: "HOU", 118: "KCA", 119: "LAN",
    120: "WAS", 121: "NYN", 133: "ATH", 134: "PIT", 135: "SDN", 136: "SEA",
    137: "SFN", 138: "SLN", 139: "TBA", 140: "TEX", 141: "TOR", 142: "MIN",
    143: "PHI", 144: "ATL", 145: "CHA", 146: "MIA", 147: "NYA", 158: "MIL",
}


def get(path, **params):
    delay = 1.0
    for _ in range(6):
        try:
            r = requests.get(API + path, params=params or None,
                             headers=UA, timeout=TIMEOUT)
        except requests.RequestException:
            time.sleep(delay); delay *= 2
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(delay); delay *= 2
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"failed after retries: {path}")


def outs_from_ip(ip):
    """'5.2' -> 17 outs. StatsAPI thirds notation."""
    if ip in (None, ""):
        return 0
    whole, _, frac = str(ip).partition(".")
    try:
        return int(whole) * 3 + int(frac or 0)
    except ValueError:
        return 0


def season_schedule(season):
    """Every regular-season game: one call."""
    d = get("/schedule", sportId=1, season=season, gameType="R",
            hydrate="probablePitcher")
    games = []
    for day in d.get("dates", []):
        for g in day.get("games", []):
            games.append(g)
    return games


def parse_boxscore(game_pk):
    """(pitching_rows, home_sp, away_sp) from one final game."""
    box = get(f"/game/{game_pk}/boxscore")
    rows, sps = [], {}
    for side in ("home", "away"):
        t = box["teams"][side]
        code = ID2R.get(t["team"]["id"], t["team"].get("abbreviation", "?"))
        order = t.get("pitchers", [])
        sps[side] = order[0] if order else None
        for pid in order:
            pl = t["players"].get(f"ID{pid}")
            if not pl:
                continue
            st = (pl.get("stats") or {}).get("pitching") or {}
            if not st:
                continue
            rows.append({
                "team": code,
                "pitcher_id": pid,
                "pitcher_name": (pl.get("person") or {}).get("fullName", ""),
                "is_starter": 1 if pid == sps[side] else 0,
                "outs": outs_from_ip(st.get("inningsPitched")),
                "k": st.get("strikeOuts", 0),
                "bb": st.get("baseOnBalls", 0),
                "hbp": st.get("hitBatsmen", st.get("hitByPitch", 0)),
                "hr": st.get("homeRuns", 0),
                "r": st.get("runs", 0),
                "er": st.get("earnedRuns", 0),
            })
    return rows, sps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--limit", type=int, default=0, help="stop after N games (testing)")
    ap.add_argument("--dump", type=int, default=0,
                    help="print raw JSON for N schedule games and one boxscore, then exit")
    args = ap.parse_args()

    games = season_schedule(args.season)
    finals = [g for g in games
              if g.get("status", {}).get("abstractGameState") == "Final"
              and ID2R.get(g["teams"]["home"]["team"]["id"])]
    print(f"{args.season}: {len(games)} scheduled, {len(finals)} final")

    if args.dump:
        import json as J
        print("\n" + "=" * 70)
        print(f"RAW SCHEDULE GAMES (first {args.dump})")
        print("=" * 70)
        for g in finals[:args.dump]:
            print(J.dumps(g, indent=1)[:2200])
            print("-" * 70)
        if finals:
            pk = finals[0]["gamePk"]
            print("=" * 70)
            print(f"RAW BOXSCORE PITCHING for gamePk {pk}")
            print("=" * 70)
            box = get(f"/game/{pk}/boxscore")
            t = box["teams"]["home"]
            print("home team:", J.dumps(t.get("team", {}), indent=1)[:300])
            print("pitchers list:", t.get("pitchers"))
            pid = (t.get("pitchers") or [None])[0]
            if pid:
                print(f"first pitcher ID{pid} stats.pitching:")
                print(J.dumps((t["players"].get(f"ID{pid}", {}).get("stats") or {})
                              .get("pitching", {}), indent=1)[:1200])
            print("\nPARSED:")
            rows, sps = parse_boxscore(pk)
            for r in rows[:6]:
                print(" ", r)
        return 0

    Path("data").mkdir(exist_ok=True)
    gpath = Path(f"data/statsapi_games_{args.season}.csv")
    ppath = Path(f"data/statsapi_pitching_{args.season}.csv")

    done = set()
    if gpath.exists():
        with open(gpath) as f:
            done = {r["game_pk"] for r in csv.DictReader(f)}
        print(f"resuming — {len(done)} games already fetched")

    gf = open(gpath, "a", newline="")
    pf = open(ppath, "a", newline="")
    gw = csv.DictWriter(gf, fieldnames=[
        "game_pk", "date", "home", "away", "home_score", "away_score",
        "home_sp", "away_sp", "status"])
    pw = csv.DictWriter(pf, fieldnames=[
        "game_pk", "date", "team", "pitcher_id", "pitcher_name",
        "is_starter", "outs", "k", "bb", "hbp", "hr", "r", "er"])
    if not done:
        gw.writeheader(); pw.writeheader()

    todo = [g for g in finals if str(g["gamePk"]) not in done]
    if args.limit:
        todo = todo[:args.limit]
    fetched = errors = 0
    for i, g in enumerate(todo, 1):
        pk = g["gamePk"]
        try:
            rows, sps = parse_boxscore(pk)
        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"  boxscore failed for {pk}: {e}", file=sys.stderr)
            continue
        date = (g.get("officialDate") or g.get("gameDate", ""))[:10].replace("-", "")
        gw.writerow({
            "game_pk": pk, "date": date,
            "home": ID2R[g["teams"]["home"]["team"]["id"]],
            "away": ID2R[g["teams"]["away"]["team"]["id"]],
            "home_score": g["teams"]["home"].get("score", ""),
            "away_score": g["teams"]["away"].get("score", ""),
            "home_sp": sps.get("home") or "",
            "away_sp": sps.get("away") or "",
            "status": "F",
        })
        for r in rows:
            pw.writerow({"game_pk": pk, "date": date, **r})
        fetched += 1
        if i % 25 == 0:
            gf.flush(); pf.flush()
            print(f"  {i}/{len(todo)} games", end="\r", flush=True)
    gf.close(); pf.close()
    print(f"\n{fetched} games fetched, {errors} errors")
    print(f"wrote {gpath} and {ppath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
