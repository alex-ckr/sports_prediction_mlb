"""
make_preds.py — the missing link between backtest.py and patterns.py.

backtest.py walks a season and prints a report. patterns.py wants a CSV of
per-game predictions with the drivers attached. This writes that CSV.

    python make_preds.py GL2023.TXT GL2024.TXT GL2025.TXT
    python patterns.py backtest_preds.csv

Columns written: prob_home, home_won, rec_gap, pitch_edge, date, home, vis.

WHAT THIS FIT CAN AND CANNOT SEE
Retrosheet game logs carry records, scores and starter ids — nothing about
lineups, bullpen usage, platoon splits or wOBA. So the walk-forward runs a
REDUCED v5: team strength from records only, and a starter-quality proxy
built from runs allowed in that pitcher's prior starts.

That means the fitted CAL_A/CAL_B correct the overconfidence of the reduced
model. Applying them to the full model is a reasonable first move — if the
skeleton is overconfident the fleshed-out version usually is too — but it is
an approximation, not a measurement. Re-fit against your own settled picks
in picks.csv once you have a few hundred.
"""
import csv
import sys
from collections import defaultdict

import baseline as B
from backtest import load_games


def walk(games, sp_prior=5):
    """Strictly walk-forward: every input uses only games BEFORE this one."""
    W, L = defaultdict(int), defaultdict(int)
    sp_runs, sp_starts = defaultdict(float), defaultdict(int)
    lg_runs, lg_games = 0.0, 0
    rows = []

    for g in games:
        lg_mean = (lg_runs / lg_games) if lg_games >= 50 else 4.4

        def sp_rate(pid):
            n = sp_starts[pid]
            if n == 0:
                return B.LEAGUE_RATE
            raw = sp_runs[pid] / n
            shrunk = lg_mean + (raw - lg_mean) * n / (n + sp_prior)
            return B.LEAGUE_RATE + (shrunk - lg_mean)

        home = B.TeamDay(g["home"], W[g["home"]], L[g["home"]], g["hsp"], sp_rate(g["hsp"]))
        away = B.TeamDay(g["vis"], W[g["vis"]], L[g["vis"]], g["vsp"], sp_rate(g["vsp"]))

        p = B.predict(home, away)
        hw = 1 if g["hscore"] > g["vscore"] else 0

        rows.append({
            "prob_home": f"{p:.6f}",
            "home_won": hw,
            "rec_gap": f"{B.regressed_wpct(home.wins, home.losses) - B.regressed_wpct(away.wins, away.losses):.6f}",
            "pitch_edge": f"{B.pitching_runs_vs_avg(away) - B.pitching_runs_vs_avg(home):.6f}",
            "date": g["date"], "home": g["home"], "vis": g["vis"],
        })

        # state updates happen AFTER the prediction
        if hw:
            W[g["home"]] += 1; L[g["vis"]] += 1
        else:
            W[g["vis"]] += 1; L[g["home"]] += 1
        sp_runs[g["hsp"]] += g["vscore"]; sp_starts[g["hsp"]] += 1
        sp_runs[g["vsp"]] += g["hscore"]; sp_starts[g["vsp"]] += 1
        lg_runs += g["vscore"] + g["hscore"]; lg_games += 2

    return rows


def main():
    paths = sys.argv[1:] or ["GL2025.TXT"]
    out = "backtest_preds.csv"
    all_rows = []
    for path in paths:
        games = load_games(path)
        rows = walk(games)
        # Drop the first 30% of each season: records are pure noise in April
        # and a fit dominated by them will be wrong about July.
        keep = rows[int(len(rows) * 0.3):]
        all_rows.extend(keep)
        print(f"{path}: {len(games)} games, kept {len(keep)} after warm-up")

    fields = ["prob_home", "home_won", "rec_gap", "pitch_edge", "date", "home", "vis"]
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(all_rows)
    print(f"\nwrote {out} — {len(all_rows)} rows")
    print(f"HFA_ODDS in use: {B.HFA_ODDS}")
    print(f"\nnext:  python patterns.py {out}")


if __name__ == "__main__":
    main()
