---
name: flight-search
description: Search flights and fares via SerpApi Google Flights
required_environment_variables:
  - SERPAPI_KEY
---

# Flight search

Use when the user asks about airfares, flight options, best travel dates,
or multi-city routing.

Do NOT use the browser for flight lookups. Google Flights renders fares in
JavaScript, so a scraped page contains no prices, and a browsing session
costs orders of magnitude more tokens than this skill. Use this skill or
report failure — never fall back to browsing.

Run all commands from the skill directory. All output is JSON.

## Hard limits

**Budget: 6 SerpApi searches per user request.** The quota is 250/month.
If answering properly needs more, stop and ask the user to narrow the
window. Do not spend first and report after.

**Never call this script from `execute_code` or a shell loop.** Run the CLI
directly, one invocation per terminal call, so documented defaults apply.
If a task seems to need scripted iteration, it has exceeded the budget —
stop and ask.

**Never sweep a date-pair grid.** N outbound dates x M return dates is N*M
searches. For a flexible round trip: pick at most 3 candidate outbound
dates (prefer Tue/Wed/Sat, usually cheapest), pair each with the single
return date the user most likely wants, and search those 3. Present them
and offer to check other pairings.

**Carry ALL filter flags on every call.** If the user specifies one bound,
keep the default for the other. "Max 2 hours" means
`--min-layover 60 --max-layover 120`, not `--max-layover 120` alone.

## Three rules before searching

**1. Default to round trip.** One-way fares are usually more than half the
round-trip price, so a one-way quote is rarely what the user wants. If the
user gives a departure date but no return, ASK for the return date or trip
length before spending a search. Only search one-way on explicit request.

**2. Always pass `--min-layover 60 --max-layover 300`** unless the user
says otherwise. Google returns both 40-minute connections that will be
missed and 10-hour layovers that are useless. Adjust on request:

- "short layovers are fine" / travelling light -> `--min-layover 45`
- "plenty of buffer" / checked bags -> `--min-layover 90`
- "any layover, just cheapest" -> omit both flags
- International connections -> `--min-layover 90` minimum

Nonstop itineraries are never filtered by these flags.

**3. Flexible dates.** `deals` only returns routes Google is currently
flagging as deals and is often EMPTY for a specific city pair. Try it once
if the user is flexible — it is one cheap search — but if it returns
nothing, that is normal, not an error. Fall back to at most 3 targeted
`search` calls per the budget above. Never expand into a grid.

## Exact dates (1 search)

Round trip — this is the normal case:

    python3 scripts/flights.py search RNO ORD 2026-10-13 2026-10-20 \
      --max-stops 1 --min-layover 60 --max-layover 300

One-way — only on explicit request. Omit the return date:

    python3 scripts/flights.py search RNO ORD 2026-10-13 \
      --min-layover 60 --max-layover 300

Flags: `--max-stops N` (default 1; 0 = nonstop only), `--min-layover MIN`
and `--max-layover MIN` (no defaults — always pass both), `--class NAME`,
`--no-basic`, `--limit N` (default 3), `--adults N`.

### The price is already the full round-trip fare

Verified against live output. The `price` field on a round-trip search is
the TOTAL cost of the trip, not the outbound half. Answer "what does this
cost" directly from it. Never spend an extra search to price a trip.

### Cabin class

Pass `--class economy | premium | business | first`. Default is economy.

    python3 scripts/flights.py search RNO ORD 2026-10-13 2026-10-20 \
      --class business --max-stops 1 --min-layover 60 --max-layover 300

Basic Economy is NOT a class — it is a fare brand inside Economy. To
exclude it, add `--no-basic`. Each result carries a `basic_economy`
boolean; mention it when true, since those fares usually have no seat
selection, no changes, and no carry-on.

Business and first class on smaller routes may return few or no results
under tight layover filters. If a search comes back empty in business, say
so and suggest loosening `--max-stops` or the layover bounds rather than
silently falling back to economy.

### Return legs — follow-up, not default

Do NOT pass `--with-returns` on a first search. `price` is already the
total round-trip fare, so returns are not needed to answer what a trip
costs.

Each result carries an `option` number. When the user asks about the return
for a specific one — "show me the returns on option 2" — run:

    python3 scripts/flights.py returns 2

That reads the cached search and costs 1 SerpApi search. Do NOT re-run
`search` first. If the output warns the cache is stale, or errors that no
cached search exists, re-run the original `search` and try again.

