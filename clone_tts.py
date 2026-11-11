"""Speak text in a cloned voice with Chatterbox (MIT). Runs in .venv-clone, which has its own torch.

  .venv-clone/bin/python clone_tts.py --text "..." --sample brand/voice_sample.wav --out voice.wav
"""
import argparse, os, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("HF_HOME", str(ROOT / ".cache" / "hf"))  # model downloads stay inside the project

import torch
import torchaudio

ap = argparse.ArgumentParser()
ap.add_argument("--text", required=True)
ap.add_argument("--sample", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--exaggeration", type=float, default=0.5)  # 0.25 flat ... 1.0 very expressive
args = ap.parse_args()

from chatterbox.tts import ChatterboxTTS


def load(device):
    real_load = torch.load  # checkpoints were saved on CUDA; map them to this machine
    torch.load = lambda *a, **k: real_load(*a, **{"map_location": torch.device(device), **k})
    try:
        return ChatterboxTTS.from_pretrained(device=device)
    finally:
        torch.load = real_load


try:
    model = load("mps") if torch.backends.mps.is_available() else load("cpu")
except Exception:
    model = load("cpu")

sentences = [s for s in re.split(r"(?<=[.!?])\s+", args.text.strip()) if s]  # short inputs keep the voice stable
gap = torch.zeros(1, int(model.sr * 0.12))
parts = []
for s in sentences:
    wav = model.generate(s, audio_prompt_path=args.sample, exaggeration=args.exaggeration)
    parts += [wav.cpu(), gap]
torchaudio.save(args.out, torch.cat(parts, dim=1), model.sr)
print(f"cloned voice: {len(sentences)} sentences -> {args.out}")
