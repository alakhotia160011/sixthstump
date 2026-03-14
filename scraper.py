from __future__ import annotations

import asyncio
import re
import json
import hmac
import hashlib
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass
class CommentaryEntry:
    over: str        # e.g. "15.3"
    text: str        # the raw commentary text
    entry_hash: str  # for deduplication
    # Structured ball data from API (for accurate stat tracking)
    batsman_runs: int = 0
    total_runs: int = 0
    is_four: bool = False
    is_six: bool = False
    is_wicket: bool = False
    wides: int = 0
    noballs: int = 0
    legbyes: int = 0
    byes: int = 0
    batsman_id: int | None = None
    bowler_id: int | None = None
    batsman_name: str = ""
    bowler_name: str = ""
    innings_number: int = 1


# Akamai EdgeAuth token generation for hs-consumer-api
from config import HSCI_KEY as _HSCI_KEY
if not _HSCI_KEY:
    print("[scraper] WARNING: HSCI_KEY not set - ESPNcricinfo API calls will fail with 403")
_TOKEN_TTL = 60


def _escape_early(s: str) -> str:
    encoded = urllib.parse.quote(s, safe="")
    return re.sub(r"%[0-9A-Fa-f]{2}", lambda m: m.group(0).lower(), encoded)


def _generate_auth_token(url_path_with_query: str) -> str:
    """Generate an Akamai EdgeAuth URL token for the hs-consumer-api."""
    exp = int(time.time()) + _TOKEN_TTL
    visible = [f"exp={exp}"]
    hmac_input = "~".join(visible + [f"url={_escape_early(url_path_with_query)}"])
    h = hmac.new(bytes.fromhex(_HSCI_KEY), hmac_input.encode(), hashlib.sha256)
    visible.append(f"hmac={h.hexdigest()}")
    return "~".join(visible)


_API_BASE = "https://hs-consumer-api.espncricinfo.com"


async def fetch_matches() -> list[dict]:
    """Fetch current matches (live, recent, upcoming) from ESPNcricinfo."""
    async with httpx.AsyncClient(
        http2=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Origin": "https://www.espncricinfo.com",
            "Referer": "https://www.espncricinfo.com/",
        },
        follow_redirects=True,
        timeout=15,
    ) as client:
        path = "/v1/pages/matches/current?lang=en"
        token = _generate_auth_token(path)
        url = f"{_API_BASE}{path}"
        try:
            resp = await client.get(url, headers={"x-hsci-auth-token": token})
            if resp.status_code != 200:
                print(f"[scraper] matches API HTTP {resp.status_code}")
                return []
            data = resp.json()
        except Exception as e:
            print(f"[scraper] matches API error: {e}")
            return []

    raw_matches = data.get("matches", [])
    results = []
    for m in raw_matches:
        state = m.get("state", "")
        # Only include matches with ball-by-ball coverage (or upcoming)
        if not m.get("hasCommentary") and state != "PRE":
            continue

        # Skip Test matches - only support ODIs and T20s
        match_format = (m.get("format") or "").upper()
        if match_format == "TEST":
            continue

        series = m.get("series", {})
        ground = m.get("ground", {})
        teams = m.get("teams", [])

        team_list = []
        for t in teams:
            team = t.get("team", {})
            team_list.append({
                "name": team.get("longName") or team.get("name") or "?",
                "abbreviation": team.get("abbreviation") or "",
                "id": team.get("objectId", ""),
                "score": t.get("score") or "",
                "scoreInfo": t.get("scoreInfo") or "",
            })

        # Skip matches with club/minor teams (no logos available on CDN)
        if any(not t.get("team", {}).get("objectId") or
               int(t.get("team", {}).get("objectId", 0)) > 1000
               for t in teams):
            continue

        # Build the match URL that CricketScraper can consume
        series_slug = series.get("slug", "")
        series_id = series.get("objectId", "")
        match_slug = m.get("slug", "")
        match_id = m.get("objectId", "")
        match_url = f"https://www.espncricinfo.com/series/{series_slug}-{series_id}/{match_slug}-{match_id}/live-cricket-score"

        results.append({
            "matchId": match_id,
            "seriesId": series_id,
            "url": match_url,
            "state": state,
            "format": m.get("format", ""),
            "title": m.get("title", ""),
            "statusText": m.get("statusText", ""),
            "series": series.get("longName") or series.get("name", ""),
            "venue": ground.get("name", ""),
            "teams": team_list,
            "startTime": m.get("startTime", ""),
        })

    return results


