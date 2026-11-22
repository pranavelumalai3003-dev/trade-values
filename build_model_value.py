"""
build_model_value.py - MY stats-based player value (the "second opinion").

The market (FantasyCalc) is the backbone. This model is a separate signal built
only from on-field stats, so we can spot players the market may be wrong about.

For every QB/RB/WR/TE it estimates "rest-of-season fantasy points above a
replacement-level player" in six steps:

  1. FORM      form_ppg     = 30% x last-4-games points/game + 70% x season points/game
  2. USAGE     expected_ppg = what an AVERAGE player at the position would score with
                              this player's last-4-games workload (pass attempts,
                              carries, targets). Rewards role, not just lucky TDs.
               projected    = blend of form_ppg and expected_ppg; usage weight is
                              0% QB, 35% RB/WR, 50% TE (see README for why)
  3. ROLE      x snap-share trend: if recent snap % is up vs. the season, nudge up
                              (and vice versa), capped at +/-10%.
               Small samples are pulled toward replacement level (2 "phantom" games).
  4. VOR       subtract the replacement-level points/game for the position
               (e.g. the 30th-best RB in a 12-team league).
  5. AVAILABILITY  share of his team's games the player has appeared in so far
               (players who miss games tend to keep missing games).
  6. SCHEDULE  multiply by remaining games through week 17 (bye weeks counted from the
               real schedule) x availability, minus games expected to be missed
               because of a current injury.

The weights were chosen with backtest.py on 2022-2024 (see README for results).

Run on its own to preview:  python build_model_value.py
"""

import numpy as np
import pandas as pd

from config import FANTASY_LAST_WEEK, NFLVERSE, SETTINGS, SKILL_POSITIONS, detect_season
from utils import normalize_name, normalize_team, read_csv_url

# --- model knobs (documented in the README) ---------------------------------
RECENT_GAMES = 4          # "recent form" window
FORM_RECENT_WEIGHT = 0.30 # weight of last-4 vs. full season in form_ppg
# Weight of expected_ppg (usage) vs. form_ppg. QB pass attempts don't predict QB
# scoring (efficiency and rushing matter more), so QBs use form only.
USAGE_WEIGHT = {"QB": 0.0, "RB": 0.35, "WR": 0.35, "TE": 0.50}
SNAP_SENSITIVITY = 0.5    # +10 pts of snap share -> +5% value
SNAP_CAP = 0.10           # never move value more than 10% for snap trend
SHRINK_GAMES = 2          # phantom replacement-level games added to every player

# Replacement level = the Nth best player at a position, where N = teams x this number.
# (1 QB, ~2.5 RB/WR including flex share, 1 TE per team.)
REPLACEMENT_PER_TEAM = {"QB": 1.0, "RB": 2.5, "WR": 2.5, "TE": 1.0}
REPLACEMENT_PER_TEAM_SUPERFLEX_QB = 2.0

# Expected games missed for each Sleeper injury status (rough, documented guesses).
INJURY_GAMES_MISSED = {"Questionable": 0.25, "Doubtful": 0.75, "Out": 1, "Sus": 1, "IR": 4, "PUP": 4}

MODEL_COLUMNS = [
    "gsis_id", "name", "position", "team", "games", "season_ppg", "recent_ppg", "expected_ppg",
    "snap_mult", "projected_ppg", "replacement_ppg", "availability", "remaining_games",
    "ros_points", "model_score",
]


# ---------------------------------------------------------------------------
# Loading nflverse data
# ---------------------------------------------------------------------------

def load_weekly_stats(season):
    url = f"{NFLVERSE}/stats_player/stats_player_week_{season}.csv"
    stats = read_csv_url(url, f"stats_player_week_{season}.csv", max_age_hours=6)
    needed = {"player_id", "player_display_name", "position", "team", "week", "season_type",
              "game_id", "fantasy_points", "receptions", "targets", "carries", "attempts"}
    missing = needed - set(stats.columns)
    if missing:
        raise RuntimeError(f"nflverse stats format changed, missing columns: {sorted(missing)}")
    stats = stats[(stats["season_type"] == "REG") & stats["position"].isin(SKILL_POSITIONS)]
    return stats[stats["player_id"].notna()].copy()


def load_snap_counts(season):
    url = f"{NFLVERSE}/snap_counts/snap_counts_{season}.csv"
    try:
        snaps = read_csv_url(url, f"snap_counts_{season}.csv", max_age_hours=6)
    except RuntimeError as err:
        # Snap counts only fine-tune the model, so we can live without them.
        print(f"  WARNING: snap counts unavailable ({err}); snap-trend adjustment skipped.")
        return pd.DataFrame(columns=["pfr_player_id", "week", "offense_pct", "game_type"])
    return snaps[snaps["game_type"] == "REG"].copy()