A round-trip `search` returns OUTBOUND options only. Return legs are not in
the response, so a bad return connection cannot be filtered or even seen
until fetched. When the user cares about connection times, say plainly that
the return has not been checked yet, and offer to fetch it.

## Sweep a window (1 search, often empty)

    python3 scripts/flights.py deals RNO ORD 2026-10-10,2026-10-23
    python3 scripts/flights.py deals RNO ORD 2026-10-10,2026-10-14 --ret-range 2026-10-18,2026-10-22
    python3 scripts/flights.py deals RNO ORD 2026-10-01,2026-10-31 --trip-length 1

`--trip-length`: 1 = 1 week (default), 2 = weekend, 3 = 2 weeks.

A bare `deals` with no return flag is a ONE-WAY sweep. Layover and class
flags do not apply — filter when you follow up with `search`.

## Multi-city (1 search)

    python3 scripts/flights.py multicity --min-layover 60 --max-layover 300 --legs '[
      {"departure_id":"RNO","arrival_id":"ORD","date":"2026-10-14"},
      {"departure_id":"ORD","arrival_id":"JFK","date":"2026-10-18"},
      {"departure_id":"JFK","arrival_id":"RNO","date":"2026-10-22"}
    ]'

Every leg needs `departure_id`, `arrival_id`, and `date`, in chronological
order. `--max-stops` does not apply to multi-city. `--class` does.

## Output format

Always present results as a markdown table, cheapest first, followed by
notes. Do not narrate the itineraries in prose.

| # | Price | Airline | Depart | Arrive | Via | Layover | Total |
|---|-------|---------|--------|--------|-----|---------|-------|
| 1 | $370 | Alaska | Tue 10/14 07:30 | 18:53 | SAN | 3h25 | 9h23 |
| 2 | $389 | Frontier | Mon 10/12 21:18 | 05:16 +1 | LAS | 0h45 ⚠ | 5h58 |
| 3 | $401 | Alaska | Wed 10/13 07:30 | 18:53 | SAN | 3h25 | 9h23 |

Column rules:

- **#** — the `option` number. The user refers to these in follow-ups.
- **Price** — from `price`. For round trips this is the TOTAL fare.
- **Depart** — weekday, M/DD, and time, from `depart`.
- **Arrive** — time only. Append ` +1` if the date in `arrive` is later
  than the date in `depart`.
- **Via** — layover airport codes, comma-separated if more than one.
  Write `nonstop` when there are none.
- **Layover** — `min_layover_min` as HhMM. Append ⚠ if under 60, or 🌙 if
  any layover has `overnight: true`.
- **Total** — `total_duration_min` as HhMM.

Add a **Class** column only when not economy. Add a **Legs** column for
multi-city, showing the route as `RNO→ORD→JFK→RNO`.

When showing return legs from `returns N`, use the same table with a
**Leg** column, `out` and `ret` rows. Price goes on the outbound row only,
since it is the round-trip total.

### After the table

A short `**Notes:**` bullet list — at most three bullets — covering only
what the table cannot show:

- Why the cheapest may not be the best pick (time cost, tight connection)
- Red-eyes, overnight layovers, Basic Economy restrictions
- A better option just outside the top 3, if one exists in the results

Then one line of price context from `price_insights`, one line for
`excluded` if present, and — for round trips without returns fetched — one
line offering `returns N`.

Legend line only if a ⚠ or 🌙 appears: `⚠ tight connection · 🌙 overnight`

### Report only what is in the JSON

Every number in the table must come from the tool output. Do not compute
"each way" durations or return-flight times from a round-trip `search` —
that response contains OUTBOUND legs only. Never carry figures over from an
earlier search on different dates.

## price_insights

Use it to tell the user whether the fare is actually good:

- `price_level` — "low", "typical", or "high"
- `typical_price_range` — [min, max] for this route
- `lowest_price` — cheapest currently available
- `history_summary` — {low, high, recent, points} over the tracked period

Say something like: "$401 is high for this route, which typically runs
$195-365 and has been as low as $230 recently."

History shows how this itinerary's fare has moved over time, NOT which
departure date is cheapest.

## Caching and failures

Identical calls within one hour are served from SerpApi's cache and do not
count against quota. Re-running a query to re-read output is free; changing
any parameter is not.

Every result includes `searches_used` where relevant. Report the running
total if a request approaches the 6-search budget.

On `{"error": ...}`: a quota message means the monthly allowance is gone —
say so and stop, do not retry. Any other error, report it verbatim and do
not fall back to browsing.
