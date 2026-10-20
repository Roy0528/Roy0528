"""產生 GitHub 個人頁用的統計卡片 SVG：總覽、常用語言、連續貢獻，各有淺色與深色版本。

只用標準函式庫，GitHub Actions 上不需要安裝任何套件。

Example:
    STATS_TOKEN=xxx python scripts/generate_stats.py --user Roy0528 --out dist
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

GRAPHQL_URL = "https://api.github.com/graphql"

# 練習專案內建的 Bootstrap 等樣式檔會灌水比例，個人頁的技術棧也沒列這些
EXCLUDED_LANGUAGES = {"CSS", "SCSS", "HTML", "Batchfile", "Procfile"}
TOP_LANGUAGE_COUNT = 5
FALLBACK_LANGUAGE_COLOR = "#8b949e"
BRAND_GRADIENT = ("#6a11cb", "#2575fc", "#00c9a7")

THEMES = {
    "light": {
        "bg": "#ffffff",
        "border": "#d0d7de",
        "title": "#0969da",
        "text": "#1f2328",
        "muted": "#656d76",
        "track": "#eaeef2",
        "fire": "#bc4c00",
    },
    "dark": {
        "bg": "#161b22",
        "border": "#30363d",
        "title": "#2f81f7",
        "text": "#e6edf3",
        "muted": "#7d8590",
        "track": "#30363d",
        "fire": "#fb8f44",
    },
}

PROFILE_QUERY = """
query($login: String!, $after: String) {
  user(login: $login) {
    name
    login
    createdAt
    pullRequests { totalCount }
    repositories(ownerAffiliations: OWNER, first: 100, after: $after) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        isFork
        languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""

YEAR_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      restrictedContributionsCount
      contributionCalendar {
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class Streak:
    """一段連續有貢獻的日子。

    Attributes:
        length: 連續天數，0 代表沒有。
        start: 第一天。
        end: 最後一天。
    """

    length: int = 0
    start: date | None = None
    end: date | None = None


@dataclass(frozen=True)
class Language:
    """常用語言卡片上的一列。

    Attributes:
        name: 語言名稱。
        color: GitHub 給該語言的代表色。
        ratio: 佔所有計入語言的比例（0 到 1）。
    """

    name: str
    color: str
    ratio: float


@dataclass(frozen=True)
class ProfileStats:
    """畫卡片需要的全部數字。

    Attributes:
        display_name: 卡片標題用的名字。
        member_since: 帳號建立年份。
        commits: 所有年份的 commit 數，含私人貢獻。
        pull_requests: 開過的 PR 總數。
        repositories: 擁有的 repo 數。
        contributions_total: 所有年份的貢獻總數。
        contributions_this_year: 今年的貢獻數。
        current_year: 今年。
        first_contribution: 第一次有貢獻的日子。
        current_streak: 目前的連續貢獻。
        longest_streak: 最長的連續貢獻。
        languages: 常用語言，由多到少。
    """

    display_name: str
    member_since: int
    commits: int
    pull_requests: int
    repositories: int
    contributions_total: int
    contributions_this_year: int
    current_year: int
    first_contribution: date | None
    current_streak: Streak
    longest_streak: Streak
    languages: list[Language]


def run_query(token: str, query: str, variables: dict) -> dict:
    """呼叫 GitHub GraphQL API。

    Args:
        token: GitHub token。
        query: GraphQL 查詢字串。
        variables: 查詢變數。

    Returns:
        回應中的 data 欄位。

    Raises:
        RuntimeError: API 回傳 errors 時。
    """
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-stats-generator",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.load(response)
    if body.get("errors"):
        raise RuntimeError(json.dumps(body["errors"], ensure_ascii=False))
    return body["data"]


def fetch_profile(token: str, login: str) -> tuple[dict, list[dict]]:
    """取得使用者基本資料與所有擁有的 repo。

    Args:
        token: GitHub token。
        login: GitHub 帳號。

    Returns:
        (使用者資料, repo 清單)。
    """
    repos: list[dict] = []
    after = None
    while True:
        user = run_query(token, PROFILE_QUERY, {"login": login, "after": after})["user"]
        page = user["repositories"]
        repos.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return user, repos
        after = page["pageInfo"]["endCursor"]


def fetch_contributions(token: str, login: str, since: datetime, now: datetime) -> tuple[int, dict[date, int]]:
    """逐年取得貢獻資料（API 每次最多查一年）。

    Args:
        token: GitHub token。
        login: GitHub 帳號。
        since: 帳號建立時間。
        now: 現在時間（UTC）。

    Returns:
        (commit 數, 每天的貢獻數)。
    """
    commits = 0
    days: dict[date, int] = {}
    for year in range(since.year, now.year + 1):
        start = max(since, datetime(year, 1, 1, tzinfo=timezone.utc))
        end = min(now, datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc))
        collection = run_query(
            token,
            YEAR_QUERY,
            {"login": login, "from": start.isoformat(), "to": end.isoformat()},
        )["user"]["contributionsCollection"]
        # private repo 的 commit 只會出現在 restricted 裡，就算用本人 token 也一樣；
        # 比照 github-readme-stats 的 count_private 做法把它加進 commit 數
        commits += collection["totalCommitContributions"] + collection["restrictedContributionsCount"]
        for week in collection["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                day_date = date.fromisoformat(day["date"])
                if day_date <= now.date():
                    days[day_date] = day["contributionCount"]
    return commits, days


def compute_streaks(days: dict[date, int], today: date) -> tuple[Streak, Streak]:
    """算出目前與最長的連續貢獻。

    今天還沒有貢獻時，目前的連續貢獻從昨天往回算，才不會一早就歸零。

    Args:
        days: 每天的貢獻數，日期需連續。
        today: 今天。

    Returns:
        (目前的連續貢獻, 最長的連續貢獻)。
    """
    longest = Streak()
    run_start: date | None = None
    run_length = 0
    for day in sorted(days):
        if days[day] > 0:
            run_length += 1
            if run_length == 1:
                run_start = day
            if run_length > longest.length:
                longest = Streak(run_length, run_start, day)
        else:
            run_length = 0

    cursor = today if days.get(today, 0) > 0 else today - timedelta(days=1)
    end = cursor
    length = 0
    while days.get(cursor, 0) > 0:
        length += 1
        cursor -= timedelta(days=1)
    current = Streak(length, cursor + timedelta(days=1), end) if length else Streak()
    return current, longest


def aggregate_languages(repos: list[dict], excluded: set[str], top_n: int) -> list[Language]:
    """把所有非 fork repo 的語言位元組數加總，取前幾名。

    Args:
        repos: fetch_profile 回傳的 repo 清單。
        excluded: 不計入的語言。
        top_n: 取前幾名。

    Returns:
        由多到少的語言清單；比例的分母是所有計入的語言，不只前幾名。
    """
    sizes: dict[str, int] = {}
    colors: dict[str, str] = {}
    for repo in repos:
        if repo["isFork"]:
            continue
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            if name in excluded:
                continue
            sizes[name] = sizes.get(name, 0) + edge["size"]
            colors[name] = edge["node"]["color"] or FALLBACK_LANGUAGE_COLOR
    total = sum(sizes.values())
    if not total:
        return []
    ranked = sorted(sizes.items(), key=lambda item: item[1], reverse=True)[:top_n]
    return [Language(name, colors[name], size / total) for name, size in ranked]


def collect_stats(token: str, login: str, now: datetime) -> ProfileStats:
    """抓資料並整理成畫卡片要用的數字。

    Args:
        token: GitHub token。
        login: GitHub 帳號。
        now: 現在時間（UTC）。

    Returns:
        整理好的統計資料。
    """
    user, repos = fetch_profile(token, login)
    created = datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00"))
    commits, days = fetch_contributions(token, login, created, now)
    current, longest = compute_streaks(days, now.date())
    active_days = [day for day, count in days.items() if count > 0]
    return ProfileStats(
        display_name=user["name"] or user["login"],
        member_since=created.year,
        commits=commits,
        pull_requests=user["pullRequests"]["totalCount"],
        repositories=user["repositories"]["totalCount"],
        contributions_total=sum(days.values()),
        contributions_this_year=sum(count for day, count in days.items() if day.year == now.year),
        current_year=now.year,
        first_contribution=min(active_days) if active_days else None,
        current_streak=current,
        longest_streak=longest,
        languages=aggregate_languages(repos, EXCLUDED_LANGUAGES, TOP_LANGUAGE_COUNT),
    )


def format_number(value: int) -> str:
    """把大數字縮寫成 1.2k 這種格式。"""
    if value >= 1000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    return str(value)


def format_date(value: date, with_year: bool = True) -> str:
    """把日期轉成 Mar 3, 2021 這種格式。"""
    text = f"{value:%b} {value.day}"
    return f"{text}, {value.year}" if with_year else text


def format_range(streak: Streak, current_year: int) -> str:
    """把一段連續貢獻轉成日期區間文字；同一年就省略年份。"""
    if not streak.length or streak.start is None or streak.end is None:
        return "No active streak"
    with_year = not (streak.start.year == streak.end.year == current_year)
    if streak.start == streak.end:
        return format_date(streak.start, with_year)
    return f"{format_date(streak.start, with_year)} – {format_date(streak.end, with_year)}"


def svg_document(width: int, height: int, theme: dict[str, str], label: str, body: str) -> str:
    """包上共用的 SVG 外框、字型與動畫樣式。

    Args:
        width: 寬度。
        height: 高度。
        theme: THEMES 裡的配色。
        label: 給螢幕閱讀器的說明。
        body: 卡片內容。

    Returns:
        完整的 SVG 字串。
    """
    first, middle, last = BRAND_GRADIENT
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">
  <style>
    text {{ font-family: "Segoe UI", Ubuntu, "Helvetica Neue", Arial, sans-serif; }}
    .title {{ font-size: 17px; font-weight: 600; fill: {theme["title"]}; }}
    .label {{ font-size: 14px; fill: {theme["text"]}; }}
    .value {{ font-size: 14px; font-weight: 700; fill: {theme["text"]}; }}
    .muted {{ font-size: 12px; fill: {theme["muted"]}; }}
    .big {{ font-size: 28px; font-weight: 700; fill: {theme["text"]}; }}
    .fire {{ font-size: 14px; font-weight: 700; fill: {theme["fire"]}; }}
    .fade {{ opacity: 0; animation: fade 0.6s ease-out forwards; }}
    @keyframes fade {{ from {{ opacity: 0; transform: translateX(-6px); }} to {{ opacity: 1; transform: translateX(0); }} }}
  </style>
  <defs>
    <linearGradient id="brand" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{first}"/>
      <stop offset="0.5" stop-color="{middle}"/>
      <stop offset="1" stop-color="{last}"/>
    </linearGradient>
  </defs>
  <rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="6" fill="{theme["bg"]}" stroke="{theme["border"]}"/>
{body}
</svg>
"""


def ring(cx: int, cy: int, radius: int, stroke: str, track: str) -> str:
    """畫一個會從頂端開始描出來的圓環。

    初始值設成畫滿，不支援 SMIL 動畫的環境也會看到完整圓環。
    """
    circumference = 2 * math.pi * radius
    return (
        f'  <circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" stroke="{track}" stroke-width="8"/>\n'
        f'  <circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" stroke="{stroke}" stroke-width="8" '
        f'stroke-linecap="round" stroke-dasharray="{circumference:.2f}" stroke-dashoffset="0" '
        f'transform="rotate(-90 {cx} {cy})">\n'
        f'    <animate attributeName="stroke-dashoffset" values="{circumference:.2f};0" dur="1.4s" '
        f'calcMode="spline" keySplines="0.3 0 0.2 1" keyTimes="0;1"/>\n'
        f"  </circle>\n"
    )


def render_stats_card(stats: ProfileStats, theme: dict[str, str]) -> str:
    """總覽卡片：commit、PR、repo、加入年份，右邊圓環是今年的貢獻數。"""
    rows = [
        ("🕓", "Commits", format_number(stats.commits)),
        ("🔀", "Pull Requests", format_number(stats.pull_requests)),
        ("📦", "Repositories", format_number(stats.repositories)),
        ("🗓️", "Member Since", str(stats.member_since)),
    ]
    body = [f'  <text x="24" y="38" class="title">{escape(stats.display_name)}\'s GitHub Stats</text>']
    for index, (icon, label, value) in enumerate(rows):
        y = 78 + index * 28
        delay = 0.15 + index * 0.12
        body.append(
            f'  <g class="fade" style="animation-delay:{delay:.2f}s">'
            f'<text x="24" y="{y}" class="label">{icon}  {label}</text>'
            f'<text x="250" y="{y}" class="value" text-anchor="end">{value}</text></g>'
        )
    body.append(ring(338, 112, 46, "url(#brand)", theme["track"]))
    body.append(f'  <text x="338" y="120" class="big" text-anchor="middle">{format_number(stats.contributions_this_year)}</text>')
    body.append(f'  <text x="338" y="140" class="muted" text-anchor="middle">in {stats.current_year}</text>')
    return svg_document(420, 195, theme, f"{stats.display_name}'s GitHub Stats", "\n".join(body))


def render_languages_card(languages: list[Language], theme: dict[str, str]) -> str:
    """常用語言卡片：上面一條比例長條，下面兩欄圖例。"""
    bar_x, bar_y, bar_width, bar_height = 24, 58, 372, 10
    body = [
        '  <text x="24" y="38" class="title">Top Languages</text>',
        f'  <clipPath id="bar"><rect x="{bar_x}" y="{bar_y}" width="{bar_width}" height="{bar_height}" rx="5"/></clipPath>',
        f'  <g clip-path="url(#bar)"><rect x="{bar_x}" y="{bar_y}" width="{bar_width}" height="{bar_height}" fill="{theme["track"]}"/>',
    ]
    offset = float(bar_x)
    for language in languages:
        width = language.ratio * bar_width
        body.append(f'    <rect x="{offset:.2f}" y="{bar_y}" width="{width:.2f}" height="{bar_height}" fill="{language.color}"/>')
        offset += width
    body.append("  </g>")

    if not languages:
        body.append('  <text x="24" y="104" class="muted">No language data yet</text>')
    for index, language in enumerate(languages):
        x = 24 + (index % 2) * 190
        y = 102 + (index // 2) * 30
        delay = 0.15 + index * 0.1
        body.append(
            f'  <g class="fade" style="animation-delay:{delay:.2f}s">'
            f'<circle cx="{x + 5}" cy="{y - 5}" r="5" fill="{language.color}"/>'
            f'<text x="{x + 16}" y="{y}" class="label">{escape(language.name)} '
            f'<tspan class="muted">{language.ratio * 100:.1f}%</tspan></text></g>'
        )
    return svg_document(420, 195, theme, "Top Languages", "\n".join(body))


def render_streak_card(stats: ProfileStats, theme: dict[str, str]) -> str:
    """連續貢獻卡片：總貢獻、目前連續天數、最長連續天數三欄。"""
    since = format_date(stats.first_contribution) if stats.first_contribution else "—"
    left, middle, right = 142, 425, 708
    body = [
        f'  <line x1="283" y1="32" x2="283" y2="153" stroke="{theme["border"]}"/>',
        f'  <line x1="567" y1="32" x2="567" y2="153" stroke="{theme["border"]}"/>',
        f'  <g class="fade" style="animation-delay:0.15s">'
        f'<text x="{left}" y="86" class="big" text-anchor="middle">{format_number(stats.contributions_total)}</text>'
        f'<text x="{left}" y="118" class="label" text-anchor="middle">Total Contributions</text>'
        f'<text x="{left}" y="142" class="muted" text-anchor="middle">{since} – Present</text></g>',
        ring(middle, 78, 42, theme["fire"], theme["track"]),
        # 火焰底下墊一個背景色圓點，視覺上把圓環頂端切開
        f'  <circle cx="{middle}" cy="36" r="13" fill="{theme["bg"]}"/>',
        f'  <text x="{middle}" y="43" font-size="18" text-anchor="middle">🔥</text>',
        f'  <text x="{middle}" y="88" class="big" text-anchor="middle">{stats.current_streak.length}</text>',
        f'  <text x="{middle}" y="146" class="fire" text-anchor="middle">Current Streak</text>',
        f'  <text x="{middle}" y="166" class="muted" text-anchor="middle">{format_range(stats.current_streak, stats.current_year)}</text>',
        f'  <g class="fade" style="animation-delay:0.3s">'
        f'<text x="{right}" y="86" class="big" text-anchor="middle">{stats.longest_streak.length}</text>'
        f'<text x="{right}" y="118" class="label" text-anchor="middle">Longest Streak</text>'
        f'<text x="{right}" y="142" class="muted" text-anchor="middle">{format_range(stats.longest_streak, stats.current_year)}</text></g>',
    ]
    return svg_document(850, 185, theme, "Contribution Streak", "\n".join(body))


def write_cards(stats: ProfileStats, out_dir: Path) -> list[Path]:
    """把三種卡片的淺色、深色版本都寫出來。

    Args:
        stats: 統計資料。
        out_dir: 輸出資料夾。

    Returns:
        寫出的檔案路徑。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for theme_name, theme in THEMES.items():
        cards = {
            f"stats-{theme_name}.svg": render_stats_card(stats, theme),
            f"languages-{theme_name}.svg": render_languages_card(stats.languages, theme),
            f"streak-{theme_name}.svg": render_streak_card(stats, theme),
        }
        for file_name, content in cards.items():
            path = out_dir / file_name
            path.write_text(content, encoding="utf-8")
            written.append(path)
    return written


def main() -> int:
    """命令列進入點。"""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--user", required=True, help="GitHub 帳號")
    parser.add_argument("--out", default="dist", help="輸出資料夾")
    args = parser.parse_args()

    token = os.environ.get("STATS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("需要 STATS_TOKEN 或 GITHUB_TOKEN 環境變數", file=sys.stderr)
        return 1
    if not os.environ.get("STATS_TOKEN"):
        print("沒有 STATS_TOKEN：只會算公開 repo 的語言與數量", file=sys.stderr)

    stats = collect_stats(token, args.user, datetime.now(timezone.utc).replace(microsecond=0))
    for path in write_cards(stats, Path(args.out)):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
