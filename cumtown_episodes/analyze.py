"""Analyze Cum Town YouTube clips: search, parse episode numbers, chart views per episode.

Reads YOUTUBE_API_KEY from environment. Searches "cum town" in yearly windows,
fetches view counts via videos.list, parses episode numbers from titles+descriptions,
aggregates total views per episode, and writes CSVs + a chart.

Outputs (under data/):
    clips.csv       per-clip rows
    episodes.csv    per-episode aggregates
    chart.png       views-per-episode over time
    .cache/         raw API responses (so re-runs don't burn quota)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import requests

API = "https://www.googleapis.com/youtube/v3"
HERE = Path(__file__).parent
DATA = HERE / "data"
CACHE = DATA / ".cache"
DATA.mkdir(exist_ok=True)
CACHE.mkdir(exist_ok=True)

QUERY = "cum town"
YEARS = list(range(2016, 2027))  # 2016-01-01 .. 2026-12-31 inclusive on the low end
PAGES_PER_YEAR = 2                # 2 * 50 = 100 results per year, ~2.2k quota units
ORDER = "viewCount"               # bias toward "successful" clips, which is the user's metric

# Episode-number regexes. Cum Town ran ~470 eps; we cap at 1..700 to be safe.
# - SHOW_NAME matches must contain "cum town"/"cumtown" adjacent to the number (always trustworthy).
# - GENERIC matches ("Episode N", "Ep N", "#N") are only trusted when "cum town"/"cumtown"
#   appears within PROXIMITY chars - this drops e.g. MSSP "Ep 403" guest appearances.
EP_PATTERNS_SHOW_NAME = [
    re.compile(r"(?i)(?:^|[^a-z0-9])(?:cumtown|cum\s*town)\s*[-:#,.]?\s*(?:ep(?:isode)?\.?\s*)?#?\s*(\d{1,4})\b"),
    re.compile(r"(?i)(?:^|[^a-z0-9])ct\s*[-:#]?\s*(\d{1,4})\b"),
]
EP_PATTERNS_GENERIC = [
    re.compile(r"(?i)\b(?:episode|ep\.?)\s*#?\s*(\d{1,4})\b"),
    re.compile(r"(?i)\bep(\d{1,4})\b"),
    re.compile(r"(?<![a-zA-Z0-9])#(\d{1,4})\b"),
]
CT_MENTION = re.compile(r"(?i)\b(?:cum\s*town|cumtown)\b")
EP_MIN, EP_MAX = 1, 700
PROXIMITY = 80  # chars - max distance between episode # and "cum town" mention for generic patterns


def api_key() -> str:
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        sys.exit("YOUTUBE_API_KEY not set")
    return key


def get(path: str, params: dict) -> dict:
    params = {**params, "key": api_key()}
    for attempt in range(4):
        r = requests.get(f"{API}/{path}", params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (403, 429):
            # quota or rate limit - bail loudly so we can resume tomorrow
            sys.exit(f"API error {r.status_code}: {r.text[:400]}")
        time.sleep(2 ** attempt)
    r.raise_for_status()
    return {}


def search_year(year: int) -> list[dict]:
    """Return raw search items for one year, cached on disk."""
    cache_file = CACHE / f"search_{year}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())["items"]

    items: list[dict] = []
    page_token: str | None = None
    for _ in range(PAGES_PER_YEAR):
        params = {
            "part": "snippet",
            "q": QUERY,
            "type": "video",
            "maxResults": 50,
            "order": ORDER,
            "publishedAfter": f"{year}-01-01T00:00:00Z",
            "publishedBefore": f"{year + 1}-01-01T00:00:00Z",
        }
        if page_token:
            params["pageToken"] = page_token
        resp = get("search", params)
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    cache_file.write_text(json.dumps({"items": items}))
    return items


def fetch_video_details(video_ids: list[str]) -> dict[str, dict]:
    """videos.list in batches of 50. Cached per batch."""
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        cache_file = CACHE / f"videos_{i:05d}.json"
        if cache_file.exists():
            data = json.loads(cache_file.read_text())
        else:
            data = get("videos", {
                "part": "snippet,statistics",
                "id": ",".join(batch),
            })
            cache_file.write_text(json.dumps(data))
        for v in data.get("items", []):
            out[v["id"]] = v
    return out


def _valid(n: int) -> bool:
    return EP_MIN <= n <= EP_MAX and not (2016 <= n <= 2030)


def parse_show_name(text: str) -> list[int]:
    """Strong matches: episode # right next to 'cum town' / 'cumtown' / 'CT'.
    Returns ALL distinct cited episodes (compilation clips often cite several)."""
    out: list[int] = []
    seen: set[int] = set()
    for pat in EP_PATTERNS_SHOW_NAME:
        for m in pat.finditer(text):
            n = int(m.group(1))
            if _valid(n) and n not in seen:
                seen.add(n)
                out.append(n)
    return out


def parse_generic(text: str, trust: bool) -> list[int]:
    """Generic 'Episode N' / 'Ep N' / '#N' matches. Without trust, each match
    must be within PROXIMITY chars of a 'cum town' mention."""
    out: list[int] = []
    seen: set[int] = set()
    ct_mentions = [m.start() for m in CT_MENTION.finditer(text)] if not trust else None
    for pat in EP_PATTERNS_GENERIC:
        for m in pat.finditer(text):
            n = int(m.group(1))
            if not _valid(n) or n in seen:
                continue
            if trust or any(abs(p - m.start()) <= PROXIMITY for p in ct_mentions):
                seen.add(n)
                out.append(n)
    return out


def parse_all(rows: list[dict]) -> tuple[list[list[int]], set[str]]:
    """Two-pass: pass 1 collects all SHOW_NAME matches and learns 'trusted' channels;
    pass 2 fills in remaining clips with generic matches under trust/proximity."""
    eps: list[list[int]] = [[] for _ in rows]
    trusted: set[str] = set()
    for i, r in enumerate(rows):
        text = f"{r['title']}\n{r['description']}"
        found = parse_show_name(text)
        if found:
            eps[i] = found
            trusted.add(r["channel"])
    for r in rows:
        if CT_MENTION.search(r["channel"]):
            trusted.add(r["channel"])
    for i, r in enumerate(rows):
        if eps[i]:
            continue
        text = f"{r['title']}\n{r['description']}"
        eps[i] = parse_generic(text, trust=r["channel"] in trusted)
    return eps, trusted


def main() -> None:
    print(f"Searching '{QUERY}' across {len(YEARS)} yearly windows ({PAGES_PER_YEAR} pages each, order={ORDER})...")
    all_items: list[dict] = []
    for year in YEARS:
        items = search_year(year)
        print(f"  {year}: {len(items):3d} results")
        all_items.extend(items)

    # dedupe by videoId
    seen: dict[str, dict] = {}
    for it in all_items:
        vid = it["id"]["videoId"]
        if vid not in seen:
            seen[vid] = it
    print(f"unique videos: {len(seen)}")

    print("Fetching video details (statistics + snippet)...")
    details = fetch_video_details(list(seen.keys()))
    print(f"detail records: {len(details)}")

    rows = []
    for vid, v in details.items():
        snip = v.get("snippet", {})
        stats = v.get("statistics", {})
        rows.append({
            "video_id": vid,
            "title": snip.get("title", ""),
            "description": snip.get("description", ""),
            "channel": snip.get("channelTitle", ""),
            "published": snip.get("publishedAt", ""),
            "views": int(stats.get("viewCount", 0)),
            "likes": int(stats.get("likeCount", 0)) if "likeCount" in stats else None,
            "url": f"https://youtu.be/{vid}",
        })

    eps_list, trusted = parse_all(rows)
    for r, eps in zip(rows, eps_list):
        r["episodes"] = eps                          # list[int]
        r["episodes_str"] = ";".join(str(e) for e in eps)
        r["n_episodes"] = len(eps)
    print(f"trusted CT channels: {len(trusted)}")
    multi = sum(1 for r in rows if r["n_episodes"] > 1)
    print(f"compilations citing 2+ episodes: {multi}")

    clips = pd.DataFrame(rows).sort_values("views", ascending=False)
    # description is bulky and the python-list 'episodes' column doesn't CSV well;
    # episodes_str (semicolon-delimited) is what we keep.
    clips.drop(columns=["description", "episodes"]).to_csv(DATA / "clips.csv", index=False)

    parsed = clips[clips["n_episodes"] > 0].copy()
    print(f"clips with at least one parseable episode #: {len(parsed)} / {len(clips)} "
          f"({len(parsed) / max(len(clips), 1):.0%})")

    # Explode: one row per (clip, cited_episode). view_share splits views across
    # all cited episodes so a 2-ep compilation contributes views/2 to each.
    exploded = parsed.explode("episodes").rename(columns={"episodes": "episode"})
    exploded["episode"] = exploded["episode"].astype(int)
    exploded["view_share"] = exploded["views"] / exploded["n_episodes"]

    eps = (
        exploded.groupby("episode")
        .agg(total_views=("view_share", "sum"),
             clip_count=("video_id", "count"),
             solo_clip_count=("n_episodes", lambda s: int((s == 1).sum())),
             top_clip_views=("views", "max"),
             top_clip_title=("title", lambda s: s.iloc[s.values.argmax()] if len(s) else ""),
             first_published=("published", "min"))
        .reset_index()
        .sort_values("episode")
    )
    eps["total_views"] = eps["total_views"].round().astype(int)
    eps.to_csv(DATA / "episodes.csv", index=False)

    print("\nTop 15 episodes by total clip views:")
    print(eps.sort_values("total_views", ascending=False).head(15)
          [["episode", "total_views", "clip_count", "top_clip_title"]]
          .to_string(index=False))

    plot(eps)
    print(f"\nWrote {DATA / 'clips.csv'}, {DATA / 'episodes.csv'}, {DATA / 'chart.png'}")


def plot(eps: pd.DataFrame) -> None:
    # Re-index to dense 1..max so rolling means span gaps where no clips were parsed.
    full = pd.DataFrame({"episode": range(1, int(eps["episode"].max()) + 1)})
    eps = full.merge(eps, on="episode", how="left").fillna(0)
    window = 15
    eps["views_roll"] = eps["total_views"].rolling(window, center=True, min_periods=1).mean()
    eps["count_roll"] = eps["clip_count"].rolling(window, center=True, min_periods=1).mean()

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), sharex=True)

    # Log scale on top so ep 36's 9.7M doesn't crush everything else.
    bars = eps["total_views"].replace(0, float("nan"))  # log can't render zeros
    ax1.bar(eps["episode"], bars, width=1.0, color="#c0392b", alpha=0.55, label="per-episode total")
    ax1.plot(eps["episode"], eps["views_roll"].replace(0, float("nan")),
             color="#7f1d1d", linewidth=2.2, label=f"{window}-ep rolling mean")
    ax1.set_yscale("log")
    ax1.set_ylabel("total clip views (log scale)")
    ax1.set_title("Cum Town: YouTube clip views per episode")
    ax1.grid(axis="y", alpha=0.3, which="both")
    ax1.legend(loc="upper right")

    nonzero = eps[eps["total_views"] > 0]
    for _, row in nonzero.nlargest(10, "total_views").iterrows():
        ax1.annotate(f"#{int(row['episode'])}",
                     xy=(row["episode"], row["total_views"]),
                     xytext=(0, 5), textcoords="offset points",
                     ha="center", fontsize=8)

    ax2.bar(eps["episode"], eps["clip_count"], width=1.0, color="#2c3e50", alpha=0.55,
            label="clips found")
    ax2.plot(eps["episode"], eps["count_roll"], color="#0b1f33", linewidth=2.2,
             label=f"{window}-ep rolling mean")
    ax2.set_ylabel("# of clips")
    ax2.set_xlabel("episode number")
    ax2.set_title("Distinct clips per episode (proxy for memorable moments)")
    ax2.grid(axis="y", alpha=0.3)
    ax2.legend(loc="upper right")

    fig.tight_layout()
    fig.savefig(DATA / "chart.png", dpi=140)
    plt.close(fig)


if __name__ == "__main__":
    main()
