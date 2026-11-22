"""
backtest.py - do the values actually predict the future?

Question: if you rank players by value at week N, does that order match who scores
the most fantasy points over the REST of the season (weeks N+1 to 17)?

Metric: Spearman rank correlation, computed separately for QB / RB / WR / TE.
  1.0 = perfect order, 0 = no better than random.

We compare:
  (a) market   - the market's opinion at week N
  (b) model    - my stats model, rebuilt using ONLY data available at week N
  (c) blend    - average of (a) and (b) percentile ranks
  baseline     - plain "season points per game so far" (what a casual fan would use)

IMPORTANT - what the "market" is in each test:
  * PART 1 (2022-2024 seasons): FantasyCalc has no public history, so the market stand-in
    is the FantasyPros rest-of-season PPR expert consensus ranking, as archived weekly by
    DynastyProcess (github.com/dynastyprocess/data). Experts are not trades, so this is a
    proxy for "what the fantasy world believed", not the real trade market.
  * PART 2 (our own snapshots): uses the REAL FantasyCalc values saved in data/history/
    by the weekly job. It needs at least 3 completed weeks after a snapshot, so it fills in
    as the season goes on.

Usage:  python backtest.py
"""

import numpy as np
import pandas as pd

from build_model_value import _points, compute_model, load_nflverse_players, load_snap_counts, load_weekly_stats
from config import FANTASY_LAST_WEEK, HISTORY_DIR, SETTINGS, SKILL_POSITIONS, load_schedule
from utils import normalize_name, read_csv_url, read_json

SEASONS = [2022, 2023, 2024]          # seasons with archived weekly expert rankings
CHECK_WEEKS = [4, 6, 8, 10, 12]       # "value at week N" checkpoints
MIN_FUTURE_WEEKS = 3                  # snapshot test needs this many finished weeks after N

# Which players count as "relevant" at each position (top N by market OR by model).
UNIVERSE = {"QB": 24, "RB": 48, "WR": 60, "TE": 24}

ECR_URL = "https://github.com/dynastyprocess/data/raw/master/files/db_fpecr.csv.gz"
IDS_URL = "https://github.com/dynastyprocess/data/raw/master/files/db_playerids.csv"
ROS_PAGE = "/nfl/rankings/ros-ppr-overall.php"


# ---------------------------------------------------------------------------
# Shared scoring helpers
# ---------------------------------------------------------------------------

def pct_rank(series):
    return series.rank(pct=True)


def spearman(score, actual):
    if len(score) < 5 or score.nunique() < 2:
        return np.nan
    return score.corr(actual, method="spearman")


def ros_points(stats, after_week, through_week):
    s = stats[(stats["week"] > after_week) & (stats["week"] <= through_week)]
    return _points(s, SETTINGS["ppr"]).groupby("player_id")["pts"].sum()


def score_universe(df):
    """df has columns: position, market, model, baseline, actual. Returns one row per position."""
    rows = []
    for pos in SKILL_POSITIONS:
        d = df[df["position"] == pos].copy()
        if d.empty:
            continue
        # players missing a score go to the bottom of that ranking
        d["market"] = d["market"].fillna(d["market"].min() - 1)
        d["model"] = d["model"].fillna(d["model"].min() - 1)
        d["baseline"] = d["baseline"].fillna(0)
        d["actual"] = d["actual"].fillna(0)      # didn't play = scored 0 (that's real!)
        d["blend"] = (pct_rank(d["market"]) + pct_rank(d["model"])) / 2
        rows.append({"position": pos, "n": len(d),
                     **{k: spearman(d[k], d["actual"]) for k in ["market", "model", "blend", "baseline"]}})
    return rows


def print_table(results, title):
    df = pd.DataFrame(results)
    print(f"\n{title}")
    print("-" * len(title))
    if df.empty:
        print("  (no data yet)")
        return
    table = df.groupby("position")[["market", "model", "blend", "baseline"]].mean()
    table = table.reindex([p for p in SKILL_POSITIONS if p in table.index])
    table.loc["ALL"] = table.mean()
    table["tests"] = df.groupby("position").size().reindex(table.index).fillna(len(df))
    print(table.round(3).to_string())
    best = table.loc["ALL", ["market", "model", "blend"]].astype(float).idxmax()
    print(f"\n  Best overall: {best}.  (Spearman: 1 = perfect ranking, 0 = random)")


# ---------------------------------------------------------------------------
# PART 1: past seasons vs. expert-consensus market proxy
# ---------------------------------------------------------------------------

def load_ecr_history():
    ecr = read_csv_url(ECR_URL, "db_fpecr.csv.gz", max_age_hours=24 * 7,
                       usecols=["fp_page", "player", "id", "pos", "team", "ecr", "scrape_date"])
    ecr = ecr[(ecr["fp_page"] == ROS_PAGE) & ecr["pos"].isin(SKILL_POSITIONS)].copy()
    ecr["scrape_date"] = pd.to_datetime(ecr["scrape_date"]).dt.date
    ids = read_csv_url(IDS_URL, "db_playerids.csv", max_age_hours=24 * 7, usecols=["fantasypros_id", "gsis_id"])
    ids = ids.dropna()
    ids["fantasypros_id"] = pd.to_numeric(ids["fantasypros_id"], errors="coerce").astype("Int64").astype("string")
    fp_to_gsis = ids.drop_duplicates("fantasypros_id").set_index("fantasypros_id")["gsis_id"]
    ecr["fp_id"] = pd.to_numeric(ecr["id"], errors="coerce").astype("Int64").astype("string")
    ecr["gsis_id"] = ecr["fp_id"].map(fp_to_gsis)
    ecr["name_key"] = ecr["player"].map(normalize_name)
    return ecr


