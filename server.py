"""FastAPI server for sixthstump — streams commentary + audio over WebSocket."""

import asyncio
import base64
import logging
import re

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

from scraper import CricketScraper, fetch_matches
from enhancer import CommentaryEnhancer
from tts import CommentaryTTS
from tracker import ReplayStatTracker
from config import POLL_INTERVAL, VOICE_CONFIG

logger = logging.getLogger(__name__)

# Only allow ESPNcricinfo match URLs
_VALID_URL_RE = re.compile(r'^https?://(www\.)?espncricinfo\.com/series/.+$')

app = FastAPI(title="sixthstump")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return RedirectResponse("/static/index.html")


@app.get("/api/matches")
async def get_matches():
    """Return current live, recent, and upcoming matches from ESPNcricinfo."""
    matches = await fetch_matches()
    return {"matches": matches}


_ws_locks: dict[int, asyncio.Lock] = {}


async def send_msg(ws: WebSocket, msg: dict):
    """Send JSON, silently ignore if connection closed."""
    lock = _ws_locks.get(id(ws))
    try:
        if lock:
            async with lock:
                await ws.send_json(msg)
        else:
            await ws.send_json(msg)
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        pass
    except Exception:
        logger.exception("Unexpected error sending WebSocket message")


def create_sync_worker(ws: WebSocket, tts: CommentaryTTS, stop_event: asyncio.Event = None):
    """Background worker that sends text + audio together in order.

    Each queue item is (commentary_msg, tts_text, tts_emotion, speaker).
    The worker synthesizes audio with the right voice, then sends the
    commentary message and audio back-to-back so they arrive in sync.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=4)

    async def worker():
        while True:
            item = await queue.get()
            if item is None:
                break
            if stop_event and stop_event.is_set():
                continue  # drain queue without processing

            # Scorecard-only items: (scorecard_msg, None, None, None)
            commentary_msg, tts_text, tts_emotion, speaker = item
            if tts_text is None:
                # Just a scorecard update, send immediately
                await send_msg(ws, commentary_msg)
                continue

            # Synthesize audio with speaker-specific voice
            audio_b64 = None
            voice_cfg = VOICE_CONFIG.get(speaker or "harsha", VOICE_CONFIG["harsha"])
            try:
                pcm = await asyncio.to_thread(
                    tts.synthesize, tts_text, tts_emotion,
                    voice_id=voice_cfg["voice_id"],
                    language=voice_cfg["language"],
                )
                audio_b64 = base64.b64encode(pcm).decode("ascii")
            except Exception:
                if stop_event and stop_event.is_set():
                    continue
                logger.exception("TTS synthesis error")
                await send_msg(ws, {"type": "error", "text": "Audio synthesis failed"})
            if stop_event and stop_event.is_set():
                continue
            # Send text + audio together
            await send_msg(ws, commentary_msg)
            if audio_b64:
                await send_msg(ws, {"type": "audio", "data": audio_b64})

    task = asyncio.create_task(worker())
    return queue, task


async def _queue_segments(sync_queue: asyncio.Queue, segments, tag: str, **extra_fields):
    """Queue each commentary segment (from dual commentator) into the sync worker."""
    for i, seg in enumerate(segments):
        if not seg.text:
            continue
        msg = {"type": "commentary", "tag": tag, "text": seg.text,
               "emotion": seg.emotion, "speaker": seg.speaker, **extra_fields}
        # Only first segment of a ball gets ballData
        if i > 0:
            msg.pop("ballData", None)
        await sync_queue.put((msg, seg.text, seg.emotion, seg.speaker))


def _build_scorecard(tracker: ReplayStatTracker) -> list[dict]:
    """Build a compact scorecard from the tracker for the frontend."""
    result = []
    for inn_num in sorted(tracker.innings.keys()):
        inn = tracker.innings[inn_num]
        result.append({
            "innings": inn_num,
            "runs": inn.total_runs,
            "wickets": inn.total_wickets,
            "overs": inn.overs,
            "runRate": inn.run_rate,
        })
    return result


async def run_replay(ws: WebSocket, scraper: CricketScraper, enhancer: CommentaryEnhancer,
                     tts: CommentaryTTS, stop_event: asyncio.Event):
    """Replay mode: commentate an entire match ball-by-ball."""
    await send_msg(ws, {"type": "status", "text": "Loading match data..."})

    # Fetch all entries (this also populates match metadata)
    entries = await scraper.get_all_entries()
    match_intro = await scraper.get_match_intro()
    tracker = ReplayStatTracker()

    # Set match info so every ball-by-ball context includes format, series, teams
    m = scraper._match or {}
    fmt = m.get("format", "")
    series_name = m.get("series", {}).get("longName") or m.get("series", {}).get("name") or ""
    teams_data_info = m.get("teams", [])
    team_names = [t.get("team", {}).get("longName", t.get("team", {}).get("name", "")) for t in teams_data_info]
    info_parts = []
    if fmt:
        info_parts.append(f"Format: {fmt}")
    if series_name:
        info_parts.append(f"Series: {series_name}")
    if team_names:
        info_parts.append(f"Teams: {' vs '.join(team_names)}")
    tracker.match_info = " | ".join(info_parts)

    await send_msg(ws, {"type": "status", "text": f"Replay mode — {len(entries)} balls"})

    # Send team info for the scoreboard
    teams_data_sb = (scraper._match or {}).get("teams", [])
    team_names_sb = {}
    team_ids_sb = {}
    for i, t in enumerate(teams_data_sb):
        team = t.get("team", {})
        team_names_sb[i + 1] = team.get("abbreviation") or team.get("name") or f"Team {i+1}"
        team_ids_sb[i + 1] = team.get("objectId", "")
    await send_msg(ws, {"type": "teams", "teams": team_names_sb, "teamIds": team_ids_sb})

    # Sync worker — sends text + audio together in order
    sync_queue, sync_task = create_sync_worker(ws, tts, stop_event)

    # Match intro
    if match_intro:
        intro_segments = await enhancer.generate_intro(match_intro)
        await _queue_segments(sync_queue, intro_segments, "intro")

    prev_over = None
    recent_balls: list[str] = []
    summaries_done: set[int] = set()
    current_innings = 1
    tracker.set_innings(current_innings)
    ball_count = 0

    for entry in entries:
        if stop_event.is_set():
            break

        cur_over_num = entry.over.split(".")[0] if "." in entry.over else entry.over
        if prev_over is not None:
            prev_over_num = prev_over.split(".")[0] if "." in prev_over else prev_over
            try:
                prev_int = int(prev_over_num)
                cur_int = int(cur_over_num)

                # Innings break
                if cur_int < prev_int - 2:
                    tracker_context = tracker.get_match_context()
                    break_segments = await enhancer.generate_innings_break(tracker_context)
                    await _queue_segments(sync_queue, break_segments, "break")
                    recent_balls.clear()
                    summaries_done.clear()
                    current_innings += 1
                    tracker.set_innings(current_innings)
                    ball_count = 0

                elif prev_int != cur_int:
                    display_over = prev_int + 1
                    if prev_int in enhancer.MILESTONE_OVERS and prev_int not in summaries_done:
                        summaries_done.add(prev_int)
                        player_stats = tracker.get_player_stats(current_innings)
                        tracker_context = tracker.get_match_context()
                        summary_segments = await enhancer.generate_over_summary(display_over, tracker_context, recent_balls, player_stats)
                        await _queue_segments(sync_queue, summary_segments, "summary", over=f"Over {display_over}")
                    else:
                        current_stats = tracker.get_current_player_stats(entry.text, current_innings)
                        tracker_context = tracker.get_match_context()
                        score_segments = await enhancer.generate_score_update(display_over, tracker_context, current_stats)
                        await _queue_segments(sync_queue, score_segments, "score", over=f"Over {display_over}")
            except ValueError as e:
                logger.warning("Could not parse over number: %s", e)

        prev_over = entry.over
        ball_count += 1
        tracker.process_entry(entry)

        # Send scoreboard through the queue so it stays in sync with commentary
        scorecard_msg = {"type": "scorecard", "innings": _build_scorecard(tracker)}
        await sync_queue.put((scorecard_msg, None, None, None))

        # Ball commentary — text + audio sent together by worker
        live_context = tracker.get_match_context()
        current_stats = tracker.get_current_player_stats(entry.text, current_innings)
        ball_data = {
            "runs": entry.total_runs,
            "batsmanRuns": entry.batsman_runs,
            "totalRuns": entry.total_runs,
            "isFour": entry.is_four,
            "isSix": entry.is_six,
            "isWicket": entry.is_wicket,
            "wides": entry.wides,
            "noballs": entry.noballs,
            "legbyes": entry.legbyes,
            "byes": entry.byes,
        }
        segments = await enhancer.enhance(entry.text, live_context, over=entry.over,
                                          player_stats=current_stats, ball_data=ball_data)
        await _queue_segments(sync_queue, segments, "ball", over=entry.over, ballData=ball_data)

        recent_balls.append(f"[{entry.over}] {entry.text[:80]}")
        if len(recent_balls) > 12:
            recent_balls.pop(0)

        # Filler every 3 balls
        if ball_count % 3 == 0:
            current_stats = tracker.get_current_player_stats(entry.text, current_innings)
            tracker_context = tracker.get_match_context()
            filler_segments = await enhancer.generate_filler(tracker_context, recent_balls, current_stats)
            await _queue_segments(sync_queue, filler_segments, "filler")

    # Wait for remaining items to finish, then shut down worker
    await sync_queue.put(None)
    await sync_task
    await send_msg(ws, {"type": "done"})


def _build_live_scorecard(scraper: CricketScraper) -> list[dict]:
    """Build scorecard from the scraper's live innings data."""
    result = []
    for inn in scraper._innings:
        inn_num = inn.get("inningNumber", len(result) + 1)
        result.append({
            "innings": inn_num,
            "runs": inn.get("runs", 0),
            "wickets": inn.get("wickets", 0),
            "overs": str(inn.get("overs", "0")),
            "runRate": inn.get("runRate", 0),
        })
    return result


