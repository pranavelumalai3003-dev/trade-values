"""
fetch_market.py - pull market trade values from FantasyCalc.

FantasyCalc builds its values from real trades made in real fantasy leagues,
so this is our "what will people actually accept" backbone.

Run on its own to preview:  python fetch_market.py
"""

import pandas as pd

from config import SETTINGS
from utils import http_get

FANTASYCALC_URL = "https://api.fantasycalc.com/values/current"

# If FantasyCalc suddenly returns far fewer players, something is wrong.
MIN_EXPECTED_PLAYERS = 100


class MarketDataError(RuntimeError):
    pass


def fetch_market_values(settings=SETTINGS):
    """Return a DataFrame with one row per player and their market value."""
    params = {
        "isDynasty": "false",                       # redraft (this season only)
        "numQbs": 2 if settings["superflex"] else 1,
        "numTeams": settings["num_teams"],
        "ppr": settings["ppr"],
    }
    data = http_get(FANTASYCALC_URL, params=params, timeout=30).json()

    # --- sanity checks: fail loudly instead of saving garbage ---------------
    if not isinstance(data, list) or len(data) < MIN_EXPECTED_PLAYERS:
        size = len(data) if isinstance(data, list) else type(data).__name__
        raise MarketDataError(f"FantasyCalc returned an unexpected response (size: {size}).")
    for key in ("player", "value"):
        if key not in data[0]:
            raise MarketDataError(f"FantasyCalc response format changed: missing '{key}'.")

    rows = []
    for item in data:
        p = item["player"]
        rows.append({
            "fc_id": p.get("id"),
            "sleeper_id": str(p["sleeperId"]) if p.get("sleeperId") else None,
            "espn_id": str(p["espnId"]) if p.get("espnId") else None,
            "name": p.get("name"),
            "position": p.get("position"),
            "team": p.get("maybeTeam"),
            "birth_date": p.get("maybeBirthday"),
            "fc_age": p.get("maybeAge"),
            "market_value_raw": item["value"],
            "fc_trend_30d": item.get("trend30Day"),
        })
    df = pd.DataFrame(rows)

    if df["market_value_raw"].isna().any() or (df["market_value_raw"] <= 0).all():
        raise MarketDataError("FantasyCalc values are missing or all zero.")
    return df


if __name__ == "__main__":
    market = fetch_market_values()
    print(f"{len(market)} players from FantasyCalc")
    print(market.groupby("position").size().to_string())
    print(market.head(10)[["name", "position", "team", "market_value_raw"]].to_string(index=False))
