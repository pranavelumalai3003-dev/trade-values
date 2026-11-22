"""
config.py - league settings, file locations, and "what season/week is it?"

Change the league settings below (or set environment variables of the same
name, e.g. in the GitHub workflow) to match your league.
"""

import os
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from utils import PROJECT_DIR, read_csv_url

# ---------------------------------------------------------------------------
# League settings
# ---------------------------------------------------------------------------

NUM_TEAMS = int(os.environ.get("NUM_TEAMS", 12))
PPR = float(os.environ.get("PPR", 1))  # 1 = full PPR, 0.5 = half, 0 = standard
SUPERFLEX = os.environ.get("SUPERFLEX", "false").lower() in ("1", "true", "yes")

SETTINGS = {"num_teams": NUM_TEAMS, "ppr": PPR, "superflex": SUPERFLEX}

# Most fantasy leagues finish in NFL week 17, so "rest of season" stops there.
FANTASY_LAST_WEEK = 17

SKILL_POSITIONS = ["QB", "RB", "WR", "TE"]

# ---------------------------------------------------------------------------
# File locations
# ---------------------------------------------------------------------------

DATA_DIR = PROJECT_DIR / "data"
HISTORY_DIR = DATA_DIR / "history"
LATEST_FILE = DATA_DIR / "values_latest.json"
UNMATCHED_FILE = DATA_DIR / "unmatched_players.csv"

# nflverse publishes its data as files attached to GitHub "releases".
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
SCHEDULE_URL = f"{NFLVERSE}/schedules/games.csv"

# ---------------------------------------------------------------------------
# Season / week detection (nothing is hardcoded)
# ---------------------------------------------------------------------------


@dataclass
class SeasonInfo:
    season: int               # e.g. 2026
    last_completed_week: int  # last regular-season week where every game is final (0 = none yet)
    in_season: bool           # False in the offseason -> the pipeline skips its run
    schedule: pd.DataFrame    # this season's regular-season games


def load_schedule():
    games = read_csv_url(SCHEDULE_URL, "games.csv", max_age_hours=6)
    games["gameday"] = pd.to_datetime(games["gameday"]).dt.date
    return games


def detect_season(today=None, games=None):
    """Work out the current season and the last fully completed week.

    How it works:
      1. The schedule file lists every game for past seasons and the upcoming one.
      2. The current season is the newest one whose first game is at most 7 days away.
      3. "In season" lasts from 7 days before week 1 until 2 days after the final
         regular-season game (fantasy leagues are over by the NFL playoffs).
      4. A week counts as completed once every game that week has a final score.
    """
    today = today or date.today()
    games = load_schedule() if games is None else games
    reg = games[games["game_type"] == "REG"]

    first_game = reg.groupby("season")["gameday"].min()
    last_game = reg.groupby("season")["gameday"].max()

    started = [s for s in first_game.index if first_game[s] - timedelta(days=7) <= today]
    if not started:
        raise RuntimeError("Schedule file has no season that has started - cannot detect season.")
    season = int(max(started))

    in_season = today <= last_game[season] + timedelta(days=2)

    season_games = reg[reg["season"] == season].copy()
    completed_weeks = [
        int(week)
        for week, wk_games in season_games.groupby("week")
        if wk_games["home_score"].notna().all() and (wk_games["gameday"] < today).all()
    ]
    # weeks must be completed in order, so take the longest unbroken run from week 1
    last_completed = 0
    while last_completed + 1 in completed_weeks:
        last_completed += 1

    return SeasonInfo(season, last_completed, in_season, season_games)


if __name__ == "__main__":
    info = detect_season()
    print("Settings:", SETTINGS)
    print(f"Season {info.season}, last completed week {info.last_completed_week}, in season: {info.in_season}")