async def run_live(ws: WebSocket, scraper: CricketScraper, enhancer: CommentaryEnhancer,
                   tts: CommentaryTTS, stop_event: asyncio.Event):
    """Live mode: poll for new balls and commentate."""
    entries = await scraper.get_new_entries()
    match_context = await scraper.get_match_context()

    # Send team info for the scoreboard
    live_teams_data = (scraper._match or {}).get("teams", [])
    live_team_names = {}
    live_team_ids = {}
    for i, t in enumerate(live_teams_data):
        team = t.get("team", {})
        live_team_names[i + 1] = team.get("abbreviation") or team.get("name") or f"Team {i+1}"
        live_team_ids[i + 1] = team.get("objectId", "")
    await send_msg(ws, {"type": "teams", "teams": live_team_names, "teamIds": live_team_ids})

    # Send initial scorecard
    live_scorecard = _build_live_scorecard(scraper)
    if live_scorecard:
        await send_msg(ws, {"type": "scorecard", "innings": live_scorecard})

    # Sync worker — sends text + audio together in order
    sync_queue, sync_task = create_sync_worker(ws, tts, stop_event)

    match_intro = await scraper.get_match_intro()
    if match_intro:
        intro_segments = await enhancer.generate_intro(match_intro, match_context)
        await _queue_segments(sync_queue, intro_segments, "intro")

    if entries:
        await send_msg(ws, {"type": "status", "text": f"Skipped {len(entries)} existing — waiting for live balls"})

    await send_msg(ws, {"type": "status", "text": f"Polling every {POLL_INTERVAL}s"})

    empty_polls = 0
    prev_over = None
    last_ball_text = ""
    recent_balls: list[str] = []
    summaries_done: set[int] = set()

    while not stop_event.is_set():
        try:
            entries = await scraper.get_new_entries()
            match_context = await scraper.get_match_context()

            # Update scorecard on every poll (score refreshes even between balls)
            live_sc = _build_live_scorecard(scraper)
            if live_sc:
                await send_msg(ws, {"type": "scorecard", "innings": live_sc})

            if entries:
                empty_polls = 0
                for entry in entries:
                    if stop_event.is_set():
                        break

                    cur_over_num = entry.over.split(".")[0] if "." in entry.over else entry.over
                    if prev_over is not None:
                        prev_over_num = prev_over.split(".")[0] if "." in prev_over else prev_over
                        try:
                            prev_int = int(prev_over_num)
                            cur_int = int(cur_over_num)
                            if prev_int != cur_int and prev_int in enhancer.MILESTONE_OVERS and prev_int not in summaries_done:
                                summaries_done.add(prev_int)
                                player_stats = scraper.get_player_stats()
                                summary_segments = await enhancer.generate_over_summary(prev_int, match_context, recent_balls, player_stats)
                                await _queue_segments(sync_queue, summary_segments, "summary")
                        except ValueError as e:
                            logger.warning("Could not parse over number: %s", e)
                    prev_over = entry.over
                    last_ball_text = entry.text

                    # Send updated scorecard through queue (stays in sync)
                    live_sc = _build_live_scorecard(scraper)
                    if live_sc:
                        sc_msg = {"type": "scorecard", "innings": live_sc}
                        await sync_queue.put((sc_msg, None, None, None))

                    current_stats = scraper.get_current_player_stats(entry.text)
                    ball_data = {
                        "runs": entry.total_runs,
                        "batsmanRuns": entry.batsman_runs,
                        "totalRuns": entry.total_runs,
                        "isFour": entry.is_four,
                        "isSix": entry.is_six,
                        "isWicket": entry.is_wicket,
                        "wides": entry.wides,
                        "noballs": entry.noballs,
                        "legbyes": entry.legbyes,
                        "byes": entry.byes,
                    }
                    segments = await enhancer.enhance(entry.text, match_context, over=entry.over,
                                                      player_stats=current_stats, ball_data=ball_data)
                    await _queue_segments(sync_queue, segments, "ball", over=entry.over, ballData=ball_data)

                    recent_balls.append(f"[{entry.over}] {entry.text[:80]}")
                    if len(recent_balls) > 12:
                        recent_balls.pop(0)
            else:
                empty_polls += 1

                # Check match status for breaks/delays
                match_status = ((scraper._match or {}).get("statusText", "") or "").lower()
                break_keywords = ["lunch", "tea", "stumps", "drinks", "break",
                                  "rain", "bad light", "delay", "timeout", "review"]
                is_break = any(kw in match_status for kw in break_keywords)

                if empty_polls == 2 and is_break:
                    # Inform listener about the break
                    status_text = (scraper._match or {}).get("statusText", "")
                    msg = {"type": "commentary", "tag": "status",
                           "text": status_text}
                    await send_msg(ws, msg)
                elif empty_polls == 4 and last_ball_text and not is_break:
                    # First filler — use current player stats
                    current_stats = scraper.get_current_player_stats(last_ball_text)
                    filler_segments = await enhancer.generate_filler(match_context, recent_balls, current_stats)
                    await _queue_segments(sync_queue, filler_segments, "filler")
                elif empty_polls == 10 and last_ball_text and not is_break:
                    # Second filler — use full innings stats for a different angle
                    full_stats = scraper.get_player_stats()
                    filler_segments = await enhancer.generate_filler(match_context, recent_balls, full_stats)
                    await _queue_segments(sync_queue, filler_segments, "filler")

        except Exception as e:
            logger.exception("Error in live polling loop")
            await send_msg(ws, {"type": "error", "text": "An error occurred during live commentary"})

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass

    # Shut down sync worker
    await sync_queue.put(None)
    await sync_task
    await send_msg(ws, {"type": "done"})


