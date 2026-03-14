"""Ball-by-ball stat tracker for replay mode.

Uses structured data from the ESPNcricinfo API (runs, fours, sixes, wickets)
to accumulate accurate point-in-time stats, avoiding end-of-match data leaks.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BatsmanStats:
    name: str
    runs: int = 0
    balls: int = 0
    fours: int = 0
    sixes: int = 0
    is_out: bool = False

    @property
    def strike_rate(self) -> float:
        return round(self.runs / self.balls * 100, 1) if self.balls else 0.0

    def __str__(self) -> str:
        status = " *out*" if self.is_out else " *not out*"
        return f"{self.name}: {self.runs}({self.balls}) [4s:{self.fours} 6s:{self.sixes} SR:{self.strike_rate}]{status}"


@dataclass
class BowlerStats:
    name: str
    balls: int = 0  # legal deliveries
    runs_conceded: int = 0
    wickets: int = 0
    maidens: int = 0
    dots: int = 0
    _current_over_runs: int = 0
    _current_over_balls: int = 0

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
    total_runs: int = 0
    total_wickets: int = 0
    total_balls: int = 0  # legal deliveries
    batsmen: dict[str, BatsmanStats] = field(default_factory=dict)
    bowlers: dict[str, BowlerStats] = field(default_factory=dict)
    current_striker: str = ""
    current_bowler: str = ""
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
    """Accumulates stats ball-by-ball using structured API data during replay."""

    def __init__(self):
        self.innings: dict[int, InningsTracker] = {}
        self._current_innings = 1
        self.match_info: str = ""  # format, series, teams - set once at start

    def set_innings(self, innings_number: int):
        self._current_innings = innings_number
        if innings_number not in self.innings:
            self.innings[innings_number] = InningsTracker()

    def process_entry(self, entry):
        """Update stats from a CommentaryEntry with structured API data."""
        if self._current_innings not in self.innings:
            self.innings[self._current_innings] = InningsTracker()
        inn = self.innings[self._current_innings]

        batsman_name = entry.batsman_name
        bowler_name = entry.bowler_name
        if not batsman_name or not bowler_name:
            return

        # Get or create batsman
        bat_key = batsman_name.lower()
        if bat_key not in inn.batsmen:
            inn.batsmen[bat_key] = BatsmanStats(name=batsman_name)
        batsman = inn.batsmen[bat_key]
        inn.current_striker = bat_key

        # Get or create bowler
        bowl_key = bowler_name.lower()
        if bowl_key not in inn.bowlers:
            inn.bowlers[bowl_key] = BowlerStats(name=bowler_name)
        bowler = inn.bowlers[bowl_key]
        inn.current_bowler = bowl_key

        # Legal delivery? (wides and no-balls don't count as balls faced)
        is_legal = entry.wides == 0 and entry.noballs == 0

        if is_legal:
            batsman.balls += 1
            inn.total_balls += 1
            bowler.balls += 1
            bowler._current_over_balls += 1

        # Batsman runs (from API - accurate)
        batsman.runs += entry.batsman_runs

        if entry.is_four:
            batsman.fours += 1
        if entry.is_six:
            batsman.sixes += 1

        # Total runs and bowler conceded (includes extras)
        inn.total_runs += entry.total_runs
        bowler.runs_conceded += entry.total_runs

        # Dots
        if entry.total_runs == 0 and is_legal:
            bowler.dots += 1

        # Wicket
        if entry.is_wicket:
            batsman.is_out = True
            bowler.wickets += 1
            inn.total_wickets += 1
            inn.fall_of_wickets.append({
                "wicket": inn.total_wickets,
                "runs": inn.total_runs,
                "overs": entry.over,
            })

        # Maiden tracking: at end of over (ball 6), check if 0 runs in the over
        over_parts = entry.over.split(".")
        if len(over_parts) == 2 and over_parts[1] == "6":
            bowler._current_over_runs += entry.total_runs
            if bowler._current_over_runs == 0 and bowler._current_over_balls >= 6:
                bowler.maidens += 1
            bowler._current_over_runs = 0
            bowler._current_over_balls = 0
        else:
            bowler._current_over_runs += entry.total_runs

    def get_current_player_stats(self, ball_text: str = "", innings_number: int | None = None) -> str:
        """Get point-in-time stats for current batsman and bowler."""
        inn_num = innings_number or self._current_innings
        inn = self.innings.get(inn_num)
        if not inn:
            return ""

        lines = []

        # Try to find batsman/bowler from ball text
        bat_key = inn.current_striker
        bowl_key = inn.current_bowler

        if " to " in ball_text:
            parts = ball_text.split(",")[0]
            names = parts.split(" to ")
            if len(names) == 2:
                bowl_key = names[0].strip().lower()
                bat_key = names[1].strip().lower()

        if bat_key and bat_key in inn.batsmen:
            lines.append(f"On strike - {inn.batsmen[bat_key]}")

        if bowl_key and bowl_key in inn.bowlers:
            lines.append(f"Bowling - {inn.bowlers[bowl_key]}")

        return "\n".join(lines)

    def get_player_stats(self, innings_number: int | None = None) -> str:
        """Get full point-in-time stats for an innings."""
        inn_num = innings_number or self._current_innings
        inn = self.innings.get(inn_num)
        if not inn:
            return ""

        lines = [f"=== Innings {inn_num} - {inn.total_runs}/{inn.total_wickets} ({inn.overs} ov, RR: {inn.run_rate}) ==="]

        active = [b for b in inn.batsmen.values() if b.balls > 0]
        if active:
            lines.append("Batting:")
            for b in active:
                lines.append(f"  {b}")

        bowlers = [bw for bw in inn.bowlers.values() if bw.balls > 0]
        if bowlers:
            lines.append("Bowling:")
            for bw in bowlers:
                lines.append(f"  {bw}")

        if inn.fall_of_wickets:
            lines.append("Fall of wickets:")
            for f in inn.fall_of_wickets:
                lines.append(f"  {f['wicket']}/{f['runs']} (ov {f['overs']})")

        return "\n".join(lines)

    def get_match_context(self, innings_number: int | None = None) -> str:
        """Get accumulated match context string."""
        lines = []
        if self.match_info:
            lines.append(self.match_info)
        for inn_num in sorted(self.innings.keys()):
            inn = self.innings[inn_num]
            lines.append(f"Innings {inn_num}: {inn.total_runs}/{inn.total_wickets} ({inn.overs} ov, RR: {inn.run_rate})")
        return "\n".join(lines)
