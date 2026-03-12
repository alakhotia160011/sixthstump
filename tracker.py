"""Ball-by-ball stat tracker for replay mode.

Parses commentary text to accumulate point-in-time stats,
avoiding the "future information" leak from end-of-match API data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class BatsmanStats:
    name: str
    runs: int = 0
    balls: int = 0
    fours: int = 0
    sixes: int = 0
    is_out: bool = False
    dismissal: str = ""

    @property
    def strike_rate(self) -> float:
        return round(self.runs / self.balls * 100, 1) if self.balls else 0.0

    def __str__(self) -> str:
        status = f" — {self.dismissal}" if self.is_out else " *not out*"
        return f"{self.name}: {self.runs}({self.balls}) [4s:{self.fours} 6s:{self.sixes} SR:{self.strike_rate}]{status}"


@dataclass
class BowlerStats:
    name: str
    balls: int = 0  # legal deliveries
    runs_conceded: int = 0
    wickets: int = 0
    maidens: int = 0
    dots: int = 0
    _current_over_runs: int = 0  # for maiden tracking

    @property
    def overs(self) -> str:
        complete = self.balls // 6
        remaining = self.balls % 6
        return f"{complete}.{remaining}" if remaining else str(complete)

    @property
    def economy(self) -> float:
        overs_dec = self.balls / 6
        return round(self.runs_conceded / overs_dec, 1) if overs_dec else 0.0

    def __str__(self) -> str:
        return f"{self.name}: {self.overs}-{self.maidens}-{self.runs_conceded}-{self.wickets} (econ {self.economy}, {self.dots} dots)"


@dataclass
class InningsTracker:
    team: str = ""
    total_runs: int = 0
    total_wickets: int = 0
    total_balls: int = 0  # legal deliveries
    batsmen: dict[str, BatsmanStats] = field(default_factory=dict)
    bowlers: dict[str, BowlerStats] = field(default_factory=dict)
    current_striker: str = ""
    current_bowler: str = ""
    partnerships: list[dict] = field(default_factory=list)
    fall_of_wickets: list[dict] = field(default_factory=list)

    @property
    def overs(self) -> str:
        complete = self.total_balls // 6
        remaining = self.total_balls % 6
        return f"{complete}.{remaining}" if remaining else str(complete)

    @property
    def run_rate(self) -> float:
        overs_dec = self.total_balls / 6
        return round(self.total_runs / overs_dec, 2) if overs_dec else 0.0


class ReplayStatTracker:
    """Accumulates stats ball-by-ball from commentary text during replay mode."""

    def __init__(self):
        self.innings: dict[int, InningsTracker] = {}
        self._current_innings = 1

    def set_innings(self, innings_number: int):
        self._current_innings = innings_number
        if innings_number not in self.innings:
            self.innings[innings_number] = InningsTracker()

    def process_ball(self, over: str, text: str):
        """Parse a ball's commentary and update running stats."""
        if self._current_innings not in self.innings:
            self.innings[self._current_innings] = InningsTracker()
        inn = self.innings[self._current_innings]

        # Parse "Bowler to Batsman, ..." format
        bowler_name, batsman_name, description = self._parse_ball_text(text)

        if not bowler_name or not batsman_name:
            return

        # Get or create batsman
        bat_key = self._normalize(batsman_name)
        if bat_key not in inn.batsmen:
            inn.batsmen[bat_key] = BatsmanStats(name=batsman_name)
        batsman = inn.batsmen[bat_key]
        inn.current_striker = bat_key

        # Get or create bowler
        bowl_key = self._normalize(bowler_name)
        if bowl_key not in inn.bowlers:
            inn.bowlers[bowl_key] = BowlerStats(name=bowler_name)
        bowler = inn.bowlers[bowl_key]
        inn.current_bowler = bowl_key

        # Determine what happened
        desc_lower = description.lower()
        is_wide = "wide" in desc_lower and "wicket" not in desc_lower
        is_noball = "no ball" in desc_lower or "no-ball" in desc_lower
        is_wicket = "wicket" in desc_lower or "out" in desc_lower.split(",")[0] if "," in desc_lower else "wicket" in desc_lower
        is_four = self._is_boundary(desc_lower, 4)
        is_six = self._is_boundary(desc_lower, 6)

        # Check for WICKET more carefully
        is_wicket = "WICKET" in text or "wicket!" in desc_lower

        # Extract runs scored
        runs = self._extract_runs(description, is_wide, is_noball)

        # Legal delivery? (wides and no-balls don't count)
        is_legal = not is_wide and not is_noball

        if is_legal:
            batsman.balls += 1
            inn.total_balls += 1
            bowler.balls += 1

        # Runs
        batsman_runs = runs if not is_wide else 0  # wides don't go to batsman
        if is_noball:
            # No-ball: 1 extra run to team, batsman gets whatever they ran
            noball_batsman_runs = max(0, runs - 1)
            batsman.runs += noball_batsman_runs
        else:
            batsman.runs += batsman_runs

        if is_four:
            batsman.fours += 1
        if is_six:
            batsman.sixes += 1

        inn.total_runs += runs
        bowler.runs_conceded += runs

        if runs == 0 and is_legal:
            bowler.dots += 1

        # Wicket
        if is_wicket:
            batsman.is_out = True
            bowler.wickets += 1
            inn.total_wickets += 1
            inn.fall_of_wickets.append({
                "wicket": inn.total_wickets,
                "runs": inn.total_runs,
                "overs": over,
            })

        # Check for maiden at end of over
        over_parts = over.split(".")
        if len(over_parts) == 2 and over_parts[1] == "6":
            # End of over — check if maiden
            if is_legal:
                bowler._current_over_runs += runs
                if bowler._current_over_runs == 0:
                    bowler.maidens += 1
                bowler._current_over_runs = 0
        elif is_legal:
            bowler._current_over_runs += runs

    def get_current_player_stats(self, ball_text: str = "", innings_number: int | None = None) -> str:
        """Get point-in-time stats for current batsman and bowler."""
        inn_num = innings_number or self._current_innings
        inn = self.innings.get(inn_num)
        if not inn:
            return ""

        lines = []

        # Try to find batsman/bowler from ball text
        bowler_name, batsman_name, _ = self._parse_ball_text(ball_text)

        # Batsman stats
        bat_key = self._normalize(batsman_name) if batsman_name else inn.current_striker
        if bat_key and bat_key in inn.batsmen:
            b = inn.batsmen[bat_key]
            lines.append(f"On strike — {b}")

        # Bowler stats
        bowl_key = self._normalize(bowler_name) if bowler_name else inn.current_bowler
        if bowl_key and bowl_key in inn.bowlers:
            bw = inn.bowlers[bowl_key]
            lines.append(f"Bowling — {bw}")

        return "\n".join(lines)

    def get_player_stats(self, innings_number: int | None = None) -> str:
        """Get full point-in-time stats for an innings."""
        inn_num = innings_number or self._current_innings
        inn = self.innings.get(inn_num)
        if not inn:
            return ""

        lines = [f"=== Innings {inn_num} — {inn.total_runs}/{inn.total_wickets} ({inn.overs} ov, RR: {inn.run_rate}) ==="]

        # Batting
        active = [b for b in inn.batsmen.values() if b.balls > 0]
        if active:
            lines.append("Batting:")
            for b in active:
                lines.append(f"  {b}")

        # Bowling
        bowlers = [bw for bw in inn.bowlers.values() if bw.balls > 0]
        if bowlers:
            lines.append("Bowling:")
            for bw in bowlers:
                lines.append(f"  {bw}")

        # Fall of wickets
        if inn.fall_of_wickets:
            lines.append("Fall of wickets:")
            for f in inn.fall_of_wickets:
                lines.append(f"  {f['wicket']}/{f['runs']} (ov {f['overs']})")

        return "\n".join(lines)

    def get_match_context(self, innings_number: int | None = None) -> str:
        """Get accumulated match context string."""
        lines = []
        for inn_num in sorted(self.innings.keys()):
            inn = self.innings[inn_num]
            lines.append(f"Innings {inn_num}: {inn.total_runs}/{inn.total_wickets} ({inn.overs} ov, RR: {inn.run_rate})")
        return "\n".join(lines)

    # --- parsing helpers ---

    def _parse_ball_text(self, text: str) -> tuple[str, str, str]:
        """Parse 'Bowler to Batsman, description' -> (bowler, batsman, description)."""
        if " to " not in text:
            return "", "", text

        # Split on first comma after "to"
        to_idx = text.index(" to ")
        bowler = text[:to_idx].strip()
        rest = text[to_idx + 4:]

        comma_idx = rest.find(",")
        if comma_idx != -1:
            batsman = rest[:comma_idx].strip()
            description = rest[comma_idx + 1:].strip()
        else:
            batsman = rest.strip()
            description = ""

        return bowler, batsman, description

    def _normalize(self, name: str) -> str:
        """Normalize name for dictionary key."""
        return name.lower().strip()

    def _is_boundary(self, desc: str, value: int) -> bool:
        """Check if the ball was a boundary (4 or 6)."""
        if value == 4:
            return "four" in desc or "boundary" in desc or "4 runs" in desc
        if value == 6:
            return "six" in desc or "6 runs" in desc or "over the rope" in desc or "into the stands" in desc
        return False

    def _extract_runs(self, description: str, is_wide: bool, is_noball: bool) -> int:
        """Extract total runs from the description."""
        desc_lower = description.lower().strip()

        if not desc_lower or "no run" in desc_lower or "dot" in desc_lower:
            if is_wide:
                return 1  # wide = 1 extra
            if is_noball:
                return 1  # no-ball = 1 extra minimum
            return 0

        if "six" in desc_lower or "6 runs" in desc_lower:
            extra = 1 if is_noball else 0
            return 6 + extra

        if "four" in desc_lower or "boundary" in desc_lower or "4 runs" in desc_lower:
            extra = 1 if is_noball else 0
            return 4 + extra

        # Look for "N run(s)" pattern
        m = re.search(r"(\d+)\s*run", desc_lower)
        if m:
            runs = int(m.group(1))
            if is_wide:
                return runs + 1  # wide runs include the extra
            if is_noball:
                return runs + 1
            return runs

        # "single" = 1, "double" = 2, "triple" = 3
        if "single" in desc_lower or "one run" in desc_lower:
            return 1 + (1 if is_wide else 0) + (1 if is_noball else 0)
        if "double" in desc_lower or "two run" in desc_lower or "couple" in desc_lower:
            return 2 + (1 if is_wide else 0) + (1 if is_noball else 0)
        if "triple" in desc_lower or "three run" in desc_lower:
            return 3 + (1 if is_wide else 0) + (1 if is_noball else 0)

        # Wide/no-ball with no runs described = 1 extra
        if is_wide or is_noball:
            return 1

        return 0
