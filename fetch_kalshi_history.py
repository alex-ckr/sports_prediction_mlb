"""
fetch_kalshi_history.py — pull settled MLB markets and their closing prices.

YOU run this, not me. It needs your Kalshi credentials and my sandbox has no
route to Kalshi anyway. It writes kalshi_history.csv, which contains no
secrets — that file is what you hand over for analysis.

WHAT IT COLLECTS
For every settled KXMLBGAME market in the window: the last quoted bid/ask
before the game started (the closing line), plus how it settled. The closing
line is the number that matters — it's the market's final answer, and beating
it is the whole test.

CREDENTIALS
Market-data endpoints are documented as public, so it tries unauthenticated
first. If Kalshi returns 401 it signs requests, reading:

    KALSHI_KEY_ID            your API key ID (the UUID)
    KALSHI_PRIVATE_KEY_PATH  path to the .pem private key file

Set them as environment variables. Never paste them into a file, a repo, or
a chat window. If you have already pasted a key anywhere, rotate it first.

    pip install requests cryptography
    python fetch_kalshi_history.py --days 400

OUTPUT: kalshi_history.csv
    date, ticker, event, yes_team, close_bid, close_ask, close_mid,
    volume, open_interest, result, settled_ts
"""
import argparse
import base64
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import re

import requests

LIVE = "https://api.elections.kalshi.com/trade-api/v2"
HIST = "https://external-api.kalshi.com/trade-api/v2"
SERIES = "KXMLBGAME"
UA = {"User-Agent": "mlb-model-backtest/1.0"}
TIMEOUT = 40


# ————————————————————————————————————————————————
# Optional request signing
# ————————————————————————————————————————————————
_signer = None


def load_signer():
    """Returns a callable(method, path) -> headers, or None if unavailable."""
    global _signer
    if _signer is not None:
        return _signer or None
    key_id = os.environ.get("KALSHI_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not key_path:
        _signer = False
        return None
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        print("cryptography not installed; run: pip install cryptography", file=sys.stderr)
        _signer = False
        return None
    try:
        with open(key_path, "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)
    except Exception as e:
        print(f"could not read private key: {e}", file=sys.stderr)
        _signer = False
        return None

    def sign(method, path):
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path).encode()
        sig = private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": key_id,
                "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}

    _signer = sign
    print("request signing enabled")
    return sign


def get(base, path, **params):
    """GET with signed-header retry on 401 and exponential backoff on 429/5xx."""
    url = base + path
    headers = dict(UA)
    signed = False
    delay = 1.0
    for attempt in range(6):
        try:
            r = requests.get(url, params=params or None, headers=headers,
                             timeout=TIMEOUT)
        except requests.RequestException:
            time.sleep(delay); delay *= 2
            continue
        if r.status_code == 401 and not signed:
            sign = load_signer()
            if not sign:
                r.raise_for_status()
            headers.update(sign("GET", "/trade-api/v2" + path))
            signed = True
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(delay); delay *= 2
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"failed after retries: {url}")



MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
          "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


