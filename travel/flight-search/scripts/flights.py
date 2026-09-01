#!/usr/bin/env python3
"""SerpApi flight helper for Hermes. Standard library only."""
import argparse, json, os, sys, time, urllib.error, urllib.parse, urllib.request

BASE = "https://serpapi.com/search"
CACHE = os.path.expanduser("~/.cache/hermes-flights/last.json")

# max_stops -> SerpApi `stops`. Verified: 2 returns only 1-stop routes.
STOPS_MAP = {0: 1, 1: 2, 2: 3}   # 0=any, 1=nonstop, 2=<=1 stop, 3=<=2 stops

# friendly name -> SerpApi `travel_class`
CLASS_MAP = {
    "economy": 1, "basic": 1,
    "premium": 2, "premium-economy": 2,
    "business": 3, "first": 4,
}
CLASS_NAME = {1: "Economy", 2: "Premium economy", 3: "Business", 4: "First"}


def die(msg):
    print(json.dumps({"error": msg}, indent=2))
    sys.exit(1)


def call(params):
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        die("SERPAPI_KEY not set — check ~/.hermes/.env")
    params = {k: v for k, v in params.items() if v not in (None, "")}
    params["api_key"] = key
    url = BASE + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        die("HTTP %s: %s" % (e.code, e.read()[:300].decode(errors="replace")))
    except Exception as e:
        die("request failed: %s" % e)
    if isinstance(data, dict) and data.get("error"):
        die("SerpApi: %s" % data["error"])
    return data


def resolve_class(a):
    """--class beats --travel-class. Returns int 1-4."""
    if getattr(a, "cabin", None):
        c = a.cabin.strip().lower()
        if c not in CLASS_MAP:
            die("unknown class '%s' — use: %s"
                % (a.cabin, ", ".join(sorted(set(CLASS_MAP)))))
        return CLASS_MAP[c]
    return getattr(a, "travel_class", 1) or 1


def summarize_insights(data):
    """Collapse price_insights.price_history (60+ points) into 4 numbers."""
    pi = data.get("price_insights")
    if not pi:
        return None
    pi = dict(pi)
    hist = pi.pop("price_history", None)
    if hist:
        prices = [p for _, p in hist if isinstance(p, (int, float))]
        if prices:
            pi["history_summary"] = {
                "low": min(prices), "high": max(prices),
                "recent": prices[-1], "points": len(prices),
            }
    return pi


def looks_basic(it):
    """Heuristic: does this itinerary look like Basic Economy?
    UNVERIFIED — check against --raw output before relying on it."""
    return "basic economy" in json.dumps(it).lower()


def trim(it):
    legs = it.get("flights", []) or []
    lays = [
        {"airport": lo.get("id"), "minutes": lo.get("duration"),
         "overnight": bool(lo.get("overnight"))}
        for lo in (it.get("layovers") or [])
    ]
    mins = [l["minutes"] for l in lays if l["minutes"] is not None]
    return {
        "price": it.get("price"),
        "stops": max(len(legs) - 1, 0),
        "total_duration_min": it.get("total_duration"),
        "airlines": sorted({l.get("airline") for l in legs if l.get("airline")}),
        "cabin": sorted({l.get("travel_class") for l in legs
                         if l.get("travel_class")}),
        "basic_economy": looks_basic(it),
        "depart": (legs[0].get("departure_airport", {}) or {}).get("time") if legs else None,
        "arrive": (legs[-1].get("arrival_airport", {}) or {}).get("time") if legs else None,
        "min_layover_min": min(mins) if mins else None,
        "max_layover_min": max(mins) if mins else None,
        "layovers": lays,
        "segments": [
            {"from": (l.get("departure_airport", {}) or {}).get("id"),
             "to": (l.get("arrival_airport", {}) or {}).get("id"),
             "airline": l.get("airline"),
             "flight": l.get("flight_number"),
             "minutes": l.get("duration")}
            for l in legs
        ],
        "departure_token": it.get("departure_token"),
    }


def collect(data, limit, max_stops=None, min_layover=None, max_layover=None,
            no_basic=False):
    """Filter, sort by price then duration, slice. Returns (results, dropped)."""
    kept, dropped = [], {"stops": 0, "too_short": 0, "too_long": 0, "basic": 0}
    for it in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        t = trim(it)
        if t["price"] is None:
            continue
        if max_stops is not None and t["stops"] > max_stops:
            dropped["stops"] += 1
            continue
        if no_basic and t["basic_economy"]:
            dropped["basic"] += 1
            continue
        # Nonstop itineraries have no layovers — never filter them on layover.
        if t["min_layover_min"] is not None:
            if min_layover is not None and t["min_layover_min"] < min_layover:
                dropped["too_short"] += 1
                continue
            if max_layover is not None and t["max_layover_min"] > max_layover:
                dropped["too_long"] += 1
                continue
        kept.append(t)
    kept.sort(key=lambda x: (x["price"], x["total_duration_min"] or 0))
    return kept[:limit], dropped


