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

# Episode-number regexes. Cum Town ran ~470 main eps + Patreon-only "bonus"/"premium"
# episodes with their own numbering; we cap at 1..700 to be safe and tag each match
# as "regular" or "bonus".
EP_PATTERNS_SHOW_NAME = [
    re.compile(r"(?i)(?:^|[^a-z0-9])(?:cumtown|cum\s*town)\s*[-:#,.]?\s*(?:ep(?:isode)?\.?\s*)?#?\s*(\d{1,4})\b"),
    re.compile(r"(?i)(?:^|[^a-z0-9])ct\s*[-:#]?\s*(\d{1,4})\b"),
]
EP_PATTERNS_GENERIC = [
    re.compile(r"(?i)\b(?:episode|ep\.?)\s*#?\s*(\d{1,4})\b"),
    re.compile(r"(?i)\bep(\d{1,4})\b"),
    re.compile(r"(?<![a-zA-Z0-9])#(\d{1,4})\b"),
]
# Explicit bonus/premium episode markers - "Bonus 11", "Premium Ep 49", "Premium #6",
# "premium episode 183". Anything matched here is bonus by definition.
EP_PATTERNS_BONUS = [
    re.compile(r"(?i)\b(?:bonus|premium)\s*[-:]?\s*(?:ep(?:isode)?\.?\s*)?#?\s*(\d{1,4})\b"),
]
CT_MENTION = re.compile(r"(?i)\b(?:cum\s*town|cumtown)\b")
BONUS_MARKER = re.compile(r"(?i)\b(?:bonus|premium)\b")
EP_MIN, EP_MAX = 1, 700
PROXIMITY = 80    # max chars between ep # and "cum town" mention for generic patterns
BONUS_LOOKBACK = 20  # max chars before ep # to look for "bonus"/"premium" marker


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


def _classify(text: str, ep_start: int) -> str:
    """Return 'bonus' if 'bonus'/'premium' appears within BONUS_LOOKBACK chars
    immediately before the episode-# match, else 'regular'."""
    window = text[max(0, ep_start - BONUS_LOOKBACK):ep_start]
    return "bonus" if BONUS_MARKER.search(window) else "regular"


def find_episodes(text: str, trust: bool) -> list[tuple[str, int]]:
    """Extract all (kind, episode) pairs from one clip's text.
    - Explicit "Bonus N" / "Premium N" patterns are always tagged bonus.
    - Show-name matches (e.g. "Cum Town Episode 36") are accepted directly;
      kind is determined by lookback for a nearby bonus marker.
    - Generic "Episode N"/"Ep N"/"#N" matches need either a trusted channel
      or proximity to a 'cum town' mention; kind via the same lookback rule.
    """
    out: set[tuple[str, int]] = set()

    for pat in EP_PATTERNS_BONUS:
        for m in pat.finditer(text):
            n = int(m.group(1))
            if _valid(n):
                out.add(("bonus", n))

    for pat in EP_PATTERNS_SHOW_NAME:
        for m in pat.finditer(text):
            n = int(m.group(1))
            if _valid(n):
                out.add((_classify(text, m.start()), n))

    ct_mentions = [m.start() for m in CT_MENTION.finditer(text)] if not trust else None
    for pat in EP_PATTERNS_GENERIC:
        for m in pat.finditer(text):
            n = int(m.group(1))
            if not _valid(n):
                continue
            if trust or any(abs(p - m.start()) <= PROXIMITY for p in ct_mentions):
                out.add((_classify(text, m.start()), n))

    return sorted(out, key=lambda kn: (kn[0], kn[1]))


