import io
import struct
from typing import Optional
from cartesia import Cartesia
from config import CARTESIA_API_KEY, CARTESIA_VOICE_ID


class CommentaryTTS:
    """Converts enhanced commentary text to speech using Cartesia."""

    def __init__(self, voice_id: Optional[str] = None):
        self.client = Cartesia(api_key=CARTESIA_API_KEY)
        self.voice_id = voice_id or CARTESIA_VOICE_ID
        self.sample_rate = 44100
        self.output_format = {
            "container": "raw",
            "encoding": "pcm_f32le",
            "sample_rate": self.sample_rate,
        }

    def synthesize(self, text: str, emotion: str = "neutral") -> bytes:
        """Convert text to raw PCM audio bytes (float32, 44100 Hz, mono)."""
        # Ensure text ends with punctuation to prevent TTS repeating the last word
        clean = text.strip()
        if clean and clean[-1] not in ".!?":
            clean += "."

        kwargs = dict(
            model_id="sonic-3",
            transcript=clean,
            voice={"mode": "id", "id": self.voice_id},
            output_format=self.output_format,
            language="hi",
        )

        # Add emotion, speed, and volume controls for sonic-3
        gen_config = self.EMOTION_PROFILES.get(emotion, {})
        if gen_config:
            kwargs["generation_config"] = gen_config

        output = self.client.tts.bytes(**kwargs)
        # Cartesia SDK v3 returns a generator of chunks
        if isinstance(output, bytes):
            return output
        return b"".join(output)

    # Emotion profiles: speed + volume tuned per emotion for dramatic commentary
    # Using "highest" intensity for high-energy moments, "high" for moderate
    EMOTION_PROFILES = {
        "excited":       {"emotion": "excited:highest",       "speed": 1.0,  "volume": 1.8},
        "enthusiastic":  {"emotion": "enthusiastic:highest",  "speed": 0.95, "volume": 1.6},
        "triumphant":    {"emotion": "triumphant:highest",    "speed": 0.9,  "volume": 1.9},
        "amazed":        {"emotion": "amazed:highest",        "speed": 0.95, "volume": 1.7},
        "surprised":     {"emotion": "surprised:highest",     "speed": 0.95, "volume": 1.7},
        "calm":          {"emotion": "calm:high",             "speed": 0.75, "volume": 0.9},
        "content":       {"emotion": "content:high",          "speed": 0.8,  "volume": 1.0},
        "anticipation":  {"emotion": "anticipation:highest",  "speed": 0.9,  "volume": 1.4},
        "disappointed":  {"emotion": "disappointed:highest",  "speed": 0.7,  "volume": 0.8},
        "proud":         {"emotion": "proud:highest",         "speed": 0.85, "volume": 1.5},
        "confident":     {"emotion": "confident:high",        "speed": 0.85, "volume": 1.3},
        "contemplative": {"emotion": "contemplative:high",    "speed": 0.7,  "volume": 0.85},
        "determined":    {"emotion": "determined:highest",    "speed": 0.9,  "volume": 1.5},
    }

    def synthesize_to_wav(self, text: str) -> bytes:
        """Convert text to a complete WAV file in memory."""
        pcm_data = self.synthesize(text)
        return self._pcm_to_wav(pcm_data)

    def _pcm_to_wav(self, pcm_data: bytes) -> bytes:
        """Wrap raw PCM float32 data in a WAV header."""
        num_channels = 1
        sample_width = 4  # float32 = 4 bytes
        byte_rate = self.sample_rate * num_channels * sample_width
        block_align = num_channels * sample_width
        data_size = len(pcm_data)

        buf = io.BytesIO()
        # RIFF header
        buf.write(b"RIFF")
        buf.write(struct.pack("<I", 36 + data_size))
        buf.write(b"WAVE")
        # fmt chunk — format 3 = IEEE float
        buf.write(b"fmt ")
        buf.write(struct.pack("<I", 16))
        buf.write(struct.pack("<H", 3))  # IEEE float
        buf.write(struct.pack("<H", num_channels))
        buf.write(struct.pack("<I", self.sample_rate))
        buf.write(struct.pack("<I", byte_rate))
        buf.write(struct.pack("<H", block_align))
        buf.write(struct.pack("<H", sample_width * 8))
        # data chunk
        buf.write(b"data")
        buf.write(struct.pack("<I", data_size))
        buf.write(pcm_data)
        return buf.getvalue()
