"""FastAPI server for sixthstump — streams commentary + audio over WebSocket."""

import asyncio
import base64
import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

from scraper import CricketScraper, fetch_matches
from enhancer import CommentaryEnhancer
from tts import CommentaryTTS
from tracker import ReplayStatTracker
from config import POLL_INTERVAL

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


async def send_msg(ws: WebSocket, msg: dict):
    """Send JSON, silently ignore if connection closed."""
    try:
        await ws.send_json(msg)
    except Exception:
        pass


def create_sync_worker(ws: WebSocket, tts: CommentaryTTS):
    """Background worker that sends text + audio together in order.

    Each queue item is (commentary_msg, tts_text, tts_emotion).
    The worker synthesizes audio, then sends the commentary message
    and audio back-to-back so they arrive in sync on the client.
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def worker():
        while True:
            item = await queue.get()
            if item is None:
                break
            commentary_msg, tts_text, tts_emotion = item
            # Synthesize audio first
            audio_b64 = None
            try:
                pcm = await asyncio.to_thread(tts.synthesize, tts_text, tts_emotion)
                audio_b64 = base64.b64encode(pcm).decode("ascii")
            except Exception as e:
                await send_msg(ws, {"type": "error", "text": f"TTS error: {e}"})
            # Send text + audio together
            await send_msg(ws, commentary_msg)
            if audio_b64:
                await send_msg(ws, {"type": "audio", "data": audio_b64})

    task = asyncio.create_task(worker())
    return queue, task


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
    entries = await scraper.get_all_entries()
    match_intro = await scraper.get_match_intro()
    tracker = ReplayStatTracker()

    await send_msg(ws, {"type": "status", "text": f"Replay mode — {len(entries)} balls"})

    # Send team info for the scoreboard
    teams_data = scraper._match.get("teams", [])
    team_names = {}
    team_ids = {}
    for i, t in enumerate(teams_data):
        team = t.get("team", {})
        team_names[i + 1] = team.get("abbreviation") or team.get("name") or f"Team {i+1}"
        team_ids[i + 1] = team.get("objectId", "")
    await send_msg(ws, {"type": "teams", "teams": team_names, "teamIds": team_ids})

    # Sync worker — sends text + audio together in order
    sync_queue, sync_task = create_sync_worker(ws, tts)

    # Match intro
    if match_intro:
        await send_msg(ws, {"type": "status", "text": "Generating match intro..."})
        intro = await asyncio.to_thread(
            lambda: enhancer.generate_intro.__wrapped__(enhancer, match_intro)
            if hasattr(enhancer.generate_intro, '__wrapped__')
            else None
        )
        # generate_intro is async, call it directly
        intro = await enhancer.generate_intro(match_intro)
        if intro.text:
            msg = {"type": "commentary", "tag": "intro", "text": intro.text, "emotion": intro.emotion}
            await sync_queue.put((msg, intro.text, intro.emotion))

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
                    break_result = await enhancer.generate_innings_break(tracker_context)
                    if break_result.text:
                        msg = {"type": "commentary", "tag": "break",
                               "text": break_result.text, "emotion": break_result.emotion}
                        await sync_queue.put((msg, break_result.text, break_result.emotion))
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
                        summary = await enhancer.generate_over_summary(display_over, tracker_context, recent_balls, player_stats)
                        if summary.text:
                            msg = {"type": "commentary", "tag": "summary",
                                   "text": summary.text, "emotion": summary.emotion,
                                   "over": f"Over {display_over}"}
                            await sync_queue.put((msg, summary.text, summary.emotion))
                    else:
                        current_stats = tracker.get_current_player_stats(entry.text, current_innings)
                        tracker_context = tracker.get_match_context()
                        score_update = await enhancer.generate_score_update(display_over, tracker_context, current_stats)
                        if score_update.text:
                            msg = {"type": "commentary", "tag": "score",
                                   "text": score_update.text, "emotion": score_update.emotion,
                                   "over": f"Over {display_over}"}
                            await sync_queue.put((msg, score_update.text, score_update.emotion))
            except ValueError:
                pass

        prev_over = entry.over
        ball_count += 1
        tracker.process_entry(entry)

        # Send live scoreboard update immediately (no TTS needed)
        await send_msg(ws, {"type": "scorecard", "innings": _build_scorecard(tracker)})

        # Ball commentary — text + audio sent together by worker
        live_context = tracker.get_match_context()
        result = await enhancer.enhance(entry.text, live_context, over=entry.over)
        ball_data = {
            "runs": entry.total_runs,
            "batsmanRuns": entry.batsman_runs,
            "isFour": entry.is_four,
            "isSix": entry.is_six,
            "isWicket": entry.is_wicket,
            "wides": entry.wides,
            "noballs": entry.noballs,
            "legbyes": entry.legbyes,
            "byes": entry.byes,
        }
        msg = {"type": "commentary", "tag": "ball",
               "text": result.text, "emotion": result.emotion,
               "over": entry.over, "ballData": ball_data}
        await sync_queue.put((msg, result.text, result.emotion))

        recent_balls.append(f"[{entry.over}] {entry.text[:80]}")
        if len(recent_balls) > 12:
            recent_balls.pop(0)

        # Filler every 3 balls
        if ball_count % 3 == 0:
            current_stats = tracker.get_current_player_stats(entry.text, current_innings)
            tracker_context = tracker.get_match_context()
            filler = await enhancer.generate_filler(tracker_context, recent_balls, current_stats)
            if filler.text:
                msg = {"type": "commentary", "tag": "filler",
                       "text": filler.text, "emotion": filler.emotion}
                await sync_queue.put((msg, filler.text, filler.emotion))

    # Wait for remaining items to finish, then shut down worker
    await sync_queue.put(None)
    await sync_task
    await send_msg(ws, {"type": "done"})


async def run_live(ws: WebSocket, scraper: CricketScraper, enhancer: CommentaryEnhancer,
                   tts: CommentaryTTS, stop_event: asyncio.Event):
    """Live mode: poll for new balls and commentate."""
    entries = await scraper.get_new_entries()
    match_context = await scraper.get_match_context()

    # Sync worker — sends text + audio together in order
    sync_queue, sync_task = create_sync_worker(ws, tts)

    match_intro = await scraper.get_match_intro()
    if match_intro:
        intro = await enhancer.generate_intro(match_intro, match_context)
        if intro.text:
            msg = {"type": "commentary", "tag": "intro", "text": intro.text, "emotion": intro.emotion}
            await sync_queue.put((msg, intro.text, intro.emotion))

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
                                summary = await enhancer.generate_over_summary(prev_int, match_context, recent_balls, player_stats)
                                if summary.text:
                                    msg = {"type": "commentary", "tag": "summary",
                                           "text": summary.text, "emotion": summary.emotion}
                                    await sync_queue.put((msg, summary.text, summary.emotion))
                        except ValueError:
                            pass
                    prev_over = entry.over
                    last_ball_text = entry.text

                    result = await enhancer.enhance(entry.text, match_context, over=entry.over)
                    ball_data = {
                        "runs": entry.total_runs,
                        "batsmanRuns": entry.batsman_runs,
                        "isFour": entry.is_four,
                        "isSix": entry.is_six,
                        "isWicket": entry.is_wicket,
                        "wides": entry.wides,
                        "noballs": entry.noballs,
                        "legbyes": entry.legbyes,
                        "byes": entry.byes,
                    }
                    msg = {"type": "commentary", "tag": "ball",
                           "text": result.text, "emotion": result.emotion,
                           "over": entry.over, "ballData": ball_data}
                    await sync_queue.put((msg, result.text, result.emotion))

                    recent_balls.append(f"[{entry.over}] {entry.text[:80]}")
                    if len(recent_balls) > 12:
                        recent_balls.pop(0)
            else:
                empty_polls += 1
                if empty_polls == 3 and last_ball_text:
                    current_stats = scraper.get_current_player_stats(last_ball_text)
                    filler = await enhancer.generate_filler(match_context, recent_balls, current_stats)
                    if filler.text:
                        msg = {"type": "commentary", "tag": "filler",
                               "text": filler.text, "emotion": filler.emotion}
                        await sync_queue.put((msg, filler.text, filler.emotion))

        except Exception as e:
            await send_msg(ws, {"type": "error", "text": f"Error: {e}"})

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

        stop_task = asyncio.create_task(listen_for_stop())

        try:
            await scraper.start()

            if mode == "replay":
                await run_replay(websocket, scraper, enhancer, tts, stop_event)
            else:
                await run_live(websocket, scraper, enhancer, tts, stop_event)
        finally:
            stop_event.set()
            stop_task.cancel()
            await scraper.stop()

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await send_msg(websocket, {"type": "error", "text": str(e)})
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
