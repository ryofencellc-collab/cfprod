"""
Free narration with Piper TTS.

Default voice: en_US-libritts-high — trained from scratch on LibriTTS
(CC BY 4.0), so commercial use is allowed with attribution. Voices derived
from the "lessac" dataset (lessac, amy, ryan, hfc_* and most fine-tunes) are
non-commercial and must NOT be used — see docs/COMPLIANCE.md section 4.

Each sentence is synthesized separately so we know exactly when it starts
and ends; word timings for captions are spread across each sentence by
word length.
"""
import os
import re
import threading
import wave

import numpy as np

from config import VOICES_DIR

DEFAULT_VOICE = "en_US-libritts-high"
VOICE_CREDIT = "Narration voice: Piper TTS, LibriTTS voice (CC BY 4.0, Zen et al. 2019)."
SENTENCE_GAP = 0.32  # seconds of silence between sentences

# Voices whose training data forbids commercial use.
NON_COMMERCIAL = re.compile(r"lessac|amy|ryan|hfc_|kristin|kusal|ljspeech|arctic", re.I)

_voices = {}
_lock = threading.Lock()


def voice_path(name: str = DEFAULT_VOICE) -> str:
    return str(VOICES_DIR / f"{name}.onnx")


def _load(name: str):
    if NON_COMMERCIAL.search(name):
        raise RuntimeError(f"Voice '{name}' is licensed for non-commercial use only.")
    with _lock:
        if name not in _voices:
            from piper import PiperVoice
            path = voice_path(name)
            if not os.path.exists(path):
                raise RuntimeError(f"Voice model missing: {path}")
            _voices[name] = PiperVoice.load(path)
        return _voices[name]


def split_sentences(text: str) -> list:
    text = re.sub(r"\s+", " ", text or "").strip()
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text)
    return [p.strip() for p in parts if p.strip()]


def synthesize(text: str, out_wav: str, speaker: int = 0, length_scale: float = 1.15,
               voice: str = DEFAULT_VOICE) -> dict:
    """Write narration to out_wav. Returns {"duration", "words": [{word,start,end}]}."""
    from piper.config import SynthesisConfig
    v = _load(voice)
    cfg = SynthesisConfig(speaker_id=speaker, length_scale=length_scale)
    rate = v.config.sample_rate
    gap = np.zeros(int(SENTENCE_GAP * rate), dtype=np.float32)

    pieces, timed_words, t = [], [], 0.0
    for sentence in split_sentences(text):
        audio = [c.audio_float_array for c in v.synthesize(sentence, syn_config=cfg)]
        if not audio:
            continue
        samples = np.concatenate(audio).astype(np.float32)
        dur = len(samples) / rate
        timed_words += _spread_words(sentence, t, t + dur)
        pieces += [samples, gap]
        t += dur + SENTENCE_GAP
    if not pieces:
        raise RuntimeError("Nothing to synthesize.")

    audio = np.concatenate(pieces[:-1])
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(out_wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return {"duration": len(audio) / rate, "words": timed_words}


def _spread_words(sentence: str, start: float, end: float) -> list:
    """Distribute a sentence's duration over its words, weighted by length."""
    toks = sentence.split()
    if not toks:
        return []
    # Trim a little lead-in/out silence the model adds around each sentence.
    lead = min(0.08, (end - start) * 0.05)
    start, end = start + lead, end - lead
    weights = [max(2, len(re.sub(r"[^A-Za-z0-9]", "", w))) + 1.5 for w in toks]
    total = sum(weights)
    out, t = [], start
    for tok, wgt in zip(toks, weights):
        d = (end - start) * wgt / total
        out.append({"word": tok, "start": round(t, 3), "end": round(t + d, 3)})
        t += d
    return out


def words_to_segments(words: list, offset: float = 0.0) -> list:
    """Wrap timed words into the segment format the caption builder expects."""
    if not words:
        return []
    shifted = [{"word": w["word"], "start": w["start"] + offset, "end": w["end"] + offset} for w in words]
    return [{
        "start": shifted[0]["start"],
        "end": shifted[-1]["end"],
        "text": " ".join(w["word"] for w in shifted),
        "words": shifted,
    }]
