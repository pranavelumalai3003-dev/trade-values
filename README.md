# Fantasy Football Trade Values (weekly-updating)

A small Python pipeline that produces trade values for every fantasy-relevant NFL
player and refreshes itself every week of the season. A trade-calculator website
reads the output file, `data/values_latest.json`.

**Design in one sentence:** the *market* (values built from real fantasy trades) is the
backbone, and my own *stats model* is a second opinion used to flag players the market
may be mis-pricing.

---

## Quick start (Windows)

```powershell
cd trade-values
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe build_values.py
```

That writes `data/values_latest.json` and a snapshot in `data/history/`.

Other scripts (each can be run on its own):

| Command | What it does |
|---|---|
| `python config.py` | Shows detected season / week and league settings |
| `python fetch_market.py` | Previews FantasyCalc market values |
| `python fetch_players.py` | Previews Sleeper player info |
| `python build_model_value.py` | Shows the stats model's top players by position |
| `python build_values.py` | **Main pipeline** - writes the output files |
| `python build_values.py --force` | Same, but runs even in the offseason |
| `python evaluate_trade.py "Player A" "Player B, Player C"` | Judges a trade |
| `python backtest.py` | Accuracy check (see below) |

League settings live in `config.py` (or set environment variables `NUM_TEAMS`, `PPR`,
`SUPERFLEX`). Defaults: 12 teams, full PPR, 1-QB.

## Files

```
trade-values/
  config.py              league settings, file paths, season/week detection
  utils.py               downloads with retries + caching, safe file writes, name matching
  fetch_market.py        FantasyCalc market values
  fetch_players.py       Sleeper player info (cached 24h)
  build_model_value.py   my stats-based value
  build_values.py        merges everything -> data/values_latest.json
  evaluate_trade.py      evaluate_trade(side_a_ids, side_b_ids)
  backtest.py            accuracy check
  data/
    values_latest.json           <- the website reads this
    history/values_YYYY_wWW.json <- one snapshot per week
    unmatched_players.csv        <- every player that couldn't be matched (check this!)
    cache/                       <- big downloads, not committed
  .github/workflows/weekly.yml   automatic refresh
```

---

## Output format