def note_dropped(res, dropped, min_layover, max_layover):
    parts = []
    if dropped["too_short"]:
        parts.append("%d with a layover under %d min"
                     % (dropped["too_short"], min_layover))
    if dropped["too_long"]:
        parts.append("%d with a layover over %d min"
                     % (dropped["too_long"], max_layover))
    if dropped.get("basic"):
        parts.append("%d Basic Economy" % dropped["basic"])
    if parts:
        res["excluded"] = ("Dropped " + " and ".join(parts) +
                           ". Adjust --min-layover / --max-layover / --no-basic "
                           "to see them.")


def save_cache(query, results):
    """Persist departure_tokens so `returns N` can follow up without
    re-running the search."""
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        payload = {
            "saved_at": int(time.time()),
            "query": query,
            "options": [
                {"option": i + 1, "price": r.get("price"),
                 "airlines": r.get("airlines"),
                 "depart": r.get("depart"),
                 "departure_token": r.get("departure_token")}
                for i, r in enumerate(results)
            ],
        }
        with open(CACHE, "w") as f:
            json.dump(payload, f)
        os.chmod(CACHE, 0o600)
    except Exception:
        pass   # cache is a convenience, never fatal


def load_cache():
    try:
        with open(CACHE) as f:
            return json.load(f)
    except Exception:
        die("No cached search. Run `search` first, then `returns N`.")


def fetch_return(q, token):
    """One extra SerpApi search: return legs for a chosen outbound."""
    p = {
        "engine": "google_flights",
        "departure_id": q["from"], "arrival_id": q["to"],
        "outbound_date": q["out"], "return_date": q["ret"],
        "type": "1", "currency": "USD", "hl": "en",
        "adults": q.get("adults", 1),
        "travel_class": q.get("travel_class", 1),
        "departure_token": token,
    }
    if q.get("max_stops") in STOPS_MAP:
        p["stops"] = STOPS_MAP[q["max_stops"]]
    rets, _ = collect(call(p), q.get("return_limit", 3), q.get("max_stops"),
                      q.get("min_layover_min"), q.get("max_layover_min"))
    for r in rets:
        r.pop("departure_token", None)
    return rets


def cmd_search(a):
    tc = resolve_class(a)
    p = {
        "engine": "google_flights",
        "departure_id": a.dep, "arrival_id": a.arr,
        "outbound_date": a.out_date,
        "type": "2" if not a.ret_date else "1",
        "currency": "USD", "hl": "en",
        "adults": a.adults, "travel_class": tc,
        "departure_token": a.departure_token,
    }
    if a.ret_date:
        p["return_date"] = a.ret_date
    if a.max_stops in STOPS_MAP:
        p["stops"] = STOPS_MAP[a.max_stops]
    data = call(p)
    if a.raw:
        return data
    results, dropped = collect(data, a.limit, a.max_stops, a.min_layover,
                               a.max_layover, a.no_basic)
    query = {"from": a.dep, "to": a.arr, "out": a.out_date, "ret": a.ret_date,
             "max_stops": a.max_stops, "min_layover_min": a.min_layover,
             "max_layover_min": a.max_layover, "adults": a.adults,
             "travel_class": tc, "cabin": CLASS_NAME.get(tc)}
    res = {"query": query, "results": results,
           "price_insights": summarize_insights(data)}
    note_dropped(res, dropped, a.min_layover, a.max_layover)

    if a.ret_date and not a.departure_token:
        save_cache(query, results)
        n = min(a.with_returns, len(results))
        for r in results[:n]:
            if r.get("departure_token"):
                r["return_options"] = fetch_return(
                    dict(query, return_limit=a.return_limit),
                    r["departure_token"])
        res["searches_used"] = 1 + n
        if n == 0:
            res["note"] = ("`price` is the TOTAL round-trip fare. These are "
                           "OUTBOUND options only. To see return legs for one, "
                           "run: flights.py returns N   (N = option number, "
                           "1 extra search).")

    # Replace bulky tokens with option numbers before printing.
    for i, r in enumerate(results):
        r.pop("departure_token", None)
        r["option"] = i + 1
    return res


def cmd_returns(a):
    c = load_cache()
    age_min = (int(time.time()) - c.get("saved_at", 0)) // 60
    opts = c.get("options", [])
    match = next((o for o in opts if o["option"] == a.option), None)
    if not match:
        die("Option %d not in cached search (has 1-%d)." % (a.option, len(opts)))
    if not match.get("departure_token"):
        die("Option %d has no departure_token — was it a one-way search?"
            % a.option)
    q = c["query"]
    rets = fetch_return(dict(q, return_limit=a.return_limit),
                        match["departure_token"])
    out = {"for_option": {k: match[k] for k in
                          ("option", "price", "airlines", "depart")},
           "query": q, "return_options": rets, "searches_used": 1,
           "cache_age_min": age_min}
    if age_min > 60:
        out["warning"] = ("Cached search is %d minutes old; fares and tokens "
                          "may be stale. Re-run `search` if this fails."
                          % age_min)
    if not rets:
        out["note"] = ("No return legs passed the layover filters. Re-run "
                       "`search` with looser bounds.")
    return out


