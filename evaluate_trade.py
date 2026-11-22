"""
evaluate_trade.py - judge a trade using the values in data/values_latest.json.

Why not just add up the values?
  In fantasy, one star beats two average players that "add up" to the same number:
  you only have so many starting spots, and the second player often sits on your bench.

The consolidation formula (a "power sum"):

      package_value = ( v1^K + v2^K + ... + vn^K ) ^ (1/K)      with K = 1.4

  * One player for one player: package_value = that player's value (no change).
  * Two 5,000 players: (2 x 5000^1.4)^(1/1.4) = 5000 x 2^(1/1.4) = about 8,200 -> worth less
    than one 10,000 player, even though the plain sum is equal.
  * Adding a low-value player still helps a little, but much less than its raw value.
  K = 1 would be a plain sum; bigger K = stars matter more.

Usage from the command line (names or Sleeper ids, comma separated):
  python evaluate_trade.py "Jahmyr Gibbs" "Bucky Irving, Justin Jefferson"
"""

import sys

from config import LATEST_FILE
from utils import normalize_name, read_json

K = 1.4
FAIR_PCT = 0.05     # within 5%  -> "fair"
SLIGHT_PCT = 0.15   # within 15% -> "slightly favors"


def package_value(values):
    """Combine several player values into one, rewarding consolidation."""
    values = [v for v in values if v and v > 0]
    if not values:
        return 0.0
    return sum(v ** K for v in values) ** (1 / K)


def evaluate_trade(side_a_ids, side_b_ids, values_file=LATEST_FILE):
    """Return each side's raw and adjusted totals plus a verdict.

    side_a_ids / side_b_ids: lists of player ids (the "id" field in values_latest.json).
    "Side A" is what one manager RECEIVES; "side B" is what the other manager receives.
    """
    data = read_json(values_file)
    by_id = {p["id"]: p for p in data["players"]}

    def side(ids):
        players = []
        for pid in ids:
            if str(pid) not in by_id:
                raise KeyError(f"Unknown player id: {pid}")
            players.append(by_id[str(pid)])
        vals = [p["trade_value"] for p in players]
        return {"players": [f"{p['name']} ({p['position']}) {p['trade_value']}" for p in players],
                "raw_total": sum(vals), "adjusted_total": round(package_value(vals)), "_sum_k": sum(v ** K for v in vals)}

    a, b = side(side_a_ids), side(side_b_ids)
    A, B = a["adjusted_total"], b["adjusted_total"]
    gap_pct = (A - B) / max(A, B, 1)

    if abs(gap_pct) < FAIR_PCT:
        verdict = "Fair trade"
    else:
        winner = "A" if gap_pct > 0 else "B"
        strength = "slightly favors" if abs(gap_pct) < SLIGHT_PCT else "favors"
        verdict = f"Trade {strength} side {winner} (the manager receiving side {winner})"

    # How much value would the short side need to ADD to make it even?
    big, small = (a, b) if A >= B else (b, a)
    needed = max(big["_sum_k"] - small["_sum_k"], 0) ** (1 / K)

    for s in (a, b):
        del s["_sum_k"]
    return {"side_a": a, "side_b": b, "difference_pct": round(gap_pct * 100, 1),
            "verdict": verdict, "value_to_even_it": round(needed)}


def _ids_from_names(text, players):
    by_name = {normalize_name(p["name"]): p["id"] for p in players}
    ids = []
    for item in [t.strip() for t in text.split(",") if t.strip()]:
        ids.append(item if item.isdigit() else by_name.get(normalize_name(item)) or item)
    return ids


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    all_players = read_json(LATEST_FILE)["players"]
    result = evaluate_trade(_ids_from_names(sys.argv[1], all_players), _ids_from_names(sys.argv[2], all_players))
    for label in ("side_a", "side_b"):
        s = result[label]
        print(f"{label}: {', '.join(s['players'])}")
        print(f"        raw total {s['raw_total']}, adjusted {s['adjusted_total']}")
    print(f"{result['verdict']}  (difference {result['difference_pct']}%; "
          f"short side needs ~{result['value_to_even_it']} more value to even it)")
