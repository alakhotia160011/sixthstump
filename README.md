# sixthstump

AI-powered live cricket commentary in the voice of Harsha Bhogle. Scrapes ball-by-ball data from ESPNcricinfo, enhances it into dramatic spoken commentary using Claude, and plays it back as audio using Cartesia TTS with a cloned voice.

Think of it as a live cricket podcast that runs itself.

## What it does

- Scrapes ball-by-ball commentary from ESPNcricinfo's API in real-time
- Transforms dry scorecard updates into vivid, Harsha Bhogle-style spoken commentary using Claude
- Converts text to speech with emotion-aware voice synthesis (excited for sixes, calm for dot balls)
- Fills gaps between deliveries with stats, tactical insights, and player analysis
- Delivers score updates every over, milestone summaries (powerplay, halfway, death overs), and innings break narratives

## Modes

**Live mode** — follows a match in progress, polling for new deliveries and commentating as they happen:
```bash
python main.py "https://www.espncricinfo.com/series/..."
```

**Replay mode** — replays a completed match ball-by-ball with full commentary:
```bash
python main.py "https://www.espncricinfo.com/series/..." --replay
```

## Setup

### Prerequisites
- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/)
- A [Cartesia API key](https://play.cartesia.ai/)
- A system with audio output (uses `sounddevice`)

### Install

```bash
git clone https://github.com/aryamaanlakhotia/sixthstump.git
cd sixthstump
pip install -r requirements.txt
```

### Configure

Create a `.env` file:

```
ANTHROPIC_API_KEY=your_anthropic_key
CARTESIA_API_KEY=your_cartesia_key
CARTESIA_VOICE_ID=your_voice_id
```

`CARTESIA_VOICE_ID` is optional — defaults to a built-in voice if not set. You can clone a voice on [Cartesia's playground](https://play.cartesia.ai/) and use that ID.

## Usage

Pass any ESPNcricinfo match URL (ball-by-ball commentary page):

```bash
# Live — follows the match as it happens
python main.py "https://www.espncricinfo.com/series/icc-champions-trophy-2025-1455206/india-vs-new-zealand-2nd-semi-final-1455259/ball-by-ball-commentary"

# Replay — commentates a completed match from ball 1
python main.py "https://www.espncricinfo.com/series/..." --replay

# Custom poll interval (seconds, default 8)
python main.py "https://www.espncricinfo.com/series/..." --poll-interval 5
```

Press `Ctrl+C` to stop.

## How it works

```
ESPNcricinfo API  →  scraper.py   (fetch ball-by-ball data)
                  →  tracker.py   (accumulate live stats)
                  →  enhancer.py  (Claude transforms into Harsha-style commentary)
                  →  tts.py       (Cartesia text-to-speech with emotion)
                  →  player.py    (play audio through speakers)
```

### Commentary structure

| Segment | When | What |
|---|---|---|
| Match intro | Start of broadcast | Sets the scene — teams, venue, stakes |
| Ball-by-ball | Every delivery | Vivid commentary with bowler, striker, ball number |
| Score update | Every over | Team score, batsmen at crease, run rate |
| Filler | Every 3 balls | Stats, tactical insights, player milestones |
| Milestone summary | After overs 6, 10, 15 | Powerplay/halfway/death phase analysis |
| Innings break | Between innings | Recap + chase setup |

### Emotion-aware TTS

Each commentary line is tagged with an emotion (excited, calm, triumphant, etc.) that controls the voice speed and volume:

- A six gets fast, loud, amazed delivery
- A dot ball gets slow, quiet, contemplative delivery
- A wicket gets peak volume, triumphant energy

## Project structure

```
├── main.py          # Entry point, orchestrates the pipeline
├── scraper.py       # ESPNcricinfo API client with auth
├── enhancer.py      # Claude API for commentary generation
├── tracker.py       # Ball-by-ball stat accumulator (replay mode)
├── tts.py           # Cartesia TTS with emotion profiles
├── player.py        # Audio playback via sounddevice
├── config.py        # API keys, model config, system prompt
└── requirements.txt
```

## License

MIT
