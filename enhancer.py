import asyncio
import random
import re
from dataclasses import dataclass

import anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, COMMENTARY_SYSTEM_PROMPT


@dataclass
class EnhancedCommentary:
    text: str
    emotion: str  # e.g. "excited", "calm", "triumphant"


class CommentaryEnhancer:
    """Takes dry commentary text and makes it dramatic using Claude."""

    def __init__(self):
        self.client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        self.recent_history: list[str] = []  # track recent for variety
        self.recent_fillers: list[str] = []  # track recent fillers to avoid repetition
        self._filler_topic_queue: list[str] = []  # shuffled topic queue

    async def enhance(self, raw_text: str, match_context: str = "", over: str = "",
                      player_stats: str = "", ball_data: dict | None = None) -> EnhancedCommentary:
        """Transform a raw commentary entry into vivid spoken commentary."""

        user_prompt = self._build_prompt(raw_text, match_context, over, player_stats, ball_data)

        # Retry with backoff on rate limits
        for attempt in range(3):
            try:
                response = await self.client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=300,
                    system=COMMENTARY_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                break
            except anthropic.RateLimitError:
                wait = 2 ** attempt * 3
                print(f"[enhancer] rate limited, waiting {wait}s...")
                await asyncio.sleep(wait)
        else:
            return EnhancedCommentary(text=raw_text, emotion="neutral")

        raw_response = response.content[0].text.strip()

        # Parse emotion tag and commentary text
        emotion, text = self._parse_response(raw_response)

        # Track recent outputs to avoid repetition
        self.recent_history.append(text)
        if len(self.recent_history) > 10:
            self.recent_history.pop(0)

        return EnhancedCommentary(text=text, emotion=emotion)

    async def generate_innings_break(self, match_context: str) -> EnhancedCommentary:
        """Generate a spoken innings break summary."""
        prompt = f"""It's the innings break. Summarize what just happened and set up the chase/second innings. Build the narrative tension.

Match state:
{match_context}

Rules:
- 3-4 sentences. Recap the key moments, the score, and what the chasing team needs.
- Build anticipation for the second innings.
- Reference specific numbers — the total, key performers if you know them.
- Write for SPEECH. Natural, conversational, Harsha Bhogle style.
- End with something that transitions to the second innings — "Can they chase it down?" energy.

Respond in the same format:
[emotion: <emotion>]
<your innings break commentary here>"""

        for attempt in range(3):
            try:
                response = await self.client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=400,
                    system=COMMENTARY_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                )
                break
            except anthropic.RateLimitError:
                wait = 2 ** attempt * 3
                await asyncio.sleep(wait)
        else:
            return EnhancedCommentary(text="", emotion="neutral")

        raw_response = response.content[0].text.strip()
        emotion, text = self._parse_response(raw_response)
        return EnhancedCommentary(text=text, emotion=emotion)

    async def generate_intro(self, match_intro: str, match_context: str = "") -> EnhancedCommentary:
        """Generate a spoken match introduction."""
        intro_prompt = f"""You are opening the broadcast for this match. Set the scene — the tournament, the venue, the teams, the stakes. Build anticipation. This is the FIRST thing the audience hears.

Match details:
{match_intro}

{f"Current state: {match_context}" if match_context else ""}

Rules:
- Mention "sixthstump" somewhere in the intro — weave it in naturally. Vary how you do it each time. Examples: "This is sixthstump, and I'm Harsha Bhogle..." or "Harsha Bhogle here, on sixthstump, and what a day we have ahead..." or end with "...stay with us, right here on sixthstump." Don't always put it first — sometimes mid-sentence, sometimes at the end.
- 3-5 sentences MAX after the intro line. Punchy, vivid, sets the tone.
- Paint the picture: the stadium, the crowd, the atmosphere.
- Mention both teams and the stakes naturally.
- End with something that transitions into the action — "Let's get started" energy.
- Write for SPEECH, not text. Natural rhythm, dramatic pauses with "..."
- Do NOT use cliché openers like "ladies and gentlemen" or "welcome to".

Respond in the same format:
[emotion: <emotion>]
<your intro here>"""

        for attempt in range(3):
            try:
                response = await self.client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=400,
                    system=COMMENTARY_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": intro_prompt}],
                )
                break
            except anthropic.RateLimitError:
                wait = 2 ** attempt * 3
                await asyncio.sleep(wait)
        else:
            return EnhancedCommentary(text="", emotion="neutral")

        raw_response = response.content[0].text.strip()
        emotion, text = self._parse_response(raw_response)
        return EnhancedCommentary(text=text, emotion=emotion)

    # Key overs that deserve an extended summary segment
    # Over numbers are 0-indexed: 0.x = first over, 5.x = sixth over (end of powerplay)
    MILESTONE_OVERS = {5, 9, 14}

    async def generate_score_update(self, over_number: int, match_context: str, current_player_stats: str = "") -> EnhancedCommentary:
        """Generate a quick score update after an over ends — score, batsmen, run rate."""
        prompt = f"""End of over {over_number}. Give a quick score update for the listener.

Match state:
{match_context}

{f"At the crease:{chr(10)}{current_player_stats}" if current_player_stats else ""}

Rules:
- 1-2 sentences ONLY. Quick and informative.
- Read out the score: "India are X for Y after Z overs"
- Mention who's at the crease and their scores: "Samson on 45 off 30, Kishan on 22 off 15"
- If it's a chase, mention what's needed: "They need X more from Y overs"
- Keep it natural — like a broadcaster giving a quick update between overs.
- Write for SPEECH.

Respond in the same format:
[emotion: <emotion>]
<your score update here>"""

        for attempt in range(3):
            try:
                response = await self.client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=200,
                    system=COMMENTARY_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                )
                break
            except anthropic.RateLimitError:
                wait = 2 ** attempt * 3
                await asyncio.sleep(wait)
        else:
            return EnhancedCommentary(text="", emotion="neutral")

        raw_response = response.content[0].text.strip()
        emotion, text = self._parse_response(raw_response)
        return EnhancedCommentary(text=text, emotion=emotion)

    async def generate_over_summary(self, over_number: int, match_context: str, recent_balls: list[str], player_stats: str = "") -> EnhancedCommentary:
        """Generate a brief over/phase summary at key milestones."""
        phase = ""
        if over_number == 6:
            phase = "That's the end of the powerplay!"
        elif over_number == 10:
            phase = "We're at the halfway mark."
        elif over_number == 15:
            phase = "The death overs are approaching."

        recent_text = "\n".join(f"- {b}" for b in recent_balls[-6:]) if recent_balls else ""

        prompt = f"""{phase} Give a brief phase summary — where the batting side stands, the momentum, what's coming next.

Match state:
{match_context}

{f"Player stats:{chr(10)}{player_stats}" if player_stats else ""}

Recent deliveries:
{recent_text}

Rules:
- 2-3 sentences MAX. This is a quick recap, not a speech.
- Reference specific player performances — who's been the star, who's been expensive, who's held firm.
- Set up what's coming next — "The death overs beckon..." or "Time to accelerate..." type energy.
- Harsha Bhogle style: insightful, not just stats. Tell us WHAT IT MEANS.
- Write for SPEECH.

Respond in the same format:
[emotion: <emotion>]
<your summary here>"""

        for attempt in range(3):
            try:
                response = await self.client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=300,
                    system=COMMENTARY_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                )
                break
            except anthropic.RateLimitError:
                wait = 2 ** attempt * 3
                await asyncio.sleep(wait)
        else:
            return EnhancedCommentary(text="", emotion="neutral")

        raw_response = response.content[0].text.strip()
        emotion, text = self._parse_response(raw_response)
        return EnhancedCommentary(text=text, emotion=emotion)

    # Rotating filler topics — cycle through these to force variety
    _FILLER_TOPICS = [
        "Focus on the BATSMAN — their score, strike rate, how they're looking, what milestone is next.",
        "Focus on the BOWLER — their figures, economy, dot balls, how they've been bowling this spell.",
        "Talk about the PARTNERSHIP — how many runs, how they're rotating strike, the dynamic between the two batsmen.",
        "Talk about the MATCH SITUATION — the session, the day, what each team needs from here, the tactical battle.",
        "Make a TACTICAL observation — what field changes might come, what bowling change you'd make, where the pressure is.",
        "Talk about a DIFFERENT player — not the current batsman or bowler, but someone else in the innings (a dismissed batsman, a bowler waiting for their next spell).",
    ]

    async def generate_filler(self, match_context: str, recent_balls: list[str], player_stats: str = "") -> EnhancedCommentary:
        """Generate between-balls filler — a stat, anecdote, or tactical insight during quiet periods."""
        recent_text = "\n".join(f"- {b}" for b in recent_balls[-6:]) if recent_balls else ""

        # Pick from a shuffled queue — reshuffle when exhausted
        if not self._filler_topic_queue:
            self._filler_topic_queue = list(self._FILLER_TOPICS)
            random.shuffle(self._filler_topic_queue)
        forced_topic = self._filler_topic_queue.pop(0)

        avoid_text = ""
        if self.recent_fillers:
            avoid_text = f"\n\nDo NOT repeat or paraphrase any of these — you already said them:\n" + "\n".join(f"- \"{f}\"" for f in self.recent_fillers[-3:])

        prompt = f"""There's a pause in play. Fill the air with something interesting.

YOUR TOPIC THIS TIME: {forced_topic}

Match state:
{match_context}

{f"Player stats:{chr(10)}{player_stats}" if player_stats else ""}

Recent deliveries:
{recent_text}{avoid_text}

Rules:
- 1-2 sentences ONLY. Quick, natural, conversational.
- STICK TO THE ASSIGNED TOPIC. Do not talk about something else.
- Reference specific numbers from the stats provided.
- Harsha style: the stat is the entry point, tell us what it MEANS.
- Write for SPEECH.

Respond in the same format:
[emotion: <emotion>]
<your filler here>"""

        for attempt in range(3):
            try:
                response = await self.client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=200,
                    system=COMMENTARY_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                )
                break
            except anthropic.RateLimitError:
                wait = 2 ** attempt * 3
                await asyncio.sleep(wait)
        else:
            return EnhancedCommentary(text="", emotion="neutral")

        raw_response = response.content[0].text.strip()
        emotion, text = self._parse_response(raw_response)

        if text:
            self.recent_fillers.append(text)
            if len(self.recent_fillers) > 5:
                self.recent_fillers.pop(0)

        return EnhancedCommentary(text=text, emotion=emotion)

    # Common TTS pronunciation fixes: pattern -> spoken form
    _TTS_FIXES = [
        (re.compile(r'\bT20\b', re.IGNORECASE), 'T twenty'),
        (re.compile(r'\bT20I\b', re.IGNORECASE), 'T twenty I'),
        (re.compile(r'\bODI\b'), 'O D I'),
        (re.compile(r'\bIPL\b'), 'I P L'),
        (re.compile(r'\bWC\b'), 'World Cup'),
        (re.compile(r'\bLBW\b', re.IGNORECASE), 'L B W'),
        (re.compile(r'\bDRS\b'), 'D R S'),
        (re.compile(r'\bRCB\b'), 'R C B'),
        (re.compile(r'\bCSK\b'), 'C S K'),
        (re.compile(r'\bMI\b'), 'M I'),
        (re.compile(r'\bKKR\b'), 'K K R'),
        (re.compile(r'\bSRH\b'), 'S R H'),
        (re.compile(r'\bDC\b'), 'D C'),
        (re.compile(r'\bPBKS\b'), 'P B K S'),
        (re.compile(r'\bGT\b'), 'G T'),
        (re.compile(r'\bLSG\b'), 'L S G'),
        (re.compile(r'\bRR\b'), 'R R'),
    ]

    @classmethod
    def _fix_tts_text(cls, text: str) -> str:
        """Fix abbreviations and terms that TTS mispronounces."""
        for pattern, replacement in cls._TTS_FIXES:
            text = pattern.sub(replacement, text)
        return text

    def _parse_response(self, raw: str) -> tuple:
        """Extract emotion tag and commentary from Claude's response."""
        emotion_match = re.match(r'\[emotion:\s*(\w+)\]\s*', raw)
        if emotion_match:
            emotion = emotion_match.group(1).lower()
            text = raw[emotion_match.end():].strip()
            return emotion, self._fix_tts_text(text)
        return "neutral", self._fix_tts_text(raw)

    def _build_prompt(self, raw_text: str, match_context: str, over: str,
                      player_stats: str = "", ball_data: dict | None = None) -> str:
        parts = []

        if match_context:
            parts.append(f"Match situation:\n{match_context}")

        if player_stats:
            parts.append(f"Current players:\n{player_stats}")

        if over:
            parts.append(f"Over: {over}")

        # Structured ball outcome — this is the TRUTH, commentary must match
        if ball_data:
            outcome_parts = []
            if ball_data.get("isWicket"):
                outcome_parts.append("WICKET!")
            if ball_data.get("isSix"):
                outcome_parts.append("SIX!")
            elif ball_data.get("isFour"):
                outcome_parts.append("FOUR!")
            if ball_data.get("wides"):
                outcome_parts.append(f"Wide ({ball_data['wides']} extra)")
            if ball_data.get("noballs"):
                outcome_parts.append(f"No-ball ({ball_data['noballs']} extra)")
            if ball_data.get("legbyes"):
                outcome_parts.append(f"{ball_data['legbyes']} leg bye(s)")
            if ball_data.get("byes"):
                outcome_parts.append(f"{ball_data['byes']} bye(s)")
            br = ball_data.get("batsmanRuns", 0)
            tr = ball_data.get("totalRuns", 0)
            if not outcome_parts:
                if br == 0 and tr == 0:
                    outcome_parts.append("Dot ball — no run")
                else:
                    outcome_parts.append(f"{br} run(s) off the bat")
            outcome_parts.append(f"Total runs this ball: {tr}")
            parts.append(f"Ball result (FACTUAL — your commentary MUST match this): {', '.join(outcome_parts)}")

        parts.append(f"Ball update: {raw_text}")

        if self.recent_history:
            last_few = self.recent_history[-3:]
            parts.append(
                "Your last few lines (continue the narrative naturally — connect to what you just said, don't restart fresh each ball):\n"
                + "\n".join(f"- {line}" for line in last_few)
            )

        return "\n\n".join(parts)
