"""
utils.py - small helpers shared by every script.

  * http_get()          -> download a URL with a timeout and automatic retries
  * cached_download()   -> download a file once and reuse it until it is "stale"
  * read_json/write_json_atomic -> save files safely (never leave half-written files)
  * normalize_name()    -> turn "D.J. Moore Jr." into "dj moore" so names match across sources
  * normalize_team()    -> turn "LA" / "JAC" / "WSH" into "LAR" / "JAX" / "WAS"
"""

import json
import os
import re
import time
import unicodedata
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# HTTP with timeouts + retries
# ---------------------------------------------------------------------------

# A generic, honest User-Agent. (No personal info - just says what this is.)
USER_AGENT = "trade-values-pipeline/1.0 (personal fantasy football project)"


def _make_session():
    """A requests Session that automatically retries temporary failures.

    Retry(total=3, backoff_factor=2) means: if the server returns 429 (too many
    requests) or a 5xx error, wait 2s, 4s, 8s between tries before giving up.
    That is polite to the APIs and survives brief hiccups.
    """
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = USER_AGENT
    return session


SESSION = _make_session()


def http_get(url, params=None, timeout=60):
    """GET a URL and raise a clear error if it fails (never return bad data)."""
    response = SESSION.get(url, params=params, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"Request failed ({response.status_code}) for {response.url}")
    return response


# ---------------------------------------------------------------------------
# Local cache for big files
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parent
CACHE_DIR = PROJECT_DIR / "data" / "cache"


def cached_download(url, filename, max_age_hours):
    """Download `url` into data/cache/<filename>, unless a fresh copy already exists.

    Returns the local file path. "Fresh" means younger than max_age_hours.
    This keeps us from hammering free APIs (e.g. Sleeper asks you to pull the
    big players file at most once a day).
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / filename
    if path.exists():
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours < max_age_hours:
            return path

    print(f"  downloading {url}")
    response = http_get(url, timeout=180)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(response.content)
    os.replace(tmp, path)  # only replace the old cache once the download fully worked
    return path


def read_csv_url(url, filename, max_age_hours=12, **read_csv_kwargs):
    """Download (or reuse cached) CSV and load it into a pandas DataFrame."""
    path = cached_download(url, filename, max_age_hours)
    return pd.read_csv(path, low_memory=False, **read_csv_kwargs)


# ---------------------------------------------------------------------------
# Safe JSON files
# ---------------------------------------------------------------------------

def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path, data):
    """Write JSON to a temp file first, then swap it in.

    If the script crashes halfway, the old file is still there untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Name / team matching helpers
# ---------------------------------------------------------------------------

NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# Players who go by a nickname in one source and a legal name in another.
# Add to this list whenever data/unmatched_players.csv shows a new one.
NICKNAMES = {
    "gabe davis": "gabriel davis",
    "hollywood brown": "marquise brown",
    "chig okonkwo": "chigoziem okonkwo",
    "josh palmer": "joshua palmer",
    "cam ward": "cameron ward",
    "tank dell": "nathaniel dell",
    "bam knight": "zonovan knight",
    "scotty miller": "scott miller",
}


def normalize_name(name):
    """'D.J. Moore Jr.' -> 'dj moore'; 'Amon-Ra St. Brown' -> 'amon ra st brown'."""
    if not isinstance(name, str):
        return ""
    # strip accents (e.g. "José" -> "Jose")
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().replace("'", "").replace(".", "")
    s = re.sub(r"[^a-z ]", " ", s)  # hyphens and other symbols become spaces
    words = [w for w in s.split() if w not in NAME_SUFFIXES]
    s = " ".join(words)
    return NICKNAMES.get(s, s)


# Different sources use different abbreviations for the same team.
TEAM_ALIASES = {"LA": "LAR", "JAC": "JAX", "WSH": "WAS", "OAK": "LV", "SD": "LAC", "STL": "LAR"}


def normalize_team(team):
    if not isinstance(team, str) or not team:
        return None
    team = team.upper()
    return TEAM_ALIASES.get(team, team)