@app.websocket("/ws")
async def commentary_ws(websocket: WebSocket):
    await websocket.accept()
    _ws_locks[id(websocket)] = asyncio.Lock()

    try:
        # Wait for start command
        data = await websocket.receive_json()
        if data.get("action") != "start":
            await send_msg(websocket, {"type": "error", "text": "Expected start action"})
            return

        match_url = data.get("url", "")
        mode = data.get("mode", "replay")

        if not match_url:
            await send_msg(websocket, {"type": "error", "text": "No match URL provided"})
            return

        if not _VALID_URL_RE.match(match_url):
            await send_msg(websocket, {"type": "error", "text": "Invalid match URL — must be an ESPNcricinfo match page"})
            return

        await send_msg(websocket, {"type": "status", "text": "Connecting to ESPNcricinfo..."})

        scraper = CricketScraper(match_url)
        enhancer = CommentaryEnhancer()
        tts = CommentaryTTS()

        stop_event = asyncio.Event()

        # Listen for stop command in background
        async def listen_for_stop():
            try:
                while not stop_event.is_set():
                    msg = await websocket.receive_json()
                    if msg.get("action") == "stop":
                        stop_event.set()
                        break
            except (WebSocketDisconnect, Exception):
                stop_event.set()

        # Keepalive ping to prevent Railway proxy from killing idle connections
        async def keepalive():
            try:
                while not stop_event.is_set():
                    await asyncio.sleep(15)
                    await send_msg(websocket, {"type": "ping"})
            except Exception:
                pass

        stop_task = asyncio.create_task(listen_for_stop())
        ping_task = asyncio.create_task(keepalive())

        try:
            await scraper.start()

            if mode == "replay":
                await run_replay(websocket, scraper, enhancer, tts, stop_event)
            else:
                await run_live(websocket, scraper, enhancer, tts, stop_event)
        finally:
            stop_event.set()
            stop_task.cancel()
            ping_task.cancel()
            await scraper.stop()

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception("WebSocket handler error")
        try:
            await send_msg(websocket, {"type": "error", "text": "An unexpected error occurred"})
        except Exception:
            pass
    finally:
        _ws_locks.pop(id(websocket), None)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