class CricketScraper:
    """Scrapes live ball-by-ball commentary from ESPNcricinfo."""

    def __init__(self, match_url: str):
        self.match_url = match_url.split("?")[0].split("#")[0].rstrip("/")
        self.seen_hashes: set[str] = set()
        self._client: httpx.AsyncClient | None = None
        self._innings: list[dict] = []
        self._match: dict = {}
        self._series_id: str | None = None
        self._match_id: str | None = None

    def _extract_ids(self) -> bool:
        """Extract seriesId and matchId from the URL."""
        # URL pattern: /series/<slug>-<seriesId>/<slug>-<matchId>/...
        m = re.search(r"/series/[^/]+-(\d+)/[^/]+-(\d+)", self.match_url)
        if m:
            self._series_id = m.group(1)
            self._match_id = m.group(2)
            return True
        print("[scraper] could not extract series/match IDs from URL")
        return False

    async def start(self):
        """Initialise the HTTP client."""
        self._client = httpx.AsyncClient(
            http2=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://www.espncricinfo.com",
                "Referer": "https://www.espncricinfo.com/",
            },
            follow_redirects=True,
            timeout=20,
        )
        if not self._extract_ids():
            raise ValueError(f"Could not extract series/match IDs from URL: {self.match_url}")
        print(f"[scraper] fetching commentary from ESPNcricinfo")
        print(f"[scraper] series={self._series_id} match={self._match_id}")

    async def _api_get(self, path: str, params: dict) -> dict | None:
        """Make an authenticated request to hs-consumer-api."""
        if not self._client:
            return None
        query = urllib.parse.urlencode(params)
        full_path = f"{path}?{query}"
        token = _generate_auth_token(full_path)
        url = f"{_API_BASE}{full_path}"
        try:
            resp = await self._client.get(
                url,
                headers={"x-hsci-auth-token": token},
            )
            if resp.status_code != 200:
                print(f"[scraper] API HTTP {resp.status_code} for {path}")
                return None
            return resp.json()
        except Exception as e:
            print(f"[scraper] API error: {e}")
            return None

    async def get_match_intro(self) -> str:
        """Return a rich description of the match for generating a spoken intro."""
        m = self._match
        if not m:
            return ""

        series = m.get("series", {})
        ground = m.get("ground", {})
        teams_data = m.get("teams", [])

        team_names = []
        captains = []
        for t in teams_data:
            team = t.get("team", {})
            team_names.append(team.get("longName", team.get("name", "?")))
            cap = t.get("captain", {})
            if cap:
                captains.append(cap.get("longName", "?"))

        lines = []
        # Series & match title
        series_name = series.get("longName") or series.get("name") or ""
        title = m.get("title", "")
        if series_name:
            lines.append(f"Tournament: {series_name}")
        if title:
            lines.append(f"Match: {title}")

        # Teams
        if team_names:
            lines.append(f"Teams: {' vs '.join(team_names)}")

        # Captains
        for i, cap in enumerate(captains):
            if i < len(team_names):
                lines.append(f"Captain ({team_names[i]}): {cap}")

        # Venue
        if ground:
            venue = ground.get("longName") or ground.get("name") or ""
            capacity = ground.get("capacity", "")
            if venue:
                line = f"Venue: {venue}"
                if capacity:
                    line += f" (capacity: {capacity})"
                lines.append(line)

        # Format & conditions
        fmt = m.get("format", "")
        floodlit = m.get("floodlit", "")
        if fmt:
            lines.append(f"Format: {fmt}")
        if floodlit:
            lines.append(f"Conditions: {floodlit} match")

        return "\n".join(lines)

    def get_player_stats(self, innings_number: int | None = None) -> str:
        """Return formatted player stats for commentary filler.

        If innings_number is given, returns stats for that innings only.
        Otherwise returns stats for the current/last innings.
        """
        target_inn = None
        if innings_number is not None:
            for inn in self._innings:
                if inn.get("inningNumber") == innings_number:
                    target_inn = inn
                    break
        if target_inn is None:
            # Use current or last innings
            for inn in self._innings:
                if inn.get("isCurrent"):
                    target_inn = inn
                    break
            if target_inn is None and self._innings:
                target_inn = self._innings[-1]
        if not target_inn:
            return ""

        lines = []
        team = target_inn.get("team", {}).get("abbreviation", "?")
        lines.append(f"=== {team} Innings ===")

        # Batsmen stats
        batsmen = target_inn.get("inningBatsmen", [])
        active_batsmen = [b for b in batsmen if b.get("battedType") == "yes"]
        if active_batsmen:
            lines.append("Batting:")
            for b in active_batsmen:
                p = b.get("player", {})
                name = p.get("longName", "?")
                runs = b.get("runs", 0)
                balls = b.get("balls", 0)
                fours = b.get("fours", 0)
                sixes = b.get("sixes", 0)
                sr = b.get("strikerate", 0)
                out = b.get("isOut", False)
                status = ""
                if out:
                    dt = b.get("dismissalText", {})
                    status = f" - {dt.get('long', 'out')}"
                else:
                    status = " *not out*"
                lines.append(f"  {name}: {runs}({balls}) [4s:{fours} 6s:{sixes} SR:{sr}]{status}")

        # Bowler stats
        bowlers = target_inn.get("inningBowlers", [])
        active_bowlers = [bw for bw in bowlers if bw.get("bowledType") == "yes"]
        if active_bowlers:
            lines.append("Bowling:")
            for bw in active_bowlers:
                p = bw.get("player", {})
                name = p.get("longName", "?")
                o = bw.get("overs", "?")
                m = bw.get("maidens", 0)
                c = bw.get("conceded", "?")
                w = bw.get("wickets", 0)
                econ = bw.get("economy", "?")
                dots = bw.get("dots", 0)
                lines.append(f"  {name}: {o}-{m}-{c}-{w} (econ {econ}, dots {dots})")

        # Partnerships
        partnerships = target_inn.get("inningPartnerships", [])
        if partnerships:
            lines.append("Partnerships:")
            for i, p in enumerate(partnerships):
                p_runs = p.get("runs", "?")
                p_balls = p.get("balls", "?")
                p1 = p.get("player1", {}).get("longName", "?")
                p2 = p.get("player2", {}).get("longName", "?")
                lines.append(f"  {i+1}. {p1} & {p2}: {p_runs} runs ({p_balls} balls)")

        # Fall of wickets
        fow = target_inn.get("inningFallOfWickets", [])
        if fow:
            lines.append("Fall of wickets:")
            for f in fow:
                wkt_num = f.get("fowWicketNum", "?")
                fow_runs = f.get("fowRuns", "?")
                fow_overs = f.get("fowOvers", "?")
                lines.append(f"  {wkt_num}/{fow_runs} (ov {fow_overs})")

        return "\n".join(lines)

    def get_current_player_stats(self, ball_text: str = "", innings_number: int | None = None) -> str:
        """Return stats for the batsman on strike and the current bowler - for filler commentary."""
        # Try to extract bowler/batsman names from the ball text (format: "Bowler to Batsman, ...")
        bowler_name = ""
        batsman_name = ""
        if " to " in ball_text:
            parts = ball_text.split(",")[0]
            names = parts.split(" to ")
            if len(names) == 2:
                bowler_name = names[0].strip()
                batsman_name = names[1].strip()

        # Find target innings
        current = None
        if innings_number is not None:
            for inn in self._innings:
                if inn.get("inningNumber") == innings_number:
                    current = inn
                    break
        if current is None:
            for inn in self._innings:
                if inn.get("isCurrent"):
                    current = inn
                    break
        if not current and self._innings:
            current = self._innings[-1]
        if not current:
            return ""

        lines = []

        def _name_matches(query: str, player: dict) -> bool:
            """Fuzzy match: 'Ferguson' matches 'Lockie Ferguson', 'Ishan' matches 'Ishan Kishan'."""
            q = query.lower()
            for field in ["longName", "name", "battingName", "fieldingName", "mobileName"]:
                val = player.get(field, "")
                if val and q in val.lower():
                    return True
            return False

        # Find batsman stats
        for b in current.get("inningBatsmen", []):
            p = b.get("player", {})
            name = p.get("longName", "")
            matched = batsman_name and _name_matches(batsman_name, p)
            if matched or (b.get("isOnStrike") and not batsman_name):
                runs = b.get("runs", 0)
                balls = b.get("balls", 0)
                fours = b.get("fours", 0)
                sixes = b.get("sixes", 0)
                sr = b.get("strikerate", 0)
                lines.append(f"On strike - {name}: {runs}({balls}) [4s:{fours} 6s:{sixes} SR:{sr}]")
                break

        # Find bowler stats
        for bw in current.get("inningBowlers", []):
            p = bw.get("player", {})
            name = p.get("longName", "")
            matched = bowler_name and _name_matches(bowler_name, p)
            if matched or (bw.get("isCurrentBowler") and not bowler_name):
                o = bw.get("overs", "?")
                c = bw.get("conceded", "?")
                w = bw.get("wickets", 0)
                econ = bw.get("economy", "?")
                dots = bw.get("dots", 0)
                lines.append(f"Bowling - {name}: {o}-{c}-{w} (econ {econ}, {dots} dots)")
                break

        # Current partnership
        partnerships = current.get("inningPartnerships", [])
        if partnerships:
            p = partnerships[-1]
            p_runs = p.get("runs", "?")
            p_balls = p.get("balls", "?")
            p1 = p.get("player1", {}).get("longName", "?")
            p2 = p.get("player2", {}).get("longName", "?")
            lines.append(f"Partnership - {p1} & {p2}: {p_runs}({p_balls})")

        return "\n".join(lines)

    async def get_new_entries(self) -> list[CommentaryEntry]:
        """Fetch the latest commentary page and return any new entries."""
        if not self._client or not self._series_id:
            return []

        data = await self._api_get("/v1/pages/match/commentary", {
            "lang": "en",
            "seriesId": self._series_id,
            "matchId": self._match_id,
            "sortDirection": "DESC",
        })
        if data is None:
            return []

        content = data.get("content", {})
        self._match = data.get("match", {})
        self._innings = content.get("innings", [])
        comments = content.get("comments", [])

        entries = self._parse_comments(comments)

        # Filter to only new entries
        new_entries = []
        for entry in entries:
            if entry.entry_hash not in self.seen_hashes:
                self.seen_hashes.add(entry.entry_hash)
                new_entries.append(entry)

        # API returns DESC (newest first), reverse to chronological order
        new_entries.reverse()

        if new_entries:
            print(f"[scraper] found {len(new_entries)} new entries")

        return new_entries

    async def get_all_entries(self) -> list[CommentaryEntry]:
        """Fetch ALL commentary for the match by paginating through every innings."""
        if not self._client or not self._series_id:
            return []

        # First, get the initial page to learn innings info
        data = await self._api_get("/v1/pages/match/commentary", {
            "lang": "en",
            "seriesId": self._series_id,
            "matchId": self._match_id,
            "sortDirection": "DESC",
        })
        if data is None:
            return []

        content = data.get("content", {})
        self._match = data.get("match", {})
        self._innings = content.get("innings", [])

        # Determine which innings exist
        innings_numbers = []
        for inn in self._innings:
            inn_num = inn.get("inningNumber")
            if inn_num:
                innings_numbers.append(inn_num)
        if not innings_numbers:
            innings_numbers = [1]

        all_entries = []

        # Fetch all innings in parallel for speed
        print(f"[scraper] fetching {len(innings_numbers)} innings in parallel...")
        innings_results = await asyncio.gather(
            *[self._fetch_full_innings(inn_num) for inn_num in innings_numbers]
        )
        for inn_num, inn_entries in zip(innings_numbers, innings_results):
            # API returns DESC (newest first), reverse to chronological ASC
            inn_entries.reverse()
            all_entries.extend(inn_entries)
            print(f"[scraper] innings {inn_num}: {len(inn_entries)} balls")

        # Mark all as seen
        for entry in all_entries:
            self.seen_hashes.add(entry.entry_hash)

        print(f"[scraper] total: {len(all_entries)} balls across {len(innings_numbers)} innings")
        return all_entries

    async def _fetch_full_innings(self, innings_number: int) -> list[CommentaryEntry]:
        """Paginate through all commentary for a single innings."""
        all_comments: list[dict] = []

        # First page: use the main commentary endpoint with inningNumber
        params = {
            "lang": "en",
            "seriesId": self._series_id,
            "matchId": self._match_id,
            "inningNumber": str(innings_number),
            "commentType": "ALL",
            "sortDirection": "DESC",
        }

        data = await self._api_get("/v1/pages/match/comments", params)
        if data is None:
            return []

        comments = data.get("comments", [])
        all_comments.extend(comments)
        next_over = data.get("nextInningOver")

        # Paginate
        page = 1
        while next_over is not None:
            page += 1
            params["fromInningOver"] = str(next_over)
            data = await self._api_get("/v1/pages/match/comments", params)
            if data is None:
                break
            comments = data.get("comments", [])
            if not comments:
                break
            all_comments.extend(comments)
            next_over = data.get("nextInningOver")

        return self._parse_comments(all_comments)

    async def get_match_context(self) -> str:
        """Return rich match context for the LLM - score, batsmen, bowler, run rate, etc."""
        lines = []

        # Match format, series, teams
        fmt = self._match.get("format", "")
        series = self._match.get("series", {})
        series_name = series.get("longName") or series.get("name") or ""
        teams_data = self._match.get("teams", [])
        team_names = [t.get("team", {}).get("longName", t.get("team", {}).get("name", "")) for t in teams_data]
        info_parts = []
        if fmt:
            info_parts.append(f"Format: {fmt}")
        if series_name:
            info_parts.append(f"Series: {series_name}")
        if team_names:
            info_parts.append(f"Teams: {' vs '.join(team_names)}")
        if info_parts:
            lines.append(" | ".join(info_parts))

        # Match status
        status = self._match.get("statusText", "")
        if status:
            lines.append(f"Match status: {status}")

        # Scores for each innings
        for inn in self._innings:
            team = inn.get("team", {}).get("abbreviation", "???")
            runs = inn.get("runs", "?")
            wickets = inn.get("wickets", "?")
            overs = inn.get("overs", "?")
            target = inn.get("target")
            line = f"{team} {runs}/{wickets} ({overs} ov)"
            if target:
                line += f" | Target: {target}"
            lines.append(line)

        # Current innings detail
        current = None
        for inn in self._innings:
            if inn.get("isCurrent"):
                current = inn
                break
        if not current and self._innings:
            current = self._innings[-1]

        if current:
            # Run rate
            run_rate = current.get("runRate")
            req_rate = current.get("requiredRunRate")
            if run_rate:
                lines.append(f"Run rate: {run_rate}")
            if req_rate:
                lines.append(f"Required rate: {req_rate}")

            # Current batsmen at crease
            batsmen = []
            for b in current.get("inningBatsmen", []):
                if b.get("runs") is not None and not b.get("dismissalText"):
                    p = b.get("player", {})
                    name = p.get("longName", "?")
                    runs = b.get("runs", 0)
                    balls = b.get("balls", 0)
                    fours = b.get("fours", 0)
                    sixes = b.get("sixes", 0)
                    strike = "*" if b.get("isOnStrike") else ""
                    batsmen.append(f"{name}{strike} {runs}({balls}) [{fours}x4, {sixes}x6]")
            if batsmen:
                lines.append("Batting: " + " & ".join(batsmen[-2:]))

            # Current bowler
            for bw in current.get("inningBowlers", []):
                if bw.get("isCurrentBowler"):
                    p = bw.get("player", {})
                    name = p.get("longName", "?")
                    o = bw.get("overs", "?")
                    m = bw.get("maidens", 0)
                    c = bw.get("conceded", "?")
                    w = bw.get("wickets", 0)
                    econ = bw.get("economy", "?")
                    lines.append(f"Bowling: {name} {o}-{m}-{c}-{w} (econ {econ})")
                    break

            # Last partnership
            partnerships = current.get("inningPartnerships", [])
            if partnerships:
                p = partnerships[-1]
                p_runs = p.get("runs")
                p_balls = p.get("balls")
                if p_runs is not None:
                    lines.append(f"Partnership: {p_runs} runs ({p_balls} balls)")

        return "\n".join(lines)

    async def stop(self):
        """Clean up the HTTP client."""
        if self._client:
            await self._client.aclose()
        print("[scraper] stopped")

    # internal helpers

    def _parse_comments(self, comments: list[dict]) -> list[CommentaryEntry]:
        """Convert raw ESPNcricinfo comment objects into CommentaryEntry list."""
        entries = []
        for c in comments:
            over = str(c.get("oversActual", c.get("oversUnique", "?")))
            title = c.get("title", "")  # e.g. "Bumrah to Smith"

            # Build the text from title + commentary HTML
            text_parts = []
            if title:
                text_parts.append(f"{title},")

            # Main commentary text
            for item in c.get("commentTextItems", []) or []:
                html = item.get("html", "")
                if isinstance(html, dict):
                    html = html.get("value", "")
                # Strip HTML tags
                clean = re.sub(r"<[^>]+>", "", str(html)).strip()
                if clean:
                    text_parts.append(clean)

            # Dismissal info
            dismissal = c.get("dismissalText", {})
            if dismissal and dismissal.get("commentary"):
                text_parts.append(f"WICKET! {dismissal['commentary']}")

            text = " ".join(text_parts).strip()
            if not text or len(text) < 5:
                continue

            # Extract bowler/batsman names from title "Bowler to Batsman"
            bowler_name = ""
            batsman_name = ""
            if " to " in title:
                parts = title.split(" to ")
                if len(parts) == 2:
                    bowler_name = parts[0].strip()
                    batsman_name = parts[1].strip()

            entry_hash = hashlib.md5(f"{c.get('id', '')}:{over}".encode()).hexdigest()
            entries.append(CommentaryEntry(
                over=over,
                text=text,
                entry_hash=entry_hash,
                batsman_runs=c.get("batsmanRuns", 0) or 0,
                total_runs=c.get("totalRuns", 0) or 0,
                is_four=bool(c.get("isFour")),
                is_six=bool(c.get("isSix")),
                is_wicket=bool(c.get("isWicket")),
                wides=c.get("wides", 0) or 0,
                noballs=c.get("noballs", 0) or 0,
                legbyes=c.get("legbyes", 0) or 0,
                byes=c.get("byes", 0) or 0,
                batsman_id=c.get("batsmanPlayerId"),
                bowler_id=c.get("bowlerPlayerId"),
                batsman_name=batsman_name,
                bowler_name=bowler_name,
                innings_number=c.get("inningNumber", 1),
            ))

        return entries
