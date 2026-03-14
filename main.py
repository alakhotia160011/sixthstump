import asyncio
import argparse
import signal
import sys

from scraper import CricketScraper
from enhancer import CommentaryEnhancer
from tts import CommentaryTTS
from player import AudioPlayer
from tracker import ReplayStatTracker
from config import POLL_INTERVAL, VOICE_CONFIG


async def run(match_url: str, replay: bool = False):
    scraper = CricketScraper(match_url)
    enhancer = CommentaryEnhancer()
    tts = CommentaryTTS()
    player = AudioPlayer(sample_rate=tts.sample_rate)

    # Graceful shutdown
    stop_event = asyncio.Event()

    def handle_signal():
        print("\n[main] shutting down...")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    await scraper.start()

    if replay:
        # Replay mode: fetch ALL commentary across all innings
        entries = await scraper.get_all_entries()
        match_intro = await scraper.get_match_intro()
        print(f"[main] replay mode - commentating on {len(entries)} entries\n")

        # Ball-by-ball stat tracker - avoids using end-of-match API stats
        tracker = ReplayStatTracker()

        # Generate and play match introduction
        if match_intro:
            print("[main] generating match intro...")
            intro_segments = await enhancer.generate_intro(match_intro)
            for seg in intro_segments:
                if not seg.text:
                    continue
                print(f"[{seg.speaker}] ({seg.emotion}) {seg.text}")
                try:
                    vcfg = VOICE_CONFIG.get(seg.speaker, VOICE_CONFIG["harsha"])
                    pcm_audio = tts.synthesize(seg.text, emotion=seg.emotion,
                                               voice_id=vcfg["voice_id"], language=vcfg["language"], speaker=seg.speaker)
                    player.play_with_pause(pcm_audio, pause_after=1.5)
                except Exception as e:
                    print(f"[tts] intro error: {e}")
            print()

        prev_over = None
        recent_balls: list[str] = []
        summaries_done: set[int] = set()
        current_innings = 1
        tracker.set_innings(current_innings)
        ball_count = 0  # total balls processed, for filler pacing

        for entry in entries:
            if stop_event.is_set():
                break

            cur_over_num = entry.over.split(".")[0] if "." in entry.over else entry.over
            if prev_over is not None:
                prev_over_num = prev_over.split(".")[0] if "." in prev_over else prev_over
                try:
                    prev_int = int(prev_over_num)
                    cur_int = int(cur_over_num)

                    # Innings break: over number drops significantly
                    if cur_int < prev_int - 2:
                        print("[main] === INNINGS BREAK ===")
                        # Use tracker's accumulated context for the break summary
                        tracker_context = tracker.get_match_context()
                        break_segments = await enhancer.generate_innings_break(tracker_context)
                        for seg in break_segments:
                            if not seg.text:
                                continue
                            print(f"[{seg.speaker}] ({seg.emotion}) {seg.text}")
                            try:
                                vcfg = VOICE_CONFIG.get(seg.speaker, VOICE_CONFIG["harsha"])
                                pcm_audio = tts.synthesize(seg.text, emotion=seg.emotion,
                                                           voice_id=vcfg["voice_id"], language=vcfg["language"], speaker=seg.speaker)
                                player.play_with_pause(pcm_audio, pause_after=2.0)
                            except Exception as e:
                                print(f"[tts] break error: {e}")
                        print()
                        recent_balls.clear()
                        summaries_done.clear()
                        current_innings += 1
                        tracker.set_innings(current_innings)
                        ball_count = 0

                    # Over change - always give a score update
                    # Display over = raw + 1 (0.x = over 1, 5.x = over 6)
                    elif prev_int != cur_int:
                        display_over = prev_int + 1
                        # Milestone overs get an extended summary
                        if prev_int in enhancer.MILESTONE_OVERS and prev_int not in summaries_done:
                            summaries_done.add(prev_int)
                            print(f"[main] over {display_over} summary")
                            player_stats = tracker.get_player_stats(current_innings)
                            tracker_context = tracker.get_match_context()
                            summary_segments = await enhancer.generate_over_summary(display_over, tracker_context, recent_balls, player_stats)
                            for seg in summary_segments:
                                if not seg.text:
                                    continue
                                print(f"[{seg.speaker}] ({seg.emotion}) {seg.text}")
                                try:
                                    vcfg = VOICE_CONFIG.get(seg.speaker, VOICE_CONFIG["harsha"])
                                    pcm_audio = tts.synthesize(seg.text, emotion=seg.emotion,
                                                               voice_id=vcfg["voice_id"], language=vcfg["language"], speaker=seg.speaker)
                                    player.play_with_pause(pcm_audio, pause_after=1.2)
                                except Exception as e:
                                    print(f"[tts] summary error: {e}")
                            print()
                        else:
                            # Regular over change - quick score + batsmen update
                            current_stats = tracker.get_current_player_stats(entry.text, current_innings)
                            tracker_context = tracker.get_match_context()
                            score_segments = await enhancer.generate_score_update(display_over, tracker_context, current_stats)
                            for seg in score_segments:
                                if not seg.text:
                                    continue
                                print(f"[{seg.speaker}] ({seg.emotion}) {seg.text}")
                                try:
                                    vcfg = VOICE_CONFIG.get(seg.speaker, VOICE_CONFIG["harsha"])
                                    pcm_audio = tts.synthesize(seg.text, emotion=seg.emotion,
                                                               voice_id=vcfg["voice_id"], language=vcfg["language"], speaker=seg.speaker)
                                    player.play_with_pause(pcm_audio, pause_after=1.0)
                                except Exception as e:
                                    print(f"[tts] score error: {e}")
                            print()
                except ValueError:
                    pass
            prev_over = entry.over
            ball_count += 1

            # Track stats BEFORE generating commentary (so filler has up-to-date stats)
            tracker.process_entry(entry)

            print(f"[ball {entry.over}] {entry.text}")

            # Use tracker context so we don't leak future match info
            live_context = tracker.get_match_context()
            segments = await enhancer.enhance(entry.text, live_context, over=entry.over)
            for seg in segments:
                if not seg.text:
                    continue
                print(f"[{seg.speaker}] ({seg.emotion}) {seg.text}")
                try:
                    vcfg = VOICE_CONFIG.get(seg.speaker, VOICE_CONFIG["harsha"])
                    pcm_audio = tts.synthesize(seg.text, emotion=seg.emotion,
                                               voice_id=vcfg["voice_id"], language=vcfg["language"], speaker=seg.speaker)
                    player.play_with_pause(pcm_audio, pause_after=0.8)
                except Exception as e:
                    print(f"[tts] error: {e}")

            recent_balls.append(f"[{entry.over}] {entry.text[:80]}")
            if len(recent_balls) > 12:
                recent_balls.pop(0)

            # Filler every 3 balls - stats, insight, or tactical observation
            if ball_count % 3 == 0:
                current_stats = tracker.get_current_player_stats(entry.text, current_innings)
                tracker_context = tracker.get_match_context()
                filler_segments = await enhancer.generate_filler(tracker_context, recent_balls, current_stats)
                for seg in filler_segments:
                    if not seg.text:
                        continue
                    print(f"[{seg.speaker}] ({seg.emotion}) {seg.text}")
                    try:
                        vcfg = VOICE_CONFIG.get(seg.speaker, VOICE_CONFIG["harsha"])
                        pcm_audio = tts.synthesize(seg.text, emotion=seg.emotion,
                                                   voice_id=vcfg["voice_id"], language=vcfg["language"], speaker=seg.speaker)
                        player.play_with_pause(pcm_audio, pause_after=0.6)
                    except Exception as e:
                        print(f"[tts] filler error: {e}")
                print()

            print()

        print("[main] replay complete")
    else:
        # Live mode: skip existing, poll for new
        entries = await scraper.get_new_entries()
        match_context = await scraper.get_match_context()

        # Generate match intro for live mode too
        match_intro = await scraper.get_match_intro()
        if match_intro:
            print("[main] generating match intro...")
            intro_segments = await enhancer.generate_intro(match_intro, match_context)
            for seg in intro_segments:
                if not seg.text:
                    continue
                print(f"[{seg.speaker}] ({seg.emotion}) {seg.text}")
                try:
                    vcfg = VOICE_CONFIG.get(seg.speaker, VOICE_CONFIG["harsha"])
                    pcm_audio = tts.synthesize(seg.text, emotion=seg.emotion,
                                               voice_id=vcfg["voice_id"], language=vcfg["language"], speaker=seg.speaker)
                    player.play_with_pause(pcm_audio, pause_after=1.5)
                except Exception as e:
                    print(f"[tts] intro error: {e}")
            print()

        if entries:
            print(f"[main] skipped {len(entries)} existing entries - waiting for live balls")
        print(f"[main] polling every {POLL_INTERVAL}s - press Ctrl+C to stop\n")

        empty_polls = 0  # consecutive polls with no new balls
        prev_over = None
        last_ball_text = ""
        recent_balls: list[str] = []
        summaries_done: set[int] = set()

        while not stop_event.is_set():
            try:
                entries = await scraper.get_new_entries()
                match_context = await scraper.get_match_context()

                if entries:
                    empty_polls = 0  # reset - we got new data

                    for entry in entries:
                        if stop_event.is_set():
                            break

                        # Over summary at milestones
                        cur_over_num = entry.over.split(".")[0] if "." in entry.over else entry.over
                        if prev_over is not None:
                            prev_over_num = prev_over.split(".")[0] if "." in prev_over else prev_over
                            try:
                                prev_int = int(prev_over_num)
                                cur_int = int(cur_over_num)
                                if prev_int != cur_int and prev_int in enhancer.MILESTONE_OVERS and prev_int not in summaries_done:
                                    summaries_done.add(prev_int)
                                    print(f"[main] over {prev_int} summary")
                                    player_stats = scraper.get_player_stats()
                                    summary_segments = await enhancer.generate_over_summary(prev_int, match_context, recent_balls, player_stats)
                                    for seg in summary_segments:
                                        if not seg.text:
                                            continue
                                        print(f"[{seg.speaker}] ({seg.emotion}) {seg.text}")
                                        try:
                                            vcfg = VOICE_CONFIG.get(seg.speaker, VOICE_CONFIG["harsha"])
                                            pcm_audio = tts.synthesize(seg.text, emotion=seg.emotion,
                                                                       voice_id=vcfg["voice_id"], language=vcfg["language"], speaker=seg.speaker)
                                            player.play_with_pause(pcm_audio, pause_after=1.2)
                                        except Exception as e:
                                            print(f"[tts] summary error: {e}")
                                    print()
                            except ValueError:
                                pass
                        prev_over = entry.over
                        last_ball_text = entry.text

                        print(f"[ball {entry.over}] {entry.text}")

                        segments = await enhancer.enhance(entry.text, match_context, over=entry.over)
                        for seg in segments:
                            if not seg.text:
                                continue
                            print(f"[{seg.speaker}] ({seg.emotion}) {seg.text}")
                            try:
                                vcfg = VOICE_CONFIG.get(seg.speaker, VOICE_CONFIG["harsha"])
                                pcm_audio = tts.synthesize(seg.text, emotion=seg.emotion,
                                                           voice_id=vcfg["voice_id"], language=vcfg["language"], speaker=seg.speaker)
                                player.play_with_pause(pcm_audio, pause_after=0.8)
                            except Exception as e:
                                print(f"[tts] error: {e}")

                        recent_balls.append(f"[{entry.over}] {entry.text[:80]}")
                        if len(recent_balls) > 12:
                            recent_balls.pop(0)

                        print()
                else:
                    # No new ball - dead air between deliveries
                    empty_polls += 1

                    # After 3+ empty polls (~24s), fill the gap with insight
                    if empty_polls == 3 and last_ball_text:
                        current_stats = scraper.get_current_player_stats(last_ball_text)
                        filler_segments = await enhancer.generate_filler(match_context, recent_balls, current_stats)
                        for seg in filler_segments:
                            if not seg.text:
                                continue
                            print(f"[{seg.speaker}] ({seg.emotion}) {seg.text}")
                            try:
                                vcfg = VOICE_CONFIG.get(seg.speaker, VOICE_CONFIG["harsha"])
                                pcm_audio = tts.synthesize(seg.text, emotion=seg.emotion,
                                                           voice_id=vcfg["voice_id"], language=vcfg["language"], speaker=seg.speaker)
                                player.play_with_pause(pcm_audio, pause_after=0.6)
                            except Exception as e:
                                print(f"[tts] filler error: {e}")
                        print()

            except Exception as e:
                print(f"[main] error in loop: {e}")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass

    await scraper.stop()
    print("[main] done")


def main():
    parser = argparse.ArgumentParser(description="Live AI Cricket Commentary")
    parser.add_argument(
        "match_url",
        help="ESPNcricinfo match URL, e.g. https://www.espncricinfo.com/series/.../ball-by-ball-commentary",
    )
    parser.add_argument(
        "--replay",
        action="store_true",
        help="Replay existing commentary instead of waiting for live updates",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=None,
        help=f"Seconds between polls (default: {POLL_INTERVAL})",
    )
    args = parser.parse_args()

    if args.poll_interval:
        import config
        config.POLL_INTERVAL = args.poll_interval

    asyncio.run(run(args.match_url, replay=args.replay))


if __name__ == "__main__":
    main()
