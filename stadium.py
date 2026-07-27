"""
stadium.py — individual-park patterns for WIN probability (not totals).

Two truths worth encoding:
  1. A park's overall run environment mostly CANCELS for win prob --
     both teams bat there. What doesn't cancel is FIT:
     * HR-reliant lineup in an HR-suppressing park loses more offense
       than a contact lineup does.
     * A fly-ball pitcher in an HR park is exposed; a GB pitcher isn't.
  2. Team-specific "plays well at stadium X" splits are tiny samples
     (a road team sees a park ~3-6 games/yr) -- shrink to near-zero
     unless multi-year data says otherwise.

fit_runs() returns a runs adjustment for one side. HR park factors are
static approximations; replace with yearly Savant park factors via the
pipeline. Team HR-reliance = share of runs scored via HR vs league avg
(~42%); pitcher FB tendency from Savant batted-ball data.
"""
# HR park factor, 100 = neutral (approx; refresh from Savant yearly)
HR_PF = {
 "Yankees":118,"Reds":117,"Dodgers":112,"Phillies":110,"Brewers":109,
 "Rangers":107,"Angels":106,"White Sox":105,"Braves":104,"Rockies":110,
 "Astros":103,"Orioles":102,"Blue Jays":101,"Cubs":100,"Mets":99,
 "Nationals":99,"Padres":98,"Twins":98,"Mariners":97,"D-backs":97,
 "Rays":96,"Red Sox":96,"Guardians":95,"Cardinals":94,"Tigers":93,
 "Athletics":93,"Royals":92,"Pirates":91,"Marlins":90,"Giants":88,
}
LEAGUE_HR_RUN_SHARE = 0.42
FIT_SCALE = 0.9  # runs impact of full mismatch; deliberately modest


def fit_runs(park: str, hr_run_share: float = None,
             pitcher_fb_pct: float = None) -> float:
    """Net runs adjustment for a lineup (positive = park helps them)
    and optionally their opponent-facing pitcher exposure."""
    pf = (HR_PF.get(park, 100) - 100) / 100
    runs = 0.0
    if hr_run_share is not None:
        # HR-reliant lineups gain/lose more from the park's HR lean
        runs += pf * (hr_run_share - LEAGUE_HR_RUN_SHARE) * FIT_SCALE * 4.5
    if pitcher_fb_pct is not None:
        # fly-ball pitchers exposed in HR parks (league FB% ~ .36)
        runs -= pf * (pitcher_fb_pct - 0.36) * FIT_SCALE * 4.5
    return runs