def parse_all(rows: list[dict]) -> tuple[list[list[tuple[str, int]]], set[str]]:
    """Two-pass: pass 1 finds strong show-name matches to learn 'trusted' channels;
    pass 2 extracts all (kind, episode) pairs with trust applied."""
    trusted: set[str] = set()
    has_strong: list[bool] = []
    for r in rows:
        text = f"{r['title']}\n{r['description']}"
        strong = any(_valid(int(m.group(1)))
                     for pat in EP_PATTERNS_SHOW_NAME for m in pat.finditer(text))
        has_strong.append(strong)
        if strong:
            trusted.add(r["channel"])
    for r in rows:
        if CT_MENTION.search(r["channel"]):
            trusted.add(r["channel"])

    eps: list[list[tuple[str, int]]] = []
    for r in rows:
        text = f"{r['title']}\n{r['description']}"
        eps.append(find_episodes(text, trust=r["channel"] in trusted))
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
        r["episodes"] = eps                                   # list[(kind, n)]
        r["regular_eps"] = ";".join(str(n) for k, n in eps if k == "regular")
        r["bonus_eps"] = ";".join(str(n) for k, n in eps if k == "bonus")
        r["n_episodes"] = len(eps)
    print(f"trusted CT channels: {len(trusted)}")
    multi = sum(1 for r in rows if r["n_episodes"] > 1)
    print(f"clips citing 2+ episodes: {multi}")
    n_bonus = sum(1 for r in rows if r["bonus_eps"])
    n_reg = sum(1 for r in rows if r["regular_eps"])
    n_both = sum(1 for r in rows if r["bonus_eps"] and r["regular_eps"])
    print(f"clips citing regular eps: {n_reg}; bonus eps: {n_bonus}; both: {n_both}")

    clips = pd.DataFrame(rows).sort_values("views", ascending=False)
    # 'episodes' column holds python tuples - replace with string columns for CSV.
    clips.drop(columns=["description", "episodes"]).to_csv(DATA / "clips.csv", index=False)

    parsed = clips[clips["n_episodes"] > 0].copy()
    print(f"clips with at least one parseable episode #: {len(parsed)} / {len(clips)} "
          f"({len(parsed) / max(len(clips), 1):.0%})")

    # Explode: one row per (clip, kind, cited_episode). view_share splits views
    # across every (kind, ep) pair the clip cites - so a clip citing both bonus 11
    # and regular 31 contributes views/2 to each.
    exploded = parsed.explode("episodes")
    exploded["kind"] = exploded["episodes"].apply(lambda kn: kn[0])
    exploded["episode"] = exploded["episodes"].apply(lambda kn: int(kn[1]))
    exploded["view_share"] = exploded["views"] / exploded["n_episodes"]

    eps = (
        exploded.groupby(["kind", "episode"])
        .agg(total_views=("view_share", "sum"),
             clip_count=("video_id", "count"),
             solo_clip_count=("n_episodes", lambda s: int((s == 1).sum())),
             top_clip_views=("views", "max"),
             top_clip_title=("title", lambda s: s.iloc[s.values.argmax()] if len(s) else ""),
             first_published=("published", "min"))
        .reset_index()
        .sort_values(["kind", "episode"])
    )
    eps["total_views"] = eps["total_views"].round().astype(int)
    eps.to_csv(DATA / "episodes.csv", index=False)

    for kind in ("regular", "bonus"):
        sub = eps[eps["kind"] == kind]
        print(f"\nTop 10 {kind} episodes by total clip views ({len(sub)} eps total):")
        print(sub.sort_values("total_views", ascending=False).head(10)
              [["episode", "total_views", "clip_count", "top_clip_title"]]
              .to_string(index=False))

    plot(eps)
    print(f"\nWrote {DATA / 'clips.csv'}, {DATA / 'episodes.csv'}, {DATA / 'chart.png'}")


def plot(eps: pd.DataFrame) -> None:
    window = 15

    def panel(ax_views, ax_count, sub: pd.DataFrame, label: str, color: str):
        if sub.empty:
            return
        max_ep = int(sub["episode"].max())
        full = pd.DataFrame({"episode": range(1, max_ep + 1)})
        d = full.merge(sub, on="episode", how="left").fillna(0)
        d["views_roll"] = d["total_views"].rolling(window, center=True, min_periods=1).mean()
        d["count_roll"] = d["clip_count"].rolling(window, center=True, min_periods=1).mean()

        bars = d["total_views"].replace(0, float("nan"))
        ax_views.bar(d["episode"], bars, width=1.0, color=color, alpha=0.55, label="per-episode total")
        ax_views.plot(d["episode"], d["views_roll"].replace(0, float("nan")),
                      color=color, linewidth=2.2, label=f"{window}-ep rolling mean")
        ax_views.set_yscale("log")
        ax_views.set_ylabel("total clip views (log)")
        ax_views.set_title(f"Cum Town {label}: YouTube clip views per episode")
        ax_views.grid(axis="y", alpha=0.3, which="both")
        ax_views.legend(loc="upper right", fontsize=8)
        for _, row in d[d["total_views"] > 0].nlargest(8, "total_views").iterrows():
            ax_views.annotate(f"#{int(row['episode'])}",
                              xy=(row["episode"], row["total_views"]),
                              xytext=(0, 5), textcoords="offset points",
                              ha="center", fontsize=8)

        ax_count.bar(d["episode"], d["clip_count"], width=1.0, color=color, alpha=0.5,
                     label="clips found")
        ax_count.plot(d["episode"], d["count_roll"], color=color, linewidth=2.2,
                      label=f"{window}-ep rolling mean")
        ax_count.set_ylabel("# of clips")
        ax_count.set_xlabel("episode number")
        ax_count.set_title(f"Cum Town {label}: distinct clips per episode")
        ax_count.grid(axis="y", alpha=0.3)
        ax_count.legend(loc="upper right", fontsize=8)

    fig, axes = plt.subplots(2, 2, figsize=(18, 9))
    panel(axes[0][0], axes[1][0], eps[eps["kind"] == "regular"], "(regular feed)", "#c0392b")
    panel(axes[0][1], axes[1][1], eps[eps["kind"] == "bonus"], "Premium (bonus feed)", "#2980b9")
    fig.tight_layout()
    fig.savefig(DATA / "chart.png", dpi=140)
    plt.close(fig)


if __name__ == "__main__":
    main()
