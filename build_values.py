"""
build_values.py - THE MAIN SCRIPT. Merges everything into data/values_latest.json.

Steps:
  1. Detect season/week (skip in the offseason).
  2. Fetch market values (FantasyCalc) and player info (Sleeper).
  3. Build my stats model (nflverse).
  4. Match players across the three sources (IDs first, then names) and log misses.
  5. Put market + model on the same 0-10,000 scale, compare them, compute weekly change.
  6. Validate, then write values_latest.json + a history snapshot.

If anything fails, the script exits with an error and the previous files are untouched.

Usage:
  python build_values.py            # normal weekly run
  python build_values.py --force    # run even in the offseason (for testing)
"""

import sys
from datetime import datetime, timezone

import pandas as pd

from build_model_value import apply_injuries, build_model_values
from config import HISTORY_DIR, LATEST_FILE, SETTINGS, UNMATCHED_FILE, detect_season
from fetch_market import fetch_market_values
from fetch_players import fetch_sleeper_players
from utils import normalize_name, normalize_team, read_json, write_json_atomic

SCALE_MAX = 10000  # top player = 10,000 every week

# How far apart market and model must be to call a player mis-valued:
DIVERGENCE_PCT = 0.25   # model at least 25% above/below market...
DIVERGENCE_MIN = 400    # ...and at least 400 points apart (avoids noise at the bottom)


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _unique_lookup(df, key_cols, value_col):
    """Build {key: value}, but only for keys that point to exactly ONE player.

    If two players share a key (e.g. two "Mike Williams" WRs), we refuse to guess.
    """
    keyed = df.dropna(subset=key_cols + [value_col])
    keys = keyed[key_cols].astype(str).agg("|".join, axis=1)
    counts = keys.value_counts()
    good = keys.map(counts) == 1
    return dict(zip(keys[good], keyed.loc[good, value_col]))


def _key(df, cols):
    return df[cols].astype(str).agg("|".join, axis=1)


def match_market_to_model(market, model, nfl_players):
    """Find the nflverse gsis_id (the model's key) for each market player.

    Tries, in order: ESPN id -> Sleeper's gsis id -> name + birth date -> name + position + team.
    Returns a Series of gsis_ids (None where no match) and a Series saying which method worked.
    """
    espn_to_gsis = _unique_lookup(nfl_players, ["espn_id"], "gsis_id")
    nfl = nfl_players.assign(name_key=nfl_players["display_name"].map(normalize_name))
    name_bday_to_gsis = _unique_lookup(nfl, ["name_key", "birth_date"], "gsis_id")
    name_pos_team_to_gsis = _unique_lookup(model, ["name_key", "position", "team"], "gsis_id")

    gsis, method = [], []
    for _, row in market.iterrows():
        candidates = [
            ("espn_id", espn_to_gsis.get(str(row["espn_id"]))),
            ("sleeper_gsis_id", row.get("sleeper_gsis_id")),
            ("name+birthdate", name_bday_to_gsis.get(f"{row['name_key']}|{row['birth_date']}")),
            ("name+pos+team", name_pos_team_to_gsis.get(f"{row['name_key']}|{row['position']}|{row['team_norm']}")),
        ]
        found = next(((m, g) for m, g in candidates if isinstance(g, str) and g), (None, None))
        method.append(found[0])
        gsis.append(found[1])
    return pd.Series(gsis, index=market.index), pd.Series(method, index=market.index)


def match_model_to_sleeper_injuries(model, sleeper):
    """{gsis_id: Sleeper injury status} for every model player we can match."""
    by_gsis = sleeper.dropna(subset=["sleeper_gsis_id"]).set_index("sleeper_gsis_id")["injury_status"].to_dict()
    by_name_pos_team = _unique_lookup(sleeper, ["name_key", "sleeper_position", "sleeper_team"], "sleeper_id")
    by_name_pos = _unique_lookup(sleeper, ["name_key", "sleeper_position"], "sleeper_id")
    status_by_sleeper = sleeper.set_index("sleeper_id")["injury_status"].to_dict()

    result = {}
    for _, row in model.iterrows():
        if row["gsis_id"] in by_gsis:
            result[row["gsis_id"]] = by_gsis[row["gsis_id"]]
            continue
        sid = (by_name_pos_team.get(f"{row['name_key']}|{row['position']}|{row['team']}")
               or by_name_pos.get(f"{row['name_key']}|{row['position']}"))
        if sid:
            result[row["gsis_id"]] = status_by_sleeper.get(sid)
    return result