```json
{
  "generated_at": "2026-11-22T01:33:49+00:00",
  "season": 2026,
  "week": 10,
  "settings": {"num_teams": 12, "ppr": 1.0, "superflex": false},
  "sources": ["FantasyCalc (market values)", "Sleeper (player info)", "nflverse (stats)"],
  "players": [
    {
      "id": "9221",
      "name": "Jahmyr Gibbs",
      "position": "RB",
      "team": "DET",
      "age": 24,
      "injury_status": null,
      "market_value": 10000,
      "model_value": 10000,
      "trade_value": 10000,
      "value_change_1wk": null,
      "market_vs_model": "aligned",
      "nflverse_id": "00-0039139"
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `id` | Sleeper player id (stable; use it as the key in your website) |
| `week` | Stats are complete **through** this week. Tuesday and Thursday runs of the same week overwrite the same snapshot. |
| `market_value` | FantasyCalc value rescaled so the #1 player = 10,000 |
| `trade_value` | What the calculator should use. Equals `market_value`. |
| `model_value` | My stats model, put on the same 0-10,000 curve (see below). `null` if the player has no stats this season. |
| `value_change_1wk` | `trade_value` minus last week's snapshot (`null` the first week or for new players) |
| `market_vs_model` | `undervalued_by_market`, `overvalued_by_market`, `aligned`, or `null` (no model value) |
| `injury_status` | From Sleeper: `null`, `Questionable`, `Doubtful`, `Out`, `IR`, `PUP`, ... |
| `nflverse_id` | Link to nflverse stats; used by `backtest.py` |

A player is flagged as mis-valued when the model is at least **25% and 400 points**
above or below the market.

---

## How the values are built

### 1. Market value (the backbone)
FantasyCalc publishes values computed from real trades in real leagues, adjusted for
league size, PPR and superflex. We request redraft values for your settings and rescale
them so the top player is always 10,000. (Because the top is fixed at 10,000, if the #1
player's value drops, everyone else's number rises slightly. Week-to-week changes are
relative to the best player.)

### 2. My model value (the second opinion)
For every QB/RB/WR/TE with stats this season (`build_model_value.py`):

1. **Form**: `form_ppg = 30% x last-4-games points/game + 70% x season points/game`
2. **Usage**: `expected_ppg` = what an *average* player at the position would score
   with this player's last-4-games workload (league-average points per pass attempt,
   per carry, and per target). This rewards role, not lucky touchdowns.
   `projected_ppg = (1 - w) x form_ppg + w x expected_ppg`, where `w` = 0 for QB,
   0.35 for RB/WR, 0.5 for TE.
3. **Role trend**: if a player's recent snap share is up compared with his season
   average, nudge him up (and vice versa), capped at plus or minus 10%. Small samples
   are pulled toward replacement level (as if he'd played 2 extra replacement-level games).
4. **Value over replacement (VOR)**: subtract the points/game of a replacement-level
   player (12-team league: QB12, RB30, WR30, TE12). This is how positional
   scarcity enters the model.
5. **Availability**: share of his team's games he has played so far.
6. **Schedule and injuries**: multiply by his team's remaining games through week 17
   (real bye weeks from the schedule) x availability, minus games expected to be
   missed because of his current injury status (Questionable 0.25, Doubtful 0.75,
   Out 1, IR/PUP 4).

Result: *rest-of-season fantasy points above replacement*. To compare it with the
market, the model's ranking is mapped onto the market's value curve: the model's #1
player gets the market's #1 value, its #2 gets the market's #2 value, and so on.
The model decides the order and the market decides the spacing.

**What the backtest taught me:** my first version weighted recent games at 60% and used
pass attempts for QBs. The backtest showed that recent games are noisier than the full
season, that QB pass attempts barely predict QB scoring (correlation ~0.03), and that
players who have missed games tend to keep missing them. The current weights fix those.
Note that they were chosen using the same 2022-2024 data the backtest measures, so the
model's backtest score is slightly flattering.

### 3. Matching players across sources
Each source names players differently. In order, we try: FantasyCalc's ESPN id ->
nflverse's ESPN id; Sleeper's gsis id; normalized name + birth date; normalized name +
position + team. "Normalized" means lowercase, no punctuation, no suffixes (Jr., III),
plus a small nickname list in `utils.py`. A key that matches more than one player is never
used. **Every player that can't be matched is written to `data/unmatched_players.csv`**,
along with up to 25 players the model rates highly but the market doesn't list (often
injured players or waiver pickups).

---

## Trade evaluation (consolidation adjustment)

`evaluate_trade(side_a_ids, side_b_ids)` (in `evaluate_trade.py`) does **not** just add up
values, because in fantasy one star is worth more than two mid players whose values add up
to the same number: you have limited starting spots, and the second player often ends up
on your bench.

**Formula (a "power sum"):**

```
package value = ( v1^1.4 + v2^1.4 + ... + vn^1.4 ) ^ (1 / 1.4)
```

| Package | Plain sum | Package value |
|---|---|---|
| one 10,000 player | 10,000 | **10,000** |
| two 5,000 players | 10,000 | **8,203** |
| 8,000 + 2,000 | 10,000 | **8,805** |
| four 2,500 players | 10,000 | **6,730** |
| 7,000 + a 500 throw-in | 7,500 | **7,124** |

Properties worth mentioning in an essay:
* A 1-for-1 trade is unchanged (one player's package value is his value).
* Adding a player always helps a little, but less than his raw value. Throw-ins don't
  balance a trade.
* The exponent (1.4) controls how much stars matter: 1.0 = plain sum, higher = more
  star-heavy. It's a judgment call, not fitted from data.

**Verdict:** if the two package values are within 5% it's "fair"; within 15% it
"slightly favors" one side; beyond that it "favors" one side. It also reports how much
value the short side would need to add to even it out (under the same formula).

The same formula in JavaScript, for your website:

```js
const K = 1.4;
const packageValue = vals => Math.pow(vals.reduce((s, v) => s + Math.pow(v, K), 0), 1 / K);
```

---

## Accuracy check (`backtest.py`)

**Question:** if you rank players by value at week N, does that order match who scores
the most fantasy points over the rest of the season (weeks N+1 to 17)?
**Metric:** Spearman rank correlation by position (1 = perfect order, 0 = random), among the
relevant players at each position (top 24 QB / 48 RB / 60 WR / 24 TE by market *or* by model).

### Results: 2022-2024 seasons, checkpoints at weeks 4, 6, 8, 10, 12 (15 tests per position)

| Position | Market (proxy) | My model | 50/50 blend | Baseline: season pts/game |
|---|---|---|---|---|
| QB  | **0.572** | 0.359 | 0.514 | 0.372 |
| RB  | **0.716** | 0.546 | 0.670 | 0.535 |
| WR  | **0.654** | 0.381 | 0.568 | 0.414 |
| TE  | **0.464** | 0.250 | 0.383 | 0.219 |
| ALL | **0.601** | 0.384 | 0.534 | 0.385 |

**Honest reading:**
* The market is clearly the best predictor, which supports the design of making it the
  backbone of `trade_value`.
* My model is about as good as plain points-per-game overall. It's better for RB and TE,
  slightly worse for QB and WR. It does **not** beat the market, and a 50/50 blend is worse
  than the market alone.
* So treat `market_vs_model` flags as **questions worth investigating, not predictions**.
  The market knows things a stats model can't see (injury details, coaching changes, depth
  charts, upcoming schedule strength).

### What the backtest can and cannot show
* **FantasyCalc has no public history**, so the 2022-2024 "market" is a **proxy**: the
  FantasyPros rest-of-season PPR expert consensus rankings, archived weekly by
  DynastyProcess. Expert rankings are not the same as trade prices.
* **Part 2 of the backtest uses the real FantasyCalc values** saved by this pipeline in
  `data/history/`. It needs 3 finished weeks after a snapshot, so it fills in as the
  2026 season goes on (the week-10 snapshot can be scored after week 13). Re-run
  `python backtest.py` later in the season to get real-market numbers.
* The historical model has no injury reports (Sleeper doesn't keep history), so the
  live model has slightly more information than the backtested one.
* The model's weights were tuned on the same seasons it is tested on (see above).
* Each position has only 15 checkpoints across 3 seasons, so small differences
  (e.g. 0.37 vs 0.38) are noise.
* The proxy rankings are full-PPR. Changing `PPR` mostly affects the model, not the proxy.

---

## Weekly automation (GitHub Actions)

`.github/workflows/weekly.yml` runs:
* **Every Tuesday 15:00 UTC**: after Monday Night Football, once stats are final.
* **Every Thursday 15:00 UTC**: picks up injury news.
* **On demand**: click "Run workflow" on the repo's Actions tab (tick "force" to run in the
  offseason).

Each run installs dependencies, runs `build_values.py`, and commits the new
`values_latest.json`, the history snapshot and the unmatched-players log back to the repo.
If anything fails, the run turns red, GitHub emails you, and the previous file stays in
place. In the offseason (more than 2 days after the last regular-season game, until 7 days
before week 1) the script exits without changes.

If nflverse hasn't finished loading Monday night's stats yet, the pipeline notices that
the week is incomplete and uses the previous complete week. The Thursday run then catches up.

**One-time setup:**
1. Create a new GitHub repository and push this folder to it.
2. In the repo: **Settings -> Actions -> General -> Workflow permissions** -> choose
   **Read and write permissions** (so the job can commit).
3. Optionally run it once from the Actions tab to check it works.

Note: GitHub pauses scheduled workflows in repos with no activity for 60 days. The
weekly commits count as activity during the season. After a long offseason you may
need to re-enable the workflow on the Actions tab.

---

## How the website reads the values

Once the repo is on GitHub, the file is available at:

```
https://raw.githubusercontent.com/<your-username>/<repo-name>/main/data/values_latest.json
```

```js
const URL = "https://raw.githubusercontent.com/<your-username>/<repo-name>/main/data/values_latest.json";
const data = await fetch(URL).then(r => r.json());
const byId = Object.fromEntries(data.players.map(p => [p.id, p]));
// e.g. byId["9221"].trade_value  -> 10000
```

GitHub caches raw files for about 5 minutes, which is fine for weekly data. If your
website lives in the same repo (or uses a build step), you can also just copy the file in.

---

## Data sources and attribution

* **FantasyCalc** (`api.fantasycalc.com`): market trade values built from real fantasy
  trades. Public API, called once per run.
* **Sleeper** (`api.sleeper.app`): player info and injury status. Public API, no key
  needed. The large players file is cached and fetched at most once a day, as Sleeper asks.
* **nflverse** (`github.com/nflverse/nflverse-data`): weekly player stats, snap counts,
  schedules, and player ID mappings, from the nflverse project (see their repository
  for license terms).
* **DynastyProcess** (`github.com/dynastyprocess/data`): archived FantasyPros expert
  consensus rankings and player ID mappings. **Used only by `backtest.py`.** The rankings
  themselves are FantasyPros content.

No API keys or secrets are needed. No websites are scraped. All requests use timeouts,
retries with back-off, and an honest User-Agent.

**Note on `nfl_data_py`:** the original plan called for this package, but nflverse has
retired it. Its weekly-stats download points to a file that no longer exists for
2026. This project reads the same nflverse files directly with pandas, which also keeps
dependencies down to just `requests` and `pandas`.