def backtest_past_seasons():
    print("Loading archived expert rankings (first run downloads ~45 MB)...")
    ecr = load_ecr_history()
    schedule = load_schedule()
    nfl_players = load_nflverse_players()
    results, unmatched_total = [], 0

    for season in SEASONS:
        print(f"  season {season}...")
        stats = load_weekly_stats(season)
        snaps = load_snap_counts(season)
        sched = schedule[(schedule["season"] == season) & (schedule["game_type"] == "REG")]
        # fallback name matching for experts' players without an id link
        name_pos_to_gsis = (stats.assign(name_key=stats["player_display_name"].map(normalize_name))
                            .drop_duplicates(["name_key", "position", "player_id"])
                            .drop_duplicates(["name_key", "position"], keep=False)
                            .set_index(["name_key", "position"])["player_id"].to_dict())

        for week in CHECK_WEEKS:
            week_end = sched.loc[sched["week"] == week, "gameday"].max()
            dates = sorted(d for d in ecr["scrape_date"].unique() if week_end < d <= week_end + pd.Timedelta(days=7))
            if not dates:
                print(f"    week {week}: no expert snapshot within a week - skipped")
                continue
            snap = ecr[ecr["scrape_date"] == dates[0]].copy()
            missing = snap["gsis_id"].isna()
            snap.loc[missing, "gsis_id"] = [name_pos_to_gsis.get((k, p)) for k, p in
                                            zip(snap.loc[missing, "name_key"], snap.loc[missing, "pos"])]
            unmatched_total += int(snap["gsis_id"].isna().sum())
            snap = snap.dropna(subset=["gsis_id"]).drop_duplicates("gsis_id")

            model = compute_model(stats, snaps, nfl_players, sched, week, SETTINGS)
            actual = ros_points(stats, week, FANTASY_LAST_WEEK)

            universe = set()
            for pos, n in UNIVERSE.items():
                universe |= set(snap[snap["pos"] == pos].nsmallest(n, "ecr")["gsis_id"])
                universe |= set(model[model["position"] == pos].nlargest(n, "model_score")["gsis_id"])

            pos_lookup = {**dict(zip(model["gsis_id"], model["position"])), **dict(zip(snap["gsis_id"], snap["pos"]))}
            df = pd.DataFrame({"gsis_id": sorted(universe)})
            df["position"] = df["gsis_id"].map(pos_lookup)
            df["market"] = -df["gsis_id"].map(snap.set_index("gsis_id")["ecr"])   # rank 1 = best -> negate
            df["model"] = df["gsis_id"].map(model.set_index("gsis_id")["model_score"])
            df["baseline"] = df["gsis_id"].map(model.set_index("gsis_id")["season_ppg"])
            df["actual"] = df["gsis_id"].map(actual)
            for row in score_universe(df):
                results.append({"season": season, "week": week, **row})

    print(f"  ({unmatched_total} expert-ranking rows could not be linked to stats and were skipped)")
    return results


# ---------------------------------------------------------------------------
# PART 2: our own weekly snapshots (real FantasyCalc market values)
# ---------------------------------------------------------------------------

def backtest_snapshots():
    results, waiting = [], []
    stats_cache = {}
    for path in sorted(HISTORY_DIR.glob("values_*_w*.json")):
        snap = read_json(path)
        season, week = snap["season"], snap["week"]
        if season not in stats_cache:
            stats_cache[season] = load_weekly_stats(season)
        stats = stats_cache[season]
        through = min(FANTASY_LAST_WEEK, int(stats["week"].max()))
        if through - week < MIN_FUTURE_WEEKS:
            waiting.append(f"{path.name} (needs weeks {week + 1}-{week + MIN_FUTURE_WEEKS} to finish)")
            continue
        actual = ros_points(stats, week, through)
        ppg = _points(stats[stats["week"] <= week], SETTINGS["ppr"]).groupby("player_id")["pts"].mean()
        df = pd.DataFrame([p for p in snap["players"] if p.get("nflverse_id")])
        df = df.rename(columns={"market_value": "market", "model_value": "model"})
        df["baseline"] = df["nflverse_id"].map(ppg)
        df["actual"] = df["nflverse_id"].map(actual)
        for row in score_universe(df):
            results.append({"season": season, "week": week, **row})
    return results, waiting


if __name__ == "__main__":
    past = backtest_past_seasons()
    print_table(past, f"PART 1 - seasons {SEASONS[0]}-{SEASONS[-1]}, weeks {CHECK_WEEKS}: "
                      "market = expert consensus (proxy)")

    snaps, waiting = backtest_snapshots()
    print_table(snaps, "PART 2 - our saved snapshots: market = real FantasyCalc values")
    if waiting:
        print("  Snapshots still waiting for future games:")
        for w in waiting:
            print("   -", w)