# ---------------------------------------------------------------------------
# Scaling + comparison
# ---------------------------------------------------------------------------

def scale_model_to_market(model_scores, market_values):
    """Give the model the SAME value curve as the market ("rank-for-rank" mapping).

    The model's #1 player gets the market's #1 value, the model's #2 gets the market's
    #2 value, and so on. The model decides the ORDER; the market decides how steeply
    value drops from rank to rank. Players ranked below the market's list get a value
    that keeps shrinking (last market value x N / rank).
    """
    curve = sorted(market_values, reverse=True)
    n = len(curve)
    ranks = model_scores.rank(ascending=False, method="first").astype(int)
    return ranks.map(lambda r: curve[r - 1] if r <= n else curve[-1] * n / r)


def classify(market_value, model_value):
    if pd.isna(model_value):
        return None  # no stats this season (e.g. hurt since week 1, or hasn't played)
    diff = model_value - market_value
    rel = diff / max(market_value, 1)
    if diff >= DIVERGENCE_MIN and rel >= DIVERGENCE_PCT:
        return "undervalued_by_market"
    if diff <= -DIVERGENCE_MIN and rel <= -DIVERGENCE_PCT:
        return "overvalued_by_market"
    return "aligned"


def previous_snapshot(season, week):
    """Most recent history snapshot from an EARLIER week (or None)."""
    files = sorted(HISTORY_DIR.glob("values_*_w*.json"))
    older = [f for f in files if f.stem < f"values_{season}_w{week:02d}"]
    return read_json(older[-1]) if older else None


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def build(force=False):
    print("1/6 Detecting season and week...")
    info = detect_season()
    print(f"    season {info.season}, last completed week {info.last_completed_week}")
    if not info.in_season and not force:
        print("    Offseason - nothing to update. (Use --force to run anyway.)")
        return None

    print("2/6 Fetching market values (FantasyCalc) and player info (Sleeper)...")
    market = fetch_market_values(SETTINGS)
    sleeper = fetch_sleeper_players()
    print(f"    {len(market)} market players, {len(sleeper)} Sleeper players")

    print("3/6 Building stats model (nflverse)...")
    model, week, nfl_players = build_model_values(info, SETTINGS)
    print(f"    {len(model)} players with stats through week {week}")

    print("4/6 Matching players across sources...")
    unmatched = []
    # Market -> Sleeper (FantasyCalc gives us the Sleeper id directly)
    market = market.merge(sleeper, on="sleeper_id", how="left")
    for _, r in market[market["sleeper_name"].isna()].iterrows():
        unmatched.append({"source": "fantasycalc", "name": r["name"], "position": r["position"],
                          "team": r["team"], "problem": "sleeper_id not found in Sleeper"})
    market["name_key"] = market["name"].map(normalize_name)
    market["team_norm"] = market["sleeper_team"].fillna(market["team"]).map(normalize_team)

    # Injuries (from Sleeper) feed into the model before we rank it
    if not model.empty:
        model = apply_injuries(model, match_model_to_sleeper_injuries(model, sleeper))

    # Market -> model
    market["gsis_id"], market["match_method"] = match_market_to_model(market, model, nfl_players)
    market["gsis_id"] = market["gsis_id"].where(market["gsis_id"].isin(set(model["gsis_id"])))
    for _, r in market[market["gsis_id"].isna()].iterrows():
        unmatched.append({"source": "fantasycalc", "name": r["name"], "position": r["position"],
                          "team": r["team_norm"], "problem": "no nflverse stats match (no model value)"})
    print("    match methods:", market["match_method"].value_counts(dropna=False).to_dict())

    print("5/6 Scaling values and comparing market vs. model...")
    top = market["market_value_raw"].max()
    market["market_value"] = (market["market_value_raw"] / top * SCALE_MAX).round().astype(int)

    if not model.empty:
        model["model_value"] = scale_model_to_market(model["model_score"], market["market_value"]).round()
        market = market.merge(model[["gsis_id", "model_value"]], on="gsis_id", how="left")
        # Players the model loves but the market doesn't list - useful for spotting sleepers.
        listed = set(market["gsis_id"].dropna())
        for _, r in model[~model["gsis_id"].isin(listed)].nlargest(25, "model_score").iterrows():
            unmatched.append({"source": "model", "name": r["name"], "position": r["position"], "team": r["team"],
                              "problem": f"model value {int(r['model_value'])} but not in FantasyCalc list"})
    else:
        market["model_value"] = pd.NA

    prev = previous_snapshot(info.season, week)
    prev_values = {p["id"]: p["trade_value"] for p in prev["players"]} if prev else {}

    players = []
    for _, r in market.sort_values("market_value", ascending=False).iterrows():
        pid = r["sleeper_id"] or f"fc_{r['fc_id']}"
        age = r["age"] if pd.notna(r["age"]) else (round(r["fc_age"]) if pd.notna(r["fc_age"]) else None)
        model_value = int(r["model_value"]) if pd.notna(r["model_value"]) else None
        trade_value = int(r["market_value"])
        players.append({
            "id": str(pid),
            "name": r["name"],
            "position": r["position"],
            "team": r["team_norm"],
            "age": int(age) if age is not None else None,
            "injury_status": r["injury_status"] if isinstance(r["injury_status"], str) else None,
            "market_value": trade_value,
            "model_value": model_value,
            "trade_value": trade_value,
            "value_change_1wk": trade_value - prev_values[pid] if pid in prev_values else None,
            "market_vs_model": classify(trade_value, model_value),
            "nflverse_id": r["gsis_id"] if isinstance(r["gsis_id"], str) else None,  # used by backtest.py
        })

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "season": info.season,
        "week": week,
        "settings": SETTINGS,
        "sources": ["FantasyCalc (market values)", "Sleeper (player info)", "nflverse (stats)"],
        "players": players,
    }

    print("6/6 Validating and writing files...")
    validate(output)
    snapshot = HISTORY_DIR / f"values_{info.season}_w{week:02d}.json"
    write_json_atomic(snapshot, output)
    write_json_atomic(LATEST_FILE, output)
    pd.DataFrame(unmatched, columns=["source", "name", "position", "team", "problem"]).to_csv(UNMATCHED_FILE, index=False)
    print(f"    wrote {LATEST_FILE.name} and history/{snapshot.name} ({len(players)} players)")
    print(f"    {len(unmatched)} unmatched/flagged players logged to {UNMATCHED_FILE.name}")
    return output


def validate(output):
    """Refuse to write obviously broken output."""
    players = output["players"]
    problems = []
    if len(players) < 100:
        problems.append(f"only {len(players)} players")
    ids = [p["id"] for p in players]
    if len(ids) != len(set(ids)):
        problems.append("duplicate player ids")
    if any(not isinstance(p["trade_value"], int) or p["trade_value"] < 0 for p in players):
        problems.append("bad trade_value")
    with_model = sum(p["model_value"] is not None for p in players)
    if output["week"] >= 2 and with_model < 0.7 * len(players):
        problems.append(f"only {with_model}/{len(players)} players have a model value (matching broke?)")
    if problems:
        raise RuntimeError("Output failed validation: " + "; ".join(problems))


if __name__ == "__main__":
    try:
        build(force="--force" in sys.argv)
    except Exception as err:  # noqa: BLE001 - we want ANY failure to be loud
        print(f"\nERROR: {err}\nNo files were changed; the previous values_latest.json is still in place.")
        sys.exit(1)
