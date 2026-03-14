# sixthstump

AI-powered live cricket commentary featuring three iconic voices - Harsha Bhogle, Nasser Hussain, and Ian Smith - talking to each other like a real commentary box. Scrapes ball-by-ball data from ESPNcricinfo, transforms it into dramatic multi-voice commentary using Claude, and plays it back as audio using Cartesia TTS with cloned voices.

Think of it as a live cricket broadcast that runs itself.

## What it does

- Scrapes ball-by-ball commentary from ESPNcricinfo's API in real-time
- Transforms dry scorecard updates into vivid, natural commentary with three distinct personalities
- Three commentators go back and forth - Harsha tells the story, Nasser dissects the tactics, Ian brings the energy
- Emotion-aware voice synthesis per commentator (excited for sixes, calm for dot balls, triumphant for wickets)
- Fills gaps between deliveries with stats, tactical insights, and player analysis - as a real conversation between the three
- Score updates every over, milestone summaries (powerplay, halfway, death overs), and innings break narratives
- Web UI with live scoreboard, speaker badges, and synchronized text + audio playback

## The commentary box

| Commentator | Style | Background |
|---|---|---|
| **Harsha Bhogle** | The storyteller - metaphors, narrative, rhetorical questions, dry wit | India's premier cricket broadcaster, never played international cricket |
| **Nasser Hussain** | The captain - tactical, opinionated, blunt about poor cricket, reads field placements | Former England captain (1999-2003), Sky Sports analyst |
| **Ian Smith** | The livewire - electric energy, rapid-fire calls, builds tension through repetition | Former New Zealand wicketkeeper, made the iconic 2019 World Cup final call |

The lead commentator is randomly selected each ball (never the same twice in a row), and others jump in based on the moment - dot balls get one voice, wickets might get all three.

## Modes

**Web UI** - browse live and completed matches, select one, and get commentary streamed over WebSocket:
```bash
python server.py
# or
uvicorn server:app --host 0.0.0.0 --port 8000
```

**CLI - Live mode** - follows a match in progress, polling for new deliveries:
```bash
python main.py "https://www.espncricinfo.com/series/..."
```

**CLI - Replay mode** - replays a completed match ball-by-ball with full commentary:
```bash
python main.py "https://www.espncricinfo.com/series/..." --replay
```

## Setup

### Prerequisites
- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/)
- A [Cartesia API key](https://play.cartesia.ai/)
- A system with audio output (uses `sounddevice` for CLI, Web Audio API for browser)

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
CARTESIA_VOICE_ID=your_harsha_voice_id
CARTESIA_VOICE_ID_NASSER=your_nasser_voice_id
CARTESIA_VOICE_ID_IAN=your_ian_voice_id
HSCI_KEY=your_hsci_key
```

Each `CARTESIA_VOICE_ID_*` corresponds to a cloned voice on [Cartesia's playground](https://play.cartesia.ai/). Defaults are built in if not set.

## Usage

### Web UI

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` - browse matches, pick one, choose Live or Replay.

### CLI

Pass any ESPNcricinfo match URL:

```bash
# Live - follows the match as it happens
python main.py "https://www.espncricinfo.com/series/icc-champions-trophy-2025-1455206/india-vs-new-zealand-2nd-semi-final-1455259/ball-by-ball-commentary"

# Replay - commentates a completed match from ball 1
python main.py "https://www.espncricinfo.com/series/..." --replay

# Custom poll interval (seconds, default 8)
python main.py "https://www.espncricinfo.com/series/..." --poll-interval 5
```

Press `Ctrl+C` to stop.

## How it works

```
ESPNcricinfo API  →  scraper.py   (fetch ball-by-ball data)
                  →  tracker.py   (accumulate live stats)
                  →  enhancer.py  (Claude generates three-voice commentary)
                  →  tts.py       (Cartesia TTS with per-speaker voice + emotion)
                  →  server.py    (WebSocket streams text + audio to browser)
                  →  player.py    (CLI audio playback via sounddevice)
```

### Commentary structure

| Segment | When | What |
|---|---|---|
| Match intro | Start of broadcast | All three set the scene - teams, venue, stakes |
| Ball-by-ball | Every delivery | 1-3 commentators react based on the moment |
| Score update | Every over | Quick score readout from one commentator |
| Filler | Between deliveries | Two or three discuss stats, tactics, players |
| Milestone summary | After overs 6, 10, 15 | Powerplay/halfway/death phase analysis |
| Innings break | Between innings | All three recap and set up the chase |

### Emotion-aware TTS

Each commentary line is tagged with an emotion (excited, calm, triumphant, etc.) and routed to the correct voice:

- A six gets fast, loud, amazed delivery - Ian likely jumps in
- A dot ball gets slow, quiet, contemplative delivery - just one voice
- A wicket gets peak volume, triumphant energy - two or three react
- Harsha's voice uses Hindi-accented English, Nasser and Ian use British/Kiwi English

## Project structure

```
├── main.py           # CLI entry point
├── server.py         # FastAPI WebSocket server + web UI
├── scraper.py        # ESPNcricinfo API client
├── enhancer.py       # Claude API - three-voice commentary generation
├── tracker.py        # Ball-by-ball stat accumulator (replay mode)
├── tts.py            # Cartesia TTS with per-speaker voice + emotion
├── player.py         # CLI audio playback via sounddevice
├── config.py         # API keys, voice config, system prompt
├── static/
│   └── index.html    # Web UI - match browser, live scoreboard, commentary feed
└── requirements.txt
```

## License

MIT
