"""Transcribe a video with Deepgram Nova-3.

Extracts mono 16kHz audio via ffmpeg, uploads to Deepgram with diarize +
word-level timestamps, converts output to ElevenLabs Scribe-compatible
JSON so pack_transcripts.py and the editor sub-agent work unchanged.

Cached: if the output file already exists, the upload is skipped.

Usage:
    python helpers/transcribe.py <video_path>
    python helpers/transcribe.py <video_path> --edit-dir /custom/edit
    python helpers/transcribe.py <video_path> --language ru
    python helpers/transcribe.py <video_path> --num-speakers 2
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests


DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


def load_api_key() -> str:
    for candidate in [Path(__file__).resolve().parent.parent / ".env", Path(".env")]:
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "DEEPGRAM_API_KEY":
                    return v.strip().strip('"').strip("'")
    v = os.environ.get("DEEPGRAM_API_KEY", "")
    if not v:
        sys.exit("DEEPGRAM_API_KEY not found in .env or environment")
    return v


def extract_audio(video_path: Path, dest: Path) -> None:
    # MP3 32kbps mono is ~7x smaller than WAV PCM — avoids Deepgram 504 on large files
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libmp3lame", "-b:a", "32k",
        str(dest),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def deepgram_to_scribe_words(dg_words: list[dict]) -> list[dict]:
    """Convert Deepgram word list to ElevenLabs Scribe-compatible format.

    Synthesizes 'spacing' entries from inter-word gaps so pack_transcripts.py
    can detect silences without any changes.
    """
    result: list[dict] = []
    for i, w in enumerate(dg_words):
        if i > 0:
            prev_end = dg_words[i - 1].get("end", 0.0)
            curr_start = w.get("start", prev_end)
            if curr_start > prev_end:
                result.append({
                    "type": "spacing",
                    "start": prev_end,
                    "end": curr_start,
                    "text": "",
                })

        speaker = w.get("speaker")
        speaker_id = f"speaker_{speaker}" if speaker is not None else None

        result.append({
            "type": "word",
            "text": w.get("punctuated_word") or w.get("word", ""),
            "start": w.get("start", 0.0),
            "end": w.get("end", 0.0),
            "speaker_id": speaker_id,
        })

    return result


def call_deepgram(
    audio_path: Path,
    api_key: str,
    language: str | None = None,
    num_speakers: int | None = None,  # kept for interface compat; Deepgram auto-detects
) -> dict:
    params: dict[str, str] = {
        "model": "nova-3",
        "diarize": "true",
        "punctuate": "true",
        "smart_format": "false",
        "utterances": "false",
    }
    if language:
        params["language"] = language

    with open(audio_path, "rb") as f:
        resp = requests.post(
            DEEPGRAM_URL,
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": "audio/mpeg",
            },
            params=params,
            data=f,
            timeout=1800,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Deepgram returned {resp.status_code}: {resp.text[:500]}")

    return resp.json()


def transcribe_one(
    video: Path,
    edit_dir: Path,
    api_key: str,
    language: str | None = None,
    num_speakers: int | None = None,
    verbose: bool = True,
) -> Path:
    """Transcribe a single video. Returns path to transcript JSON.

    Cached: returns existing path immediately if the transcript already exists.
    """
    transcripts_dir = edit_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    out_path = transcripts_dir / f"{video.stem}.json"

    if out_path.exists():
        if verbose:
            print(f"cached: {out_path.name}")
        return out_path

    if verbose:
        print(f"  extracting audio from {video.name}", flush=True)

    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / f"{video.stem}.mp3"
        extract_audio(video, audio)
        size_mb = audio.stat().st_size / (1024 * 1024)
        if verbose:
            print(f"  uploading {video.stem}.mp3 ({size_mb:.1f} MB)", flush=True)
        raw = call_deepgram(audio, api_key, language, num_speakers)

    try:
        dg_words = raw["results"]["channels"][0]["alternatives"][0]["words"]
    except (KeyError, IndexError):
        dg_words = []

    payload = {"words": deepgram_to_scribe_words(dg_words)}
    out_path.write_text(json.dumps(payload, indent=2))
    dt = time.time() - t0

    if verbose:
        kb = out_path.stat().st_size / 1024
        word_count = sum(1 for w in payload["words"] if w.get("type") == "word")
        print(f"  saved: {out_path.name} ({kb:.1f} KB) in {dt:.1f}s")
        print(f"    words: {word_count}")

    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Transcribe a video with Deepgram Nova-3")
    ap.add_argument("video", type=Path, help="Path to video file")
    ap.add_argument(
        "--edit-dir",
        type=Path,
        default=None,
        help="Edit output directory (default: <video_parent>/edit)",
    )
    ap.add_argument(
        "--language",
        type=str,
        default=None,
        help="Optional BCP-47 language code (e.g. 'ru', 'en'). Omit to auto-detect.",
    )
    ap.add_argument(
        "--num-speakers",
        type=int,
        default=None,
        help="Hint for number of speakers (optional; Deepgram auto-detects).",
    )
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")

    edit_dir = (args.edit_dir or (video.parent / "edit")).resolve()
    api_key = load_api_key()

    transcribe_one(
        video=video,
        edit_dir=edit_dir,
        api_key=api_key,
        language=args.language,
        num_speakers=args.num_speakers,
    )


if __name__ == "__main__":
    main()
