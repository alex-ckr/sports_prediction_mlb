"""
travel.py — travel distance, time zones, and schedule-spot effects.

Research basis (encoded as PRIORS, re-fit with patterns.py when the
game-log data lands):
  * Circadian studies of MLB (Song, Severini & Allada, PNAS 2017; ~20
    seasons, 40k+ games) find travel effects are real but SMALL, and
    ASYMMETRIC: crossing time zones EASTWARD hurts more than westward
    (body clocks lengthen easier than they shorten). Effects concentrate
    at 2-3 zones and decay within ~2 days per zone crossed.
  * Raw mileage matters less than zones + rest; a 2,600-mile flight with
    a day off is easier than a short hop after a night game.

Outputs a runs-per-game penalty to feed TeamDay.travel_runs.
"""
import math

# (lat, lon, tz offset from ET where ET=0, CT=-1, MT=-2, PT=-3)
PARKS = {
 "Angels":(33.800,-117.883,-3),"Astros":(29.757,-95.356,-1),
 "Athletics":(38.580,-121.513,-3),"Blue Jays":(43.641,-79.389,0),
 "Braves":(33.891,-84.468,0),"Brewers":(43.028,-87.971,-1),
 "Cardinals":(38.623,-90.193,-1),"Cubs":(41.948,-87.656,-1),
 "D-backs":(33.445,-112.067,-2),"Dodgers":(34.074,-118.240,-3),
 "Giants":(37.778,-122.389,-3),"Guardians":(41.496,-81.685,0),
 "Mariners":(47.591,-122.332,-3),"Marlins":(25.778,-80.220,0),
 "Mets":(40.757,-73.846,0),"Nationals":(38.873,-77.007,0),
 "Orioles":(39.284,-76.622,0),"Padres":(32.707,-117.157,-3),
 "Phillies":(39.906,-75.166,0),"Pirates":(40.447,-80.006,0),
 "Rangers":(32.747,-97.084,-1),"Rays":(27.768,-82.653,0),
 "Red Sox":(42.346,-71.097,0),"Reds":(39.097,-84.507,0),
 "Rockies":(39.756,-104.994,-2),"Royals":(39.051,-94.480,-1),
 "Tigers":(42.339,-83.049,0),"Twins":(44.982,-93.278,-1),
 "White Sox":(41.830,-87.634,-1),"Yankees":(40.829,-73.926,0),
}

# priors (runs per game, per unit) — tunable via patterns.py fits
EAST_ZONE_RUNS = 0.055   # per zone crossed eastward
WEST_ZONE_RUNS = 0.025   # per zone crossed westward (milder)
MILES_RUNS_PER_1000 = 0.010
NO_REST_MULT = 1.5       # played yesterday: full effect; day off: halve
DECAY_PER_DAY = 0.5      # effect fades ~half per rest day


def haversine_mi(a, b):
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    h = (math.sin((lat2-lat1)/2)**2
         + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2)
    return 3959 * 2 * math.asin(math.sqrt(h))


def travel_runs(prev_park: str, today_park: str, rest_days: int = 0) -> float:
    """Runs-per-game penalty for the TRAVELING team.
    prev_park: where they played their last game (their own park if home
    stand). rest_days: full off days between games."""
    if prev_park == today_park:
        return 0.0
    a, b = PARKS[prev_park], PARKS[today_park]
    miles = haversine_mi(a[:2], b[:2])
    tz = b[2] - a[2]                      # negative = traveled west
    zone_pen = (-tz) * WEST_ZONE_RUNS if tz < 0 else tz * EAST_ZONE_RUNS
    pen = zone_pen + miles / 1000 * MILES_RUNS_PER_1000
    pen *= DECAY_PER_DAY ** rest_days
    if rest_days == 0:
        pen *= NO_REST_MULT / 1.0 if False else 1.0  # base already no-rest
    return pen