def cmd_multicity(a):
    try:
        legs = json.loads(a.legs)
    except Exception as e:
        die("--legs is not valid JSON: %s" % e)
    if not isinstance(legs, list) or not legs:
        die("--legs must be a non-empty JSON array")
    for i, leg in enumerate(legs):
        for f in ("departure_id", "arrival_id", "date"):
            if f not in leg:
                die("leg %d missing '%s'" % (i, f))
    tc = resolve_class(a)
    data = call({
        "engine": "google_flights", "type": "3",
        "multi_city_json": json.dumps(legs),
        "currency": "USD", "hl": "en",
        "adults": a.adults, "travel_class": tc,
    })
    if a.raw:
        return data
    results, dropped = collect(data, a.limit, None, a.min_layover,
                               a.max_layover, a.no_basic)
    for r in results:
        r.pop("departure_token", None)
    res = {"query": {"legs": legs, "min_layover_min": a.min_layover,
                     "max_layover_min": a.max_layover,
                     "travel_class": tc, "cabin": CLASS_NAME.get(tc)},
           "results": results,
           "price_insights": summarize_insights(data)}
    note_dropped(res, dropped, a.min_layover, a.max_layover)
    return res


def cmd_deals(a):
    """Discovery engine. Often EMPTY for a specific city pair — that is
    normal, not an error. Fall back to targeted `search` calls."""
    p = {
        "engine": "google_flights_deals",
        "departure_id": a.dep, "arrival_id": a.arr,
        "outbound_date": a.out_range,
        "type": "2" if not a.ret_range and not a.trip_length else "1",
        "currency": "USD", "hl": "en",
    }
    if a.ret_range:
        p["return_date"] = a.ret_range
    elif a.trip_length:
        p["trip_length"] = a.trip_length
    data = call(p)
    if a.raw:
        return data
    out = {k: v for k, v in data.items()
           if k not in ("search_metadata", "search_parameters",
                        "serpapi_pagination", "price_insights")}
    pi = summarize_insights(data)
    if pi:
        out["price_insights"] = pi
    if not any(out.get(k) for k in ("deals", "best_flights", "other_flights")):
        out["note"] = ("No deals for this route. Expected — `deals` only "
                       "returns routes Google is flagging. Use `search` on "
                       "specific dates instead. Do NOT sweep a date grid.")
    return out


def main():
    ap = argparse.ArgumentParser(description="SerpApi flight lookups")
    ap.add_argument("--raw", action="store_true", help="dump untrimmed JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--limit", type=int, default=3)
        p.add_argument("--adults", type=int, default=1)
        p.add_argument("--class", dest="cabin", default=None,
                       metavar="NAME",
                       help="economy | premium | business | first")
        p.add_argument("--travel-class", dest="travel_class", type=int,
                       default=1, help="numeric alias: 1-4")
        p.add_argument("--no-basic", dest="no_basic", action="store_true",
                       help="drop Basic Economy fares (heuristic)")
        p.add_argument("--min-layover", dest="min_layover", type=int,
                       default=None, metavar="MIN",
                       help="drop layovers shorter than this (nonstops exempt)")
        p.add_argument("--max-layover", dest="max_layover", type=int,
                       default=None, metavar="MIN",
                       help="drop layovers longer than this")

    s = sub.add_parser("search", help="one-way or round-trip, exact dates")
    s.add_argument("dep"); s.add_argument("arr"); s.add_argument("out_date")
    s.add_argument("ret_date", nargs="?", default=None)
    s.add_argument("--max-stops", dest="max_stops", type=int, default=1)
    s.add_argument("--departure-token", dest="departure_token", default=None)
    s.add_argument("--with-returns", dest="with_returns", type=int, default=0,
                   metavar="N", help="fetch return legs for top N now "
                                     "(1 extra search each)")
    s.add_argument("--return-limit", dest="return_limit", type=int, default=3,
                   help="return options to show per outbound (no extra cost)")
    common(s)
    s.set_defaults(fn=cmd_search)

    r = sub.add_parser("returns", help="return legs for option N of last search")
    r.add_argument("option", type=int, help="option number from last search")
    r.add_argument("--return-limit", dest="return_limit", type=int, default=3)
    r.set_defaults(fn=cmd_returns)

    m = sub.add_parser("multicity", help="multi-city itinerary")
    m.add_argument("--legs", required=True,
                   help='JSON: [{"departure_id":"RNO","arrival_id":"ORD","date":"2026-10-14"}, ...]')
    common(m)
    m.set_defaults(fn=cmd_multicity)

    d = sub.add_parser("deals", help="deal discovery — often empty, that's OK")
    d.add_argument("dep"); d.add_argument("arr")
    d.add_argument("out_range", help="YYYY-MM-DD or YYYY-MM-DD,YYYY-MM-DD")
    d.add_argument("--ret-range", dest="ret_range", default=None)
    d.add_argument("--trip-length", dest="trip_length", type=int, default=None,
                   help="1=1wk (default), 2=weekend, 3=2wks")
    d.set_defaults(fn=cmd_deals)

    a = ap.parse_args()
    print(json.dumps(a.fn(a), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
