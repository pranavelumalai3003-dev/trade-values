"""
fetch_players.py - player info (team, age, injury status) from Sleeper.

The Sleeper players file is ~15 MB, and Sleeper asks developers to download it
at most once per day, so we cache it in data/cache/ for 24 hours.

Run on its own to preview:  python fetch_players.py
"""

import json

import pandas as pd

from config import SKILL_POSITIONS
from utils import cached_download, normalize_name, normalize_team

SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"


def fetch_sleeper_players():
    """Return a DataFrame of NFL skill-position players (QB/RB/WR/TE)."""
    path = cached_download(SLEEPER_PLAYERS_URL, "sleeper_players.json", max_age_hours=24)
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, dict) or len(raw) < 1000:
        raise RuntimeError("Sleeper players file looks wrong (too small or wrong format).")

    rows = []
    for sleeper_id, p in raw.items():
        if p.get("position") not in SKILL_POSITIONS:
            continue
        rows.append({
            "sleeper_id": str(sleeper_id),
            "sleeper_name": p.get("full_name"),
            "sleeper_position": p.get("position"),
            "sleeper_team": normalize_team(p.get("team")),  # None = free agent
            "age": p.get("age"),
            "sleeper_birth_date": p.get("birth_date"),
            # Sleeper values: None, "Questionable", "Doubtful", "Out", "IR", "PUP", "Sus", ...
            "injury_status": p.get("injury_status"),
            "sleeper_gsis_id": (p.get("gsis_id") or "").strip() or None,
            "sleeper_espn_id": str(p["espn_id"]) if p.get("espn_id") else None,
        })
    df = pd.DataFrame(rows)
    df["name_key"] = df["sleeper_name"].map(normalize_name)
    return df


if __name__ == "__main__":
    players = fetch_sleeper_players()
    print(f"{len(players)} skill-position players in Sleeper")
    print(f"{players['sleeper_team'].notna().sum()} are on an NFL team")
    print("Injury statuses:", players["injury_status"].value_counts().to_dict())