def load_nflverse_players():
    """Master player list from nflverse: links gsis_id <-> espn_id / pfr_id, plus birth dates."""
    players = read_csv_url(f"{NFLVERSE}/players/players.csv", "nflverse_players.csv", max_age_hours=24)
    players = players[["gsis_id", "display_name", "position", "birth_date", "espn_id", "pfr_id", "latest_team"]]
    players = players[players["gsis_id"].notna()].copy()
    # espn_id can be read as a float like 4429795.0 -> make it the text "4429795"
    players["espn_id"] = pd.to_numeric(players["espn_id"], errors="coerce").astype("Int64").astype("string")
    return players


def stats_complete_through(stats, schedule, last_completed_week):
    """nflverse can lag a few hours behind the schedule (e.g. Monday night games).

    Walk back from the last completed week until the stats file contains every game.
    """
    week = last_completed_week
    while week > 0:
        scheduled = set(schedule.loc[schedule["week"] == week, "game_id"])
        in_stats = set(stats.loc[stats["week"] == week, "game_id"])
        if scheduled <= in_stats:
            return week
        print(f"  NOTE: nflverse stats for week {week} are incomplete; using week {week - 1}.")
        week -= 1
    return 0


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

def _points(stats, ppr):
    """League fantasy points per game row, plus passing/rushing/receiving pieces."""
    s = stats.fillna({c: 0 for c in [
        "fantasy_points", "receptions", "passing_yards", "passing_tds", "passing_interceptions",
        "rushing_yards", "rushing_tds", "receiving_yards", "receiving_tds",
        "attempts", "carries", "targets"]})
    s["pts"] = s["fantasy_points"] + ppr * s["receptions"]  # nflverse fantasy_points = standard scoring
    s["pass_pts"] = 0.04 * s["passing_yards"] + 4 * s["passing_tds"] - 2 * s["passing_interceptions"]
    s["rush_pts"] = 0.1 * s["rushing_yards"] + 6 * s["rushing_tds"]
    s["rec_pts"] = ppr * s["receptions"] + 0.1 * s["receiving_yards"] + 6 * s["receiving_tds"]
    return s


def _team_games(schedule, first_week, last_week):
    """{team: number of games between first_week and last_week (inclusive)}."""
    games = schedule[(schedule["week"] >= first_week) & (schedule["week"] <= last_week)]
    teams = pd.concat([games["home_team"], games["away_team"]]).map(normalize_team)
    return teams.value_counts().to_dict()


def _snap_multiplier(snaps, nfl_players, through_week):
    """{gsis_id: multiplier between 0.9 and 1.1} based on recent vs. season snap share."""
    if snaps.empty:
        return {}
    pfr_to_gsis = nfl_players.dropna(subset=["pfr_id"]).set_index("pfr_id")["gsis_id"].to_dict()
    sn = snaps[snaps["week"] <= through_week].copy()
    sn["gsis_id"] = sn["pfr_player_id"].map(pfr_to_gsis)
    sn = sn.dropna(subset=["gsis_id"]).sort_values(["gsis_id", "week"])
    sn["from_end"] = sn.groupby("gsis_id").cumcount(ascending=False)
    season_pct = sn.groupby("gsis_id")["offense_pct"].mean()
    recent_pct = sn[sn["from_end"] < RECENT_GAMES].groupby("gsis_id")["offense_pct"].mean()
    mult = (1 + SNAP_SENSITIVITY * (recent_pct - season_pct)).clip(1 - SNAP_CAP, 1 + SNAP_CAP)
    return mult.to_dict()


