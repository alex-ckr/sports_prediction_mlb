# MLB game model — v5, in the browser

The v5 engine from `baseline.py`, fed automatically, with every game priced against Kalshi.

## What changed in this version

**The site was showing wrong numbers, and here is why.** It ran v4 *and* it was running on
almost no data. When `build_slate.py` couldn't supply starter and bullpen quality, those
fields fell back to league average, the pitching term computed to exactly zero, and the
model quietly reduced to records plus home field — a coin flip wearing a percentage.

Measured on a Red Sox @ Athletics shape:

| What the model had | Favourite |
|---|---|
| Records + home field only | 55.6% |
| + starter FIP | 64.2% |
| + bullpen FIP | 67.4% |
| + platoon splits | 66.7% |
| Everything, v5 | **69.8%** |

Missing inputs were worth **14 points**. That's the gap, not the model version — v4 and v5
land within about two points of each other when both are fully fed.

Three fixes followed:

1. **`model.js` is now v5**, verified against `baseline.py` to 2.2e-16 across 200 random
   matchups. Roster-blended strength, `SP_TRUST`/`BP_TRUST`, HFA 1.12, travel/park/history,
   and the Platt layer.
2. **Nothing fails quietly.** Every source reports into a `diagnostics` block, every fetch
   has a fallback, and the Advanced tab shows source-by-source status.
3. **The page refuses to show a number it can't stand behind.** When starter or bullpen
   quality is missing, the percentage is hidden and the card says what's absent. A wrong
   number is worse than no number.

## Why Kalshi odds weren't appearing

Same root cause plus one of its own. The old matcher assumed a ticker grammar
(`KXMLBGAME-<date><away><home>-<TEAM>`) that isn't contractual. The new `match_kalshi`
scores every market on team codes and names, requires a floor, and — critically — returns
**nothing** rather than a guess when it can't resolve which team YES pays on, since a
market bound to the wrong side inverts every edge downstream.

To see what Kalshi actually returns:

```bash
python build_slate.py --debug-kalshi
```

The first dozen raw markets are also written into `slate.json` and shown on the Advanced
tab, so an unmatched slate tells you what it saw instead of just going blank.

## Using it

The **Games** tab is written for someone who has never placed a bet. Each card gives the
model's estimate as "70 in 100", the Kalshi price in cents, the break-even the price and
fee imply, and one plain sentence about whether the difference means anything. **How it
works** explains the three things that matter before acting on any of it. **Advanced**
holds the machinery: diagnostics, manual matchup, every constant.

## Files

| File | Role |
|---|---|
| `baseline.py` | The v5 engine. Single source of truth |
| `model.js` | JS port, parity-tested against it |
| `build_slate.py` | ETL → `data/slate.json`, with diagnostics |
| `watch_lineups.py` | Fires on confirmed lineups, emails the pick. Imports `baseline.py` |
| `index.html`, `app.js` | The interface |
| `travel.py`, `stadium.py`, `history.py` | v5 run adjustments, used by the ETL |
| `.github/workflows/` | `update-slate.yml` (4×/day), `lineup-alerts.yml` (every 10 min) |

## Deploy

```bash
git add . && git commit -m "v5" && git push
```

1. Settings → Pages → deploy from `main` / root
2. Settings → Actions → General → Workflow permissions → **Read and write**
3. Actions → **Refresh slate** → Run workflow

Step 3 is the one that matters. Until it runs there is no `data/slate.json` and the site
shows an empty state with these instructions.

Check the Advanced tab after the first run. If `pitching` reports FAILED, the bulk endpoint
is unavailable and the fallback is doing per-starter lookups — the slate still works but
bullpen numbers become staff proxies, flagged as such on each card.

## Still manual

`ptype_rv100` (pitch-type run values) has no free JSON source; it stays 0 unless entered by
hand. `history_runs` needs pooled batter-vs-pitcher data the StatsAPI doesn't expose cheaply.
Both default to neutral and contribute nothing rather than guessing.

## The honest part

`CAL_A`/`CAL_B` are still 0.0/1.0 — the calibration layer is wired but unfitted. Run
`patterns.py` against walk-forward predictions from `backtest.py` and paste the fitted
values in. Until then the model has no evidence it's calibrated, and an uncalibrated model's
disagreement with a liquid market is a hypothesis about your own inputs, not an edge.
