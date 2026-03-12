import numpy as np
import sounddevice as sd


class AudioPlayer:
    """Plays PCM audio through system speakers."""

    def __init__(self, sample_rate: int = 44100):
        self.sample_rate = sample_rate

    def play(self, pcm_bytes: bytes):
        """Play raw PCM float32 audio bytes synchronously (blocks until done)."""
        audio = np.frombuffer(pcm_bytes, dtype=np.float32)
        sd.play(audio, samplerate=self.sample_rate)
        sd.wait()

    def play_with_pause(self, pcm_bytes: bytes, pause_after: float = 0.5):
        """Play audio then wait a beat before returning (natural pacing)."""
        self.play(pcm_bytes)
        if pause_after > 0:
            sd.sleep(int(pause_after * 1000))
