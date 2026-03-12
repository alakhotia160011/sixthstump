import asyncio
import argparse
import signal
import sys

from scraper import CricketScraper
from enhancer import CommentaryEnhancer
from tts import CommentaryTTS
from player import AudioPlayer
from config import POLL_INTERVAL


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
        match_context = await scraper.get_match_context()
        match_intro = await scraper.get_match_intro()
        print(f"[main] replay mode — commentating on {len(entries)} entries")
        print(f"[main] match: {match_context}\n")

        # Generate and play match introduction
        if match_intro:
            print("[main] generating match intro...")
            intro = await enhancer.generate_intro(match_intro, match_context)
            if intro.text:
                print(f"[intro] ({intro.emotion}) {intro.text}")
                try:
                    pcm_audio = tts.synthesize(intro.text, emotion=intro.emotion)
                    player.play_with_pause(pcm_audio, pause_after=1.5)
                except Exception as e:
                    print(f"[tts] intro error: {e}")
                print()

        prev_over = None
        recent_balls: list[str] = []
        summaries_done: set[int] = set()
        current_innings = 1
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
                        break_result = await enhancer.generate_innings_break(match_context)
                        if break_result.text:
                            print(f"[break] ({break_result.emotion}) {break_result.text}")
                            try:
                                pcm_audio = tts.synthesize(break_result.text, emotion=break_result.emotion)
                                player.play_with_pause(pcm_audio, pause_after=2.0)
                            except Exception as e:
                                print(f"[tts] break error: {e}")
                            print()
                        recent_balls.clear()
                        summaries_done.clear()
                        current_innings += 1
                        ball_count = 0

                    # Over change — always give a score update
                    elif prev_int != cur_int:
                        # Milestone overs get an extended summary
                        if prev_int in enhancer.MILESTONE_OVERS and prev_int not in summaries_done:
                            summaries_done.add(prev_int)
                            print(f"[main] --- over {prev_int} summary ---")
                            player_stats = scraper.get_player_stats(current_innings)
                            summary = await enhancer.generate_over_summary(prev_int, match_context, recent_balls, player_stats)
                            if summary.text:
                                print(f"[summary] ({summary.emotion}) {summary.text}")
                                try:
                                    pcm_audio = tts.synthesize(summary.text, emotion=summary.emotion)
                                    player.play_with_pause(pcm_audio, pause_after=1.2)
                                except Exception as e:
                                    print(f"[tts] summary error: {e}")
                                print()
                        else:
                            # Regular over change — quick score + batsmen update
                            current_stats = scraper.get_current_player_stats(entry.text, current_innings)
                            score_update = await enhancer.generate_score_update(prev_int, match_context, current_stats)
                            if score_update.text:
                                print(f"[score] ({score_update.emotion}) {score_update.text}")
                                try:
                                    pcm_audio = tts.synthesize(score_update.text, emotion=score_update.emotion)
                                    player.play_with_pause(pcm_audio, pause_after=1.0)
                                except Exception as e:
                                    print(f"[tts] score error: {e}")
                                print()
                except ValueError:
                    pass
            prev_over = entry.over
            ball_count += 1

            print(f"[ball {entry.over}] {entry.text}")

            result = await enhancer.enhance(entry.text, match_context, over=entry.over)
            print(f"[commentary] ({result.emotion}) {result.text}")

            try:
                pcm_audio = tts.synthesize(result.text, emotion=result.emotion)
                player.play_with_pause(pcm_audio, pause_after=0.8)
            except Exception as e:
                print(f"[tts] error: {e}")

            recent_balls.append(f"[{entry.over}] {entry.text[:80]}")
            if len(recent_balls) > 12:
                recent_balls.pop(0)

            # Filler every 3 balls — stats, insight, or tactical observation
            if ball_count % 3 == 0:
                current_stats = scraper.get_current_player_stats(entry.text, current_innings)
                filler = await enhancer.generate_filler(match_context, recent_balls, current_stats)
                if filler.text:
                    print(f"[filler] ({filler.emotion}) {filler.text}")
                    try:
                        pcm_audio = tts.synthesize(filler.text, emotion=filler.emotion)
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
            intro = await enhancer.generate_intro(match_intro, match_context)
            if intro.text:
                print(f"[intro] ({intro.emotion}) {intro.text}")
                try:
                    pcm_audio = tts.synthesize(intro.text, emotion=intro.emotion)
                    player.play_with_pause(pcm_audio, pause_after=1.5)
                except Exception as e:
                    print(f"[tts] intro error: {e}")
                print()

        if entries:
            print(f"[main] skipped {len(entries)} existing entries — waiting for live balls")
        print(f"[main] polling every {POLL_INTERVAL}s — press Ctrl+C to stop\n")

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
                    empty_polls = 0  # reset — we got new data

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
                                    print(f"[main] --- over {prev_int} summary ---")
                                    player_stats = scraper.get_player_stats()
                                    summary = await enhancer.generate_over_summary(prev_int, match_context, recent_balls, player_stats)
                                    if summary.text:
                                        print(f"[summary] ({summary.emotion}) {summary.text}")
                                        try:
                                            pcm_audio = tts.synthesize(summary.text, emotion=summary.emotion)
                                            player.play_with_pause(pcm_audio, pause_after=1.2)
                                        except Exception as e:
                                            print(f"[tts] summary error: {e}")
                                        print()
                            except ValueError:
                                pass
                        prev_over = entry.over
                        last_ball_text = entry.text

                        print(f"[ball {entry.over}] {entry.text}")

                        result = await enhancer.enhance(entry.text, match_context, over=entry.over)
                        print(f"[commentary] ({result.emotion}) {result.text}")

                        try:
                            pcm_audio = tts.synthesize(result.text, emotion=result.emotion)
                            player.play_with_pause(pcm_audio, pause_after=0.8)
                        except Exception as e:
                            print(f"[tts] error: {e}")

                        recent_balls.append(f"[{entry.over}] {entry.text[:80]}")
                        if len(recent_balls) > 12:
                            recent_balls.pop(0)

                        print()
                else:
                    # No new ball — dead air between deliveries
                    empty_polls += 1

                    # After 3+ empty polls (~24s), fill the gap with insight
                    if empty_polls == 3 and last_ball_text:
                        current_stats = scraper.get_current_player_stats(last_ball_text)
                        filler = await enhancer.generate_filler(match_context, recent_balls, current_stats)
                        if filler.text:
                            print(f"[filler] ({filler.emotion}) {filler.text}")
                            try:
                                pcm_audio = tts.synthesize(filler.text, emotion=filler.emotion)
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