def to_ts(v):
    """Unix seconds from an int, a millisecond int, or an ISO-8601 string."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        v = int(v)
        return v // 1000 if v > 10_000_000_000 else v
    try:
        txt = str(v).replace("Z", "+00:00")
        return int(datetime.fromisoformat(txt).timestamp())
    except Exception:
        return None


def field_ts(m, *names):
    """First parseable timestamp among several candidate field names."""
    for n in names:
        t = to_ts(m.get(n))
        if t:
            return t
    return None



TZ_OFFSET = {"EDT": 4, "EST": 5, "CDT": 5, "CST": 6,
             "MDT": 6, "MST": 7, "PDT": 7, "PST": 8, "UTC": 0, "GMT": 0}
MONTH_NAME = {"JANUARY":1,"FEBRUARY":2,"MARCH":3,"APRIL":4,"MAY":5,"JUNE":6,
              "JULY":7,"AUGUST":8,"SEPTEMBER":9,"OCTOBER":10,"NOVEMBER":11,
              "DECEMBER":12}


def game_start(m):
    """First pitch, in unix seconds.

    Kalshi states it in plain English in rules_primary: "originally scheduled
    for May 27, 2026 at 4:10 PM EDT". That is unambiguous, unlike the ticker,
    whose time encoding varies in length. Falls back to open/close times.
    """
    rules = (m.get("rules_primary") or "") + " " + (m.get("rules_secondary") or "")
    hit = re.search(
        r"scheduled for\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\s+at\s+"
        r"(\d{1,2}):(\d{2})\s*([AP])M\s+([A-Z]{3})", rules)
    if hit:
        mon, day, year, hh, mm, ampm, tz = hit.groups()
        mnum = MONTH_NAME.get(mon.upper())
        if mnum:
            hh = int(hh) % 12 + (12 if ampm == "P" else 0)
            try:
                local = datetime(int(year), mnum, int(day), hh, int(mm),
                                 tzinfo=timezone.utc)
                return int(local.timestamp()) + TZ_OFFSET.get(tz, 0) * 3600
            except ValueError:
                pass
    # Fallbacks: markets close when the game ends, so back off ~3.5 hours.
    close = field_ts(m, "close_time", "expected_expiration_time", "settlement_ts")
    if close:
        return close - int(3.5 * 3600)
    return None


def yes_team(m):
    """Which team a YES contract pays on."""
    r = re.search(r"^If\s+(.+?)\s+wins", m.get("rules_primary") or "")
    if r:
        return r.group(1).strip()
    return (m.get("yes_sub_title") or m.get("ticker", "").rsplit("-", 1)[-1])


def start_from_ticker(ticker):
    """KXMLBGAME-26JUL292210SEALAD-SEA -> first pitch, 2026-07-29 22:10 UTC.

    The market's close_time is when it SETTLES, which is after the game ends.
    The closing line is the last quote before first pitch, and the ticker is
    the only place that time is reliably encoded.
    """
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{0,4})", ticker or "")
    if not m:
        return None
    yy, mon, dd, tail = m.groups()
    if mon not in MONTHS:
        return None
    # Kalshi strips leading zeros from the time, so the tail is 0-4 digits.
    tail = tail or "0"
    hhmm = tail.zfill(4)
    hh, mm = int(hhmm[:2]), int(hhmm[2:])
    if hh > 23 or mm > 59:
        return None
    try:
        return int(datetime(2000 + int(yy), MONTHS[mon], int(dd), hh, mm,
                            tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


# ————————————————————————————————————————————————
# Discovery
# ————————————————————————————————————————————————
def historical_cutoff():
    """Markets settled before this timestamp live in the historical API.

    The response carries three cutoffs — market_settled_ts, trades_created_ts
    and orders_updated_ts. Only the first governs markets and candlesticks.
    Kalshi targets a 3-month live window, so anything older than roughly a
    season must come from /historical.
    """
    try:
        d = get(HIST, "/historical/cutoff")
        inner = d.get("cutoff") if isinstance(d.get("cutoff"), dict) else d
        ts = field_ts(inner, "market_settled_ts", "markets_settled_ts", "cutoff_ts")
        if ts:
            return ts
        print(f"unrecognised cutoff payload: {json.dumps(d)[:300]}", file=sys.stderr)
    except Exception as e:
        print(f"cutoff lookup failed ({e})", file=sys.stderr)
    # Fall back to the documented 3-month live window.
    fallback = int(time.time()) - 90 * 86400
    print(f"assuming the documented 3-month window", file=sys.stderr)
    return fallback


def list_markets(base, path, since_ts, until_ts):
    out, cursor, pages = [], None, 0
    while pages < 60:
        params = {"series_ticker": SERIES, "limit": 200,
                  "min_close_ts": since_ts, "max_close_ts": until_ts}
        if cursor:
            params["cursor"] = cursor
        try:
            d = get(base, path, **params)
        except Exception as e:
            print(f"  page {pages} failed: {e}", file=sys.stderr)
            break
        batch = d.get("markets", [])
        out.extend(batch)
        cursor = d.get("cursor")
        pages += 1
        print(f"  ...{len(out)} markets", end="\r", flush=True)
        if not cursor or not batch:
            break
    print()
    return out


def candlestick_variants(start_ts, end_ts, interval):
    """Kalshi's param spelling is not documented consistently. Try each."""
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        {"start_ts": start_ts, "end_ts": end_ts, "period_interval": interval},
        {"start_time": iso(start_ts), "end_time": iso(end_ts), "period_interval": interval},
        {"min_ts": start_ts, "max_ts": end_ts, "period_interval": interval},
        {"start_ts": start_ts, "end_ts": end_ts, "period_interval_minutes": interval},
        {"period_interval": interval},
    ]


