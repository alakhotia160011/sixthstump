from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field

import anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, COMMENTARY_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


@dataclass
class EnhancedCommentary:
    text: str
    emotion: str  # e.g. "excited", "calm", "triumphant"
    speaker: str = "harsha"  # "harsha", "nasser", or "ian"


class CommentaryEnhancer:
    """Takes dry commentary text and makes it dramatic using Claude - three commentator mode."""

    _SPEAKERS = ["harsha", "nasser", "ian"]

    def __init__(self):
        self.client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        self.recent_history: list[str] = []  # track recent for variety (includes speaker tags)
        self.recent_fillers: list[str] = []  # track recent fillers to avoid repetition
        self._filler_topic_queue: list[str] = []  # shuffled topic queue
        self._last_lead: str = ""  # avoid same lead twice in a row

    @property
    def _lead_speaker(self) -> str:
        return self._last_lead or random.choice(self._SPEAKERS)

    def _alternate_lead(self):
        # Pick a random speaker, but never the same one who just led
        choices = [s for s in self._SPEAKERS if s != self._last_lead]
        self._last_lead = random.choice(choices)

    async def _call_claude(self, prompt: str, system: str = COMMENTARY_SYSTEM_PROMPT,
                           max_tokens: int = 300) -> str | None:
        """Call Claude with retry logic. Returns raw text or None on failure."""
        for attempt in range(3):
            try:
                response = await self.client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                )
                if response.content:
                    return response.content[0].text.strip()
                logger.warning("Empty response from Claude")
                return None
            except anthropic.RateLimitError:
                wait = 2 ** attempt * 3
                logger.info("Rate limited, waiting %ds...", wait)
                await asyncio.sleep(wait)
            except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
                logger.warning("Claude API error (attempt %d): %s", attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            except Exception:
                logger.exception("Unexpected Claude API error")
                return None
        return None

    async def enhance(self, raw_text: str, match_context: str = "", over: str = "",
                      player_stats: str = "", ball_data: dict | None = None) -> list[EnhancedCommentary]:
        """Transform a raw commentary entry into vivid dual-commentator spoken commentary."""

        user_prompt = self._build_prompt(raw_text, match_context, over, player_stats, ball_data)

        raw_response = await self._call_claude(user_prompt, max_tokens=400)
        if not raw_response:
            return [EnhancedCommentary(text=raw_text, emotion="neutral", speaker=self._lead_speaker)]

        segments = self._parse_response(raw_response)

        # Track recent outputs with speaker tags
        for seg in segments:
            self.recent_history.append(f"[{seg.speaker.upper()}] {seg.text}")
        if len(self.recent_history) > 10:
            self.recent_history = self.recent_history[-10:]

        self._alternate_lead()
        return segments

    async def generate_innings_break(self, match_context: str) -> list[EnhancedCommentary]:
        """Generate a spoken innings break summary - both commentators discuss."""
        prompt = f"""It's the innings break. Have a conversation about what just happened and set up the chase/second innings.

Match state:
{match_context}

Rules:
- 4-6 sentences total across all of you. Recap the key moments, the score, and what the chasing team needs.
- All three commentators should contribute - one recaps, the others analyze or add opinion.
- Build anticipation for the second innings.
- Reference specific numbers - the total, key performers if you know them.
- Write for SPEECH.

Respond in the same format:
[HARSHA, <emotion>] <commentary>
[NASSER, <emotion>] <commentary>
[IAN, <emotion>] <commentary>"""

        raw_response = await self._call_claude(prompt, max_tokens=500)
        if not raw_response:
            return [EnhancedCommentary(text="", emotion="neutral", speaker="harsha")]

        return self._parse_response(raw_response)

    async def generate_intro(self, match_intro: str, match_context: str = "") -> list[EnhancedCommentary]:
        """Generate a spoken match introduction - both commentators open the broadcast."""
        intro_prompt = f"""You're opening the broadcast together for this match. Set the scene - the tournament, the venue, the teams, the stakes.

Match details:
{match_intro}

{f"Current state: {match_context}" if match_context else ""}

Rules:
- Harsha opens, Nasser and Ian add their takes. Like a real broadcast opening.
- Mention "sixthstump" somewhere - weave it in naturally. Examples: "This is sixthstump, I'm Harsha Bhogle, alongside Nasser Hussain and Ian Smith..." or end with "...stay with us, right here on sixthstump."
- 5-7 sentences total across all three. Punchy, vivid, sets the tone.
- Paint the picture: the stadium, the atmosphere.
- Mention both teams and the stakes naturally.
- End with something that transitions into the action.
- Write for SPEECH. Natural rhythm, dramatic pauses with "..."
- Do NOT use cliché openers like "ladies and gentlemen" or "welcome to".

Respond in the same format:
[HARSHA, <emotion>] <commentary>
[NASSER, <emotion>] <commentary>
[IAN, <emotion>] <commentary>"""

        raw_response = await self._call_claude(intro_prompt, max_tokens=600)
        if not raw_response:
            return [EnhancedCommentary(text="", emotion="neutral", speaker="harsha")]

        return self._parse_response(raw_response)

    # Key overs that deserve an extended summary segment
    # Over numbers are 0-indexed: 0.x = first over, 5.x = sixth over (end of powerplay)
    MILESTONE_OVERS = {5, 9, 14}

    async def generate_score_update(self, over_number: int, match_context: str, current_player_stats: str = "") -> list[EnhancedCommentary]:
        """Generate a quick score update after an over ends."""
        prompt = f"""End of over {over_number}. Give a quick score update.

Match state:
{match_context}

{f"At the crease:{chr(10)}{current_player_stats}" if current_player_stats else ""}

Rules:
- 1-2 sentences. Usually just one commentator for a score update.
- Read out the score, who's at the crease and their scores.
- If it's a chase, mention what's needed.
- Write for SPEECH.

Respond in the same format:
[HARSHA, <emotion>] <commentary>
or
[NASSER, <emotion>] <commentary>
or
[IAN, <emotion>] <commentary>"""

        raw_response = await self._call_claude(prompt, max_tokens=200)
        if not raw_response:
            return [EnhancedCommentary(text="", emotion="neutral", speaker=self._lead_speaker)]

        return self._parse_response(raw_response)

    async def generate_over_summary(self, over_number: int, match_context: str, recent_balls: list[str], player_stats: str = "") -> list[EnhancedCommentary]:
        """Generate a brief over/phase summary at key milestones - both commentators discuss."""
        phase = ""
        if over_number == 6:
            phase = "That's the end of the powerplay!"
        elif over_number == 10:
            phase = "We're at the halfway mark."
        elif over_number == 15:
            phase = "The death overs are approaching."

        recent_text = "\n".join(f"- {b}" for b in recent_balls[-6:]) if recent_balls else ""

        prompt = f"""{phase} Have a conversation about where the batting side stands, the momentum, what's coming next.

Match state:
{match_context}

{f"Player stats:{chr(10)}{player_stats}" if player_stats else ""}

Recent deliveries:
{recent_text}

Rules:
- 3-5 sentences total. Two or three commentators should contribute.
- Reference specific player performances.
- Set up what's coming next.
- Write for SPEECH.

Respond in the same format:
[HARSHA, <emotion>] <commentary>
[NASSER, <emotion>] <commentary>
[IAN, <emotion>] <commentary>"""

        raw_response = await self._call_claude(prompt, max_tokens=400)
        if not raw_response:
            return [EnhancedCommentary(text="", emotion="neutral", speaker=self._lead_speaker)]

        return self._parse_response(raw_response)

    # Rotating filler topics - cycle through these to force variety
    _FILLER_TOPICS = [
        "Focus on the BATSMAN - their score, strike rate, how they're looking, what milestone is next.",
        "Focus on the BOWLER - their figures, economy, dot balls, how they've been bowling this spell.",
        "Talk about the PARTNERSHIP - how many runs, how they're rotating strike, the dynamic between the two batsmen.",
        "Talk about the MATCH SITUATION - the session, the day, what each team needs from here, the tactical battle.",
        "Make a TACTICAL observation - what field changes might come, what bowling change you'd make, where the pressure is.",
        "Talk about a DIFFERENT player - not the current batsman or bowler, but someone else in the innings (a dismissed batsman, a bowler waiting for their next spell).",
    ]

    async def generate_filler(self, match_context: str, recent_balls: list[str], player_stats: str = "") -> list[EnhancedCommentary]:
        """Generate between-balls filler - a conversation between commentators during quiet periods."""
        recent_text = "\n".join(f"- {b}" for b in recent_balls[-6:]) if recent_balls else ""

        # Pick from a shuffled queue - reshuffle when exhausted
        if not self._filler_topic_queue:
            self._filler_topic_queue = list(self._FILLER_TOPICS)
            random.shuffle(self._filler_topic_queue)
        forced_topic = self._filler_topic_queue.pop(0)

        avoid_text = ""
        if self.recent_fillers:
            avoid_text = f"\n\nDo NOT repeat or paraphrase any of these - you already said them:\n" + "\n".join(f"- \"{f}\"" for f in self.recent_fillers[-3:])

        prompt = f"""There's a pause in play. Have a conversation about something interesting.

YOUR TOPIC THIS TIME: {forced_topic}

Match state:
{match_context}

{f"Player stats:{chr(10)}{player_stats}" if player_stats else ""}

Recent deliveries:
{recent_text}{avoid_text}

Rules:
- 2-4 sentences total. Two or three of you should contribute - one raises a point, the others respond.
- STICK TO THE ASSIGNED TOPIC.
- ONLY use numbers and stats that appear EXACTLY in the match state and player stats above. NEVER invent, estimate, or calculate your own numbers. If you're unsure of a stat, talk about it qualitatively instead.
- Make it a real conversation - agree, disagree, build on each other's points.
- Write for SPEECH.

Respond in the same format:
[HARSHA, <emotion>] <commentary>
[NASSER, <emotion>] <commentary>
[IAN, <emotion>] <commentary>"""

        raw_response = await self._call_claude(prompt, max_tokens=300)
        if not raw_response:
            return [EnhancedCommentary(text="", emotion="neutral", speaker=self._lead_speaker)]

        segments = self._parse_response(raw_response)

        for seg in segments:
            if seg.text:
                self.recent_fillers.append(f"[{seg.speaker.upper()}] {seg.text}")
        if len(self.recent_fillers) > 5:
            self.recent_fillers = self.recent_fillers[-5:]

        return segments

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

    # Regex for [SPEAKER, emotion] format
    _DUAL_TAG_RE = re.compile(r'\[(HARSHA|NASSER|IAN),\s*(\w+)\]\s*', re.IGNORECASE)
    # Fallback regex for old [emotion: xxx] format
    _SINGLE_TAG_RE = re.compile(r'\[emotion:\s*(\w+)\]\s*')

    def _parse_response(self, raw: str) -> list[EnhancedCommentary]:
        """Parse dual-commentator response into list of segments."""
        matches = list(self._DUAL_TAG_RE.finditer(raw))

        if matches:
            segments = []
            for i, m in enumerate(matches):
                speaker = m.group(1).lower()
                emotion = m.group(2).lower()
                start = m.end()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
                text = raw[start:end].strip()
                if text:
                    segments.append(EnhancedCommentary(
                        speaker=speaker,
                        text=self._fix_tts_text(text),
                        emotion=emotion,
                    ))
            if segments:
                return segments

        # Fallback: old single-speaker format or untagged text
        emotion_match = self._SINGLE_TAG_RE.match(raw)
        if emotion_match:
            emotion = emotion_match.group(1).lower()
            text = raw[emotion_match.end():].strip()
        else:
            emotion = "neutral"
            text = raw.strip()

        if text:
            return [EnhancedCommentary(
                speaker=self._lead_speaker,
                text=self._fix_tts_text(text),
                emotion=emotion,
            )]
        return [EnhancedCommentary(speaker=self._lead_speaker, text="", emotion="neutral")]

    def _build_prompt(self, raw_text: str, match_context: str, over: str,
                      player_stats: str = "", ball_data: dict | None = None) -> str:
        parts = []

        # Tell Claude who should lead this ball
        lead = self._lead_speaker.upper()
        parts.append(f"Lead commentator this ball: {lead} (describe the action first; the others may add if they have something to say)")

        if match_context:
            parts.append(f"Match situation:\n{match_context}")

        if player_stats:
            parts.append(f"Current players:\n{player_stats}")

        if over:
            parts.append(f"Over: {over}")

        # Structured ball outcome - this is the TRUTH, commentary must match
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
                    outcome_parts.append("Dot ball - no run")
                else:
                    outcome_parts.append(f"{br} run(s) off the bat")
            outcome_parts.append(f"Total runs this ball: {tr}")
            parts.append(f"Ball result (FACTUAL - your commentary MUST match this): {', '.join(outcome_parts)}")

        parts.append(f"Ball update: {raw_text}")

        if self.recent_history:
            last_few = self.recent_history[-4:]
            parts.append(
                "Your last few exchanges (continue the conversation naturally - connect to what was just said):\n"
                + "\n".join(f"- {line}" for line in last_few)
            )

        return "\n\n".join(parts)
