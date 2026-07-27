"""
baseline.py — v4. Adds an OFFENSE-vs-STARTER term to the v3 pitching model:

  A. Platoon: how the lineup hits the opposing starter's handedness.
     Inputs per team: `woba_vs_hand` (team/lineup wOBA vs today's opposing
     starter's hand) and `woba_overall`. The gap, over ~26 PA vs the
     starter, converts to runs at the standard ~1.15 wOBA-to-runs scale.
  B. Pitch-type matchup: `ptype_rv100` = the lineup's run value per 100
     pitches against THIS starter's specific mix (precomputed by
     pitchtype.py: sum over pitch types of mix% x team RV/100 vs type).
     Positive = the lineup feasts on what this pitcher throws.

Both default to neutral so v3-style calls still work unchanged.
"""
import math
from dataclasses import dataclass

REGRESSION_GAMES = 70
REC_WEIGHT = 0.35        # how much W-L record still counts when a
                         # roster (player-level) rating is available
LG_WOBA = 0.315
LG_RPG = 4.5             # league runs per team-game
TEAM_PA = 38             # team plate appearances per game
PYTH_EXP = 1.83
LEAGUE_RATE = 4.10
RUNS_PER_WIN = 10.0
HFA_ODDS = 1.12   # backtest-tuned: 1.19 overpriced home edge (actual ~52%)
LOGODDS_PER_WIN = 4.0
DEFAULT_SP_IP = 5.5
FATIGUE_FREE_IP3 = 9.0
FATIGUE_RUNS_PER_IP = 0.12
WOBA_TO_RUNS = 1.15
SP_TRUST = 0.65   # fraction of (rate - league) believed for starters
BP_TRUST = 0.75   # bullpens: bigger samples, trust a bit more
# Platt calibration, fitted 2026-07-27 on a walk-forward run over
# GL2023-GL2025 (5,103 games after warm-up) via make_preds.py + patterns.py.
# Brier 0.2454 -> 0.2450, logloss 0.6838 -> 0.6831. Set both back to
# 0.0 / 1.0 to disable. Re-fit from picks.csv once you have a few hundred.
CAL_A = -0.0012
CAL_B = 0.8777    # <1 = the model was overconfident by ~12%
PA_VS_STARTER = 26
PITCHES_VS_STARTER = 90


@dataclass
class TeamDay:
    name: str
    wins: int
    losses: int
    starter: str
    sp_rate: float                    # starter FIP (preferred) or ERA
    bp_rate: float = LEAGUE_RATE
    sp_ip: float = DEFAULT_SP_IP
    bp_ip3: float = None              # relief IP last 3 days
    # offense vs the OPPOSING starter:
    woba_vs_hand: float = None        # lineup wOBA vs opp starter's hand
    woba_overall: float = None
    ptype_rv100: float = 0.0          # RV/100 vs opp starter's pitch mix
    travel_runs: float = 0.0          # from travel.travel_runs()
    park_fit_runs: float = 0.0        # from stadium.fit_runs()
    history_runs: float = 0.0         # from history.bvp_runs/park_history_runs
    lineup_woba: float = None         # tonight's 9 hitters, PA-weighted
                                      # (platoon.lineup_woba); season-to-
                                      # date overall, NOT vs-hand (that
                                      # lives in woba_vs_hand)
    staff_fip: float = None           # season staff FIP (rotation+pen)


def regressed_wpct(w, l):
    g = w + l
    return (w + 0.5 * REGRESSION_GAMES) / (g + REGRESSION_GAMES)


def roster_wpct(t):
    """Player-level team strength: expected runs scored from the actual
    lineup's wOBA, runs allowed from season staff quality, through a
    pythagorean expectation. None if player inputs aren't provided."""
    if t.lineup_woba is None:
        return None
    rs = LG_RPG + (t.lineup_woba - LG_WOBA) / 1.15 * TEAM_PA / 9 * 9
    ra = LG_RPG + ((t.staff_fip or LEAGUE_RATE) - LEAGUE_RATE)
    return rs ** PYTH_EXP / (rs ** PYTH_EXP + ra ** PYTH_EXP)


def strength(t):
    """Blended team strength: mostly players, lightly the record.
    Record carries what rosters miss (defense, baserunning, managing,
    roster churn) but also luck -- hence the small weight."""
    rec = regressed_wpct(t.wins, t.losses)
    ros = roster_wpct(t)
    if ros is None:
        return rec
    return REC_WEIGHT * rec + (1 - REC_WEIGHT) * ros


def log5(a, b):
    return (a * (1 - b)) / (a * (1 - b) + b * (1 - a))


def effective_bp_rate(t):
    rate = t.bp_rate
    if t.bp_ip3 is not None and t.bp_ip3 > FATIGUE_FREE_IP3:
        rate += (t.bp_ip3 - FATIGUE_FREE_IP3) * FATIGUE_RUNS_PER_IP
    return rate


def pitching_runs_vs_avg(t):
    sp_ip = min(max(t.sp_ip, 0.0), 9.0)
    return ((t.sp_rate - LEAGUE_RATE) * SP_TRUST * sp_ip
            + (effective_bp_rate(t) - LEAGUE_RATE) * BP_TRUST * (9 - sp_ip)) / 9


def offense_runs_vs_avg(t):
    """Extra runs this lineup adds vs the opposing starter, beyond its
    overall quality (already priced into W-L record)."""
    runs = 0.0
    if t.woba_vs_hand is not None and t.woba_overall is not None:
        # wOBA gap x PA / 1.15 = runs added over those plate appearances
        runs += (t.woba_vs_hand - t.woba_overall) / WOBA_TO_RUNS * PA_VS_STARTER
    runs += t.ptype_rv100 * PITCHES_VS_STARTER / 100
    return runs


def predict(home, away):
    p = log5(strength(home), strength(away))
    logit = math.log(p / (1 - p)) + math.log(HFA_ODDS)
    delta = (pitching_runs_vs_avg(away) - pitching_runs_vs_avg(home)
             + offense_runs_vs_avg(home) - offense_runs_vs_avg(away)
             + (away.travel_runs - home.travel_runs)
             + (home.park_fit_runs - away.park_fit_runs)
             + (home.history_runs - away.history_runs))
    logit += (delta / RUNS_PER_WIN) * LOGODDS_PER_WIN
    logit = CAL_A + CAL_B * logit   # calibration fit on walk-forward preds
    return 1 / (1 + math.exp(-logit))


def report(home, away):
    p = predict(home, away)
    fav, prob = (home, p) if p >= 0.5 else (away, 1 - p)
    return f"{away.name} @ {home.name} -> {fav.name} {prob:.1%} (home {p:.1%})"