# Routes are remembered PER TIER. The historical and live APIs need
# different endpoints, and a route that works for one returns nothing for
# the other — which is exactly how games after the cutoff went missing.
CANDLE_ROUTE = {}


def candlesticks(base, ticker, start_ts, end_ts, interval=60, verbose=False,
                 tier=None):
    tier = tier or ("hist" if base == HIST else "live")

    if tier == "hist":
        paths = [(HIST, f"/historical/markets/{ticker}/candlesticks"),
                 (LIVE, f"/series/{SERIES}/markets/{ticker}/candlesticks"),
                 (LIVE, f"/markets/{ticker}/candlesticks")]
    else:
        paths = [(LIVE, f"/series/{SERIES}/markets/{ticker}/candlesticks"),
                 (LIVE, f"/markets/{ticker}/candlesticks"),
                 (HIST, f"/historical/markets/{ticker}/candlesticks")]

    variants = candlestick_variants(start_ts, end_ts, interval)

    cached = CANDLE_ROUTE.get(tier)
    if cached:
        b, tpl, vi = cached
        try:
            d = get(b, tpl.format(ticker=ticker), **variants[vi])
            c = d.get("candlesticks") or []
            if c:
                return c
            # Empty is NOT proof the route is right — fall through and
            # re-probe rather than silently dropping the market.
        except Exception:
            pass

    for b, path in paths:
        tpl = path.replace(ticker, "{ticker}")
        for vi, params in enumerate(variants):
            try:
                d = get(b, path, **params)
            except Exception as e:
                if verbose:
                    print(f"    FAIL [{tier}] {b}{path} {list(params)} -> {e}")
                continue
            c = d.get("candlesticks") or []
            if verbose:
                print(f"    OK   [{tier}] {b}{path} {list(params)} -> {len(c)} candles")
            if c:
                CANDLE_ROUTE[tier] = (b, tpl, vi)
                return c
    return []