def compute_model(stats, snaps, nfl_players, schedule, through_week, settings=SETTINGS):
    """Core model. Returns one row per player (keyed by nflverse gsis_id)."""
    s = stats[stats["week"] <= through_week]
    if s.empty:
        return pd.DataFrame(columns=MODEL_COLUMNS)
    s = _points(s, settings["ppr"]).sort_values(["player_id", "week"])

    # --- Step 2a: league-average points per opportunity, by position --------
    totals = s.groupby("position")[["pass_pts", "attempts", "rush_pts", "carries", "rec_pts", "targets"]].sum()
    rate_pass = (totals["pass_pts"] / totals["attempts"]).replace([np.inf], 0).fillna(0)
    rate_rush = (totals["rush_pts"] / totals["carries"]).replace([np.inf], 0).fillna(0)
    rate_rec = (totals["rec_pts"] / totals["targets"]).replace([np.inf], 0).fillna(0)
    s["xpts"] = (s["attempts"] * s["position"].map(rate_pass)
                 + s["carries"] * s["position"].map(rate_rush)
                 + s["targets"] * s["position"].map(rate_rec))

    # --- Step 1: form --------------------------------------------------------
    s["from_end"] = s.groupby("player_id").cumcount(ascending=False)  # 0 = most recent game
    recent = s[s["from_end"] < RECENT_GAMES]
    season_agg = s.groupby("player_id").agg(
        name=("player_display_name", "last"), position=("position", "last"),
        team=("team", "last"), games=("pts", "size"), season_ppg=("pts", "mean"))
    recent_agg = recent.groupby("player_id").agg(recent_ppg=("pts", "mean"), expected_ppg=("xpts", "mean"))
    m = season_agg.join(recent_agg)
    m.index.name = "gsis_id"
    m = m.reset_index()
    m["team"] = m["team"].map(normalize_team)
    form_ppg = FORM_RECENT_WEIGHT * m["recent_ppg"] + (1 - FORM_RECENT_WEIGHT) * m["season_ppg"]

    # --- Steps 2b + 3: blend with usage, apply snap trend ---------------------
    m["snap_mult"] = m["gsis_id"].map(_snap_multiplier(snaps, nfl_players, through_week)).fillna(1.0)
    usage_w = m["position"].map(USAGE_WEIGHT)
    m["projected_ppg"] = ((1 - usage_w) * form_ppg + usage_w * m["expected_ppg"]) * m["snap_mult"]

    # --- Step 4: replacement level per position -----------------------------
    repl = {}
    for pos in SKILL_POSITIONS:
        per_team = REPLACEMENT_PER_TEAM[pos]
        if pos == "QB" and settings["superflex"]:
            per_team = REPLACEMENT_PER_TEAM_SUPERFLEX_QB
        rank = max(1, round(settings["num_teams"] * per_team))
        ppg = m.loc[m["position"] == pos, "projected_ppg"].sort_values(ascending=False)
        repl[pos] = float(ppg.iloc[min(rank, len(ppg)) - 1]) if len(ppg) else 0.0
    m["replacement_ppg"] = m["position"].map(repl)

    # shrink small samples toward replacement level (a 1-game wonder isn't a stud yet)
    m["projected_ppg"] = ((m["games"] * m["projected_ppg"] + SHRINK_GAMES * m["replacement_ppg"])
                          / (m["games"] + SHRINK_GAMES))

    # --- Step 5: availability (games played / team games so far, lightly shrunk) ---
    team_played = m["team"].map(_team_games(schedule, 1, through_week)).fillna(m["games"])
    m["availability"] = ((m["games"] + 1) / (team_played + 1)).clip(upper=1.0)

    # --- Step 6: remaining schedule -----------------------------------------
    remaining = _team_games(schedule, through_week + 1, FANTASY_LAST_WEEK)
    m["remaining_games"] = m["team"].map(remaining).fillna(0).astype(int)
    expected_games = m["remaining_games"] * m["availability"]
    m["ros_points"] = m["projected_ppg"] * expected_games
    m["model_score"] = _score(m["projected_ppg"] - m["replacement_ppg"], expected_games, m["remaining_games"])
    return m[MODEL_COLUMNS]


def _score(vor_ppg, expected_games, remaining_games):
    """Points above replacement for the rest of the season.

    Above-replacement players: value x games we expect them to actually play.
    Below-replacement players: use ALL remaining games, so missing games can never
    make a bad player look better than a healthy one.
    """
    return np.where(vor_ppg > 0, vor_ppg * expected_games, vor_ppg * remaining_games)


def apply_injuries(model, injury_status_by_gsis):
    """Reduce remaining games for injured players, then recompute ros_points and model_score."""
    m = model.copy()
    missed = m["gsis_id"].map(injury_status_by_gsis).map(INJURY_GAMES_MISSED).fillna(0)
    expected_games = m["remaining_games"] * m["availability"]
    effective_games = (expected_games - missed).clip(lower=0)
    m["games_missed_injury"] = missed
    m["ros_points"] = m["projected_ppg"] * effective_games
    m["model_score"] = _score(m["projected_ppg"] - m["replacement_ppg"], effective_games, m["remaining_games"])
    return m


def build_model_values(info, settings=SETTINGS):
    """Load this season's data and run the model. Returns (model DataFrame, through_week)."""
    stats = load_weekly_stats(info.season)
    through_week = stats_complete_through(stats, info.schedule, info.last_completed_week)
    snaps = load_snap_counts(info.season)
    nfl_players = load_nflverse_players()
    model = compute_model(stats, snaps, nfl_players, info.schedule, through_week, settings)
    model["name_key"] = model["name"].map(normalize_name)
    return model, through_week, nfl_players


if __name__ == "__main__":
    info = detect_season()
    model, week, _ = build_model_values(info)
    print(f"Model built from {info.season} weeks 1-{week}: {len(model)} players")
    cols = ["name", "position", "team", "games", "season_ppg", "projected_ppg", "remaining_games", "model_score"]
    for pos in SKILL_POSITIONS:
        print(f"\nTop 5 {pos}:")
        top = model[model["position"] == pos].nlargest(5, "model_score")[cols]
        print(top.round(1).to_string(index=False))