def dec(v):
    """Kalshi mixes integer cents and decimal strings. Normalise to cents."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(round(f * 100)) if f <= 1.0 else int(round(f))


SHOWN_SHAPE = set()


def candle_ts(c):
    """Period end, whatever it is called and whatever unit it uses."""
    for k in ("end_period_ts", "end_ts", "ts", "timestamp", "period_end",
              "end_time", "end_period_time"):
        if k in c:
            t = to_ts(c[k])
            if t:
                return t
    return None


def candle_leg(c, *names, field="close"):
    """A price from a candle, tolerating every shape Kalshi serves.

    The historical tier nests {"yes_bid": {"close": "0.5600"}}. Other tiers
    have used flat scalars and _dollars suffixes. Rather than assume, try
    each spelling and each container type.
    """
    for base in names:
        for key in (base, base + "_dollars", base + "_cents"):
            if key not in c:
                continue
            blob = c[key]
            if isinstance(blob, dict):
                # Inner names vary by tier too: hist uses "close",
                # live uses "close_dollars". Try every combination.
                for f in (field, "close", "mean", "last", "open", "price"):
                    for fk in (f, f + "_dollars", f + "_cents"):
                        if fk in blob:
                            v = dec(blob[fk])
                            if v is not None:
                                return v
            else:
                v = dec(blob)
                if v is not None:
                    return v
    return None


def closing_quote(candles, before_ts, tier="?"):
    """Last candle that closed at or before first pitch."""
    if candles and tier not in SHOWN_SHAPE:
        SHOWN_SHAPE.add(tier)
        print(f"\n  [{tier}] first candle shape: "
              f"{json.dumps(candles[0])[:400]}\n", flush=True)

    usable = []
    for c in candles:
        t = candle_ts(c)
        if t and t <= before_ts:
            usable.append((t, c))
    if not usable:
        return None
    t, c = max(usable, key=lambda x: x[0])
    return {"bid": candle_leg(c, "yes_bid", "bid"),
            "ask": candle_leg(c, "yes_ask", "ask"),
            "last": candle_leg(c, "price", "last_price", "close"),
            "volume": c.get("volume") or c.get("volume_fp"),
            "open_interest": c.get("open_interest") or c.get("open_interest_fp"),
            "ts": t}


# ————————————————————————————————————————————————
# Main
# ————————————————————————————————————————————————
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400, help="how far back to look")
    ap.add_argument("--out", default="data/kalshi_history.csv")
    ap.add_argument("--interval", type=int, default=60,
                    choices=[1, 60, 1440], help="candle size in minutes")
    ap.add_argument("--limit", type=int, default=0, help="stop after N markets (testing)")
    ap.add_argument("--dump", type=int, default=0,
                    help="print raw JSON for N markets plus one candlestick "
                         "response, then exit. Use this when fields look wrong.")
    args = ap.parse_args()

    now = int(time.time())
    since = now - args.days * 86400
    cutoff = historical_cutoff()
    print(f"window: {datetime.fromtimestamp(since, timezone.utc):%Y-%m-%d} to now")
    print(f"historical cutoff: {datetime.fromtimestamp(cutoff, timezone.utc):%Y-%m-%d}")

    print("\nlisting historical markets...")
    markets = list_markets(HIST, "/historical/markets", since, min(cutoff, now))
    if cutoff < now:
        print("listing live markets...")
        markets += list_markets(LIVE, "/markets", cutoff, now)

    if args.dump:
        print("\n" + "=" * 70)
        print("RAW CUTOFF")
        print("=" * 70)
        try:
            print(json.dumps(get(HIST, "/historical/cutoff"), indent=2))
        except Exception as e:
            print(f"failed: {e}")
        print("\n" + "=" * 70)
        print(f"RAW MARKETS (first {args.dump})")
        print("=" * 70)
        for m in markets[:args.dump]:
            print(json.dumps(m, indent=2)[:2500])
            print("-" * 70)
        if markets:
            m0 = markets[0]
            print("\n" + "=" * 70)
            print("PARSED FROM THAT MARKET")
            print("=" * 70)
            gs = game_start(m0)
            print(f"  first pitch : {datetime.fromtimestamp(gs, timezone.utc)} UTC"
                  if gs else "  first pitch : UNPARSED")
            print(f"  yes team    : {yes_team(m0)}")
            print(f"  open_time   : {m0.get('open_time')}")
            print(f"  close_time  : {m0.get('close_time')}")
            print("\n" + "=" * 70)
            print("CANDLESTICK PROBE (every route x every param spelling)")
            print("=" * 70)
            o = field_ts(m0, "open_time", "created_time") or (gs - 3 * 86400)
            candlesticks(HIST, m0.get("ticker"), o, (gs or o) + 3600, 60, verbose=True)
            t = markets[0].get("ticker")
            print("\n" + "=" * 70)
            print(f"RAW CANDLESTICKS for {t}")
            print("=" * 70)
            now_ts = int(time.time())
            for base, path in ((HIST, f"/historical/markets/{t}/candlesticks"),
                               (LIVE, f"/series/{SERIES}/markets/{t}/candlesticks"),
                               (LIVE, f"/markets/{t}/candlesticks")):
                try:
                    d = get(base, path, start_ts=now_ts - 400 * 86400,
                            end_ts=now_ts, period_interval=60)
                    print(f"OK  {base}{path}")
                    print(json.dumps(d, indent=2)[:2000])
                    break
                except Exception as e:
                    print(f"FAIL {base}{path} -> {e}")
        return 0

    settled = [m for m in markets if m.get("result") in ("yes", "no")]
    print(f"{len(markets)} markets, {len(settled)} settled\n")
    if not settled:
        print("Nothing settled came back. Either the window is too recent or the\n"
              "series ticker changed. Try --days 700, or check the ticker on Kalshi.")
        return 1

    # Two markets per game (one per team) and we only need one — the other
    # side is one minus this one. Halves the request count.
    seen_events, unique = set(), []
    for m in settled:
        ev = m.get("event_ticker") or m.get("ticker", "")[:-4]
        if ev in seen_events:
            continue
        seen_events.add(ev)
        unique.append(m)
    print(f"{len(unique)} distinct games after collapsing both sides")

    if args.limit:
        unique = unique[:args.limit]

    # Resume: skip anything already in the output file.
    done = set()
    try:
        with open(args.out) as f:
            for r in csv.DictReader(f):
                done.add(r["ticker"])
        if done:
            print(f"resuming — {len(done)} games already collected")
    except FileNotFoundError:
        pass

    rows, no_quote, no_time, no_price, out_of_range = [], 0, 0, 0, 0
    for i, m in enumerate(unique, 1):
        ticker = m.get("ticker", "")
        if ticker in done:
            continue
        # close_time is settlement; first pitch comes from the ticker.
        settle_ts = field_ts(m, "close_time", "settlement_ts", "expiration_time")
        start_ts = game_start(m)
        open_ts = field_ts(m, "open_time", "created_time") or (
            start_ts - 3 * 86400 if start_ts else None)
        if not start_ts or not open_ts:
            no_time += 1
            continue

        tier = "hist" if (settle_ts or start_ts) < cutoff else "live"
        base = HIST if tier == "hist" else LIVE
        candles = candlesticks(base, ticker, open_ts, start_ts + 3600,
                               args.interval, tier=tier)
        q = closing_quote(candles, start_ts, tier)
        if not q:
            no_quote += 1
            if i <= 3:
                print(f"\n  no candles for {ticker} "
                      f"(window {open_ts}..{start_ts}, base {base})")
            continue

        if q["bid"] is None and q["ask"] is None:
            # Some payloads quote only the NO side; YES is 100 minus it.
            nb = candle_leg({}, "no_bid")
        mid = None
        if q["bid"] is not None and q["ask"] is not None:
            mid = round((q["bid"] + q["ask"]) / 200, 4)
        elif q["last"] is not None:
            mid = round(q["last"] / 100, 4)
        # A pre-game moneyline is basically never outside 3-97%. Anything
        # beyond that is a post-first-pitch candle and must not be counted
        # as a closing line.
        if mid is None:
            no_price += 1
            continue
        if mid < 0.03 or mid > 0.97:
            out_of_range += 1
            continue
        rows.append({
            "date": datetime.fromtimestamp(start_ts, timezone.utc).strftime("%Y-%m-%d"),
            "ticker": ticker,
            "event": m.get("event_ticker", ""),
            "yes_team": yes_team(m),
            "winner": m.get("expiration_value", ""),
            "close_bid": q["bid"], "close_ask": q["ask"], "close_mid": mid,
            "volume": q["volume"] if q["volume"] is not None else m.get("volume_fp"),
            "open_interest": q["open_interest"],
            "result": m.get("result"),
            "settled_ts": start_ts,
        })
        if i % 25 == 0:
            print(f"  {i}/{len(unique)} games, {len(rows)} with prices",
                  end="\r", flush=True)

    print(f"\n\n{len(rows)} games with a closing quote")
    print(f"  {no_quote} had no candle before first pitch")
    print(f"  {no_price} had candles but no readable price  <- payload shape")
    print(f"  {out_of_range} had a price outside 3-97% (post-game candle)")
    print(f"  {no_time} had no usable game time")
    print(f"working routes: {CANDLE_ROUTE if CANDLE_ROUTE else 'none'}")
    if rows:
        recent = sorted(r["date"] for r in rows)
        print(f"coverage: {recent[0]} to {recent[-1]}")
        post = sum(1 for r in rows if r["settled_ts"] > cutoff)
        print(f"  {post} games from after the historical cutoff "
              f"({datetime.fromtimestamp(cutoff, timezone.utc):%Y-%m-%d})")
        if post == 0:
            print("  WARNING: nothing after the cutoff — the live-tier "
                  "candlestick route is not working")
    if not rows:
        print("No candlesticks came back. Try --interval 1440.")
        return 1

    fields = ["date", "ticker", "event", "yes_team", "winner", "close_bid",
              "close_ask", "close_mid", "volume", "open_interest", "result",
              "settled_ts"]
    from pathlib import Path as _P
    _P(args.out).parent.mkdir(parents=True, exist_ok=True)
    if done:
        try:
            with open(args.out) as f:
                rows = list(csv.DictReader(f)) + rows
            print(f"merged with {len(done)} previously collected games")
        except FileNotFoundError:
            pass
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["date"]))

    dates = sorted({r["date"] for r in rows})
    print(f"wrote {args.out}")
    print(f"  {len(rows)} rows across {len(dates)} days, {dates[0]} to {dates[-1]}")
    print(f"\nThis file contains no credentials. It is safe to share.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
