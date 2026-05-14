"""Build per-question EDL files from a Deepgram transcript.

For each question boundary in questions.json:
  1. Extract words in [start_sec, end_sec]
  2. Remove filler words (мм, эм, ну, э, ах, гм, ...)
  3. Remove verbal repeats (same word twice in a row, >= 3 chars)
  4. Split into segments at pauses >= silence_threshold
  5. Write edl_q01.json ... edl_qNN.json

EDL format is identical to existing edl_q1.json so render.py works unchanged.

Usage:
    uv run python helpers/build_edl_multi.py \\
        --questions edit_14_05_26/questions.json \\
        --transcript edit_14_05_26/transcripts/source_video.json \\
        --edit-dir edit_14_05_26/ \\
        [--silence-threshold 0.8] [--pad 0.05] [--only 1]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

FILLER_WORDS: frozenset[str] = frozenset({
    "мм", "ммм", "мммм", "ммммм",
    "эм", "эмм", "эммм",
    "э", "эх", "эхм",
    "ах", "ой", "гм", "хм",
    "ыы", "ыыы", "ым",
    "аа", "ааа", "оо", "ооо", "уу",
    "ну",    # isolated filler "ну" (not "ну и", "ну вот" — контекст не виден, рискуем)
})

SHORT_FILLER_MAX_DUR = 0.15   # слово < 150ms без контекста — филлер
SHORT_FILLER_MAX_LEN = 2       # и <= 2 символов


def is_filler(word: dict) -> bool:
    text = word.get("text", "").lower().strip(".,!?;:—-«»\"'")
    if text in FILLER_WORDS:
        return True
    dur = word.get("end", 0) - word.get("start", 0)
    if dur < SHORT_FILLER_MAX_DUR and len(text) <= SHORT_FILLER_MAX_LEN:
        return True
    return False


def is_verbal_repeat(words: list[dict], idx: int) -> bool:
    """True if word[idx] duplicates the immediately preceding kept word."""
    if idx == 0:
        return False
    cur = words[idx].get("text", "").lower().strip(".,!?;:—-«»\"'")
    if len(cur) < 3:
        return False
    for j in range(idx - 1, -1, -1):
        if words[j].get("type") == "spacing":
            continue
        prev = words[j].get("text", "").lower().strip(".,!?;:—-«»\"'")
        return cur == prev
    return False


def filter_words(all_words: list[dict], q_start: float, q_end: float) -> list[dict]:
    """Extract words in range, remove fillers and verbal repeats."""
    in_range = [
        w for w in all_words
        if w.get("start", 0) >= q_start - 0.1 and w.get("end", q_end + 1) <= q_end + 0.1
    ]
    kept: list[dict] = []
    for i, w in enumerate(in_range):
        if w.get("type") == "spacing":
            kept.append(w)
            continue
        if is_filler(w):
            continue
        if is_verbal_repeat(in_range, i):
            continue
        kept.append(w)
    return kept


def build_edl(
    filtered: list[dict],
    source_name: str,
    q_idx: int,
    silence_threshold: float,
    pad: float,
) -> dict:
    segments: list[dict] = []
    seg_words: list[dict] = []
    accumulated_silence: float = 0.0

    def flush() -> None:
        if not seg_words:
            return
        start = max(0.0, seg_words[0]["start"] - pad)
        end = seg_words[-1]["end"] + pad
        quote = " ".join(
            w["text"] for w in seg_words if w.get("type") == "word"
        )
        segments.append({
            "source": "main",
            "start": round(start, 3),
            "end": round(end, 3),
            "beat": f"q{q_idx:02d}_seg_{len(segments):03d}",
            "quote": quote,
            "reason": f"Q{q_idx:02d} clean",
        })
        seg_words.clear()

    for w in filtered:
        if w.get("type") == "spacing":
            dur = w["end"] - w["start"]
            accumulated_silence += dur
            if accumulated_silence >= silence_threshold:
                flush()
                accumulated_silence = 0.0
        else:
            accumulated_silence = 0.0
            seg_words.append(w)

    flush()

    return {
        "version": 1,
        "sources": {"main": source_name},
        "ranges": segments,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Build per-question EDL files")
    ap.add_argument("--questions", required=True, type=Path)
    ap.add_argument("--transcript", required=True, type=Path)
    ap.add_argument("--edit-dir", required=True, type=Path)
    ap.add_argument("--silence-threshold", type=float, default=0.8,
                    help="Pause duration in seconds that triggers a segment break (default 0.8)")
    ap.add_argument("--pad", type=float, default=0.05,
                    help="Padding in seconds added to each segment edge (default 0.05)")
    ap.add_argument("--only", type=int, default=None,
                    help="Process only question N (for testing)")
    args = ap.parse_args()

    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    raw = json.loads(args.transcript.read_text(encoding="utf-8-sig"))
    all_words: list[dict] = raw.get("words", [])

    print(f"Transcript words: {sum(1 for w in all_words if w.get('type')=='word')}")
    print(f"Questions: {len(questions)}\n")

    targets = [q for q in questions if args.only is None or q["idx"] == args.only]

    for q in targets:
        idx = q["idx"]
        q_start = float(q["start_sec"])
        q_end = float(q["end_sec"])

        filtered = filter_words(all_words, q_start, q_end)

        word_count_before = sum(1 for w in all_words
                                if w.get("type") == "word"
                                and w.get("start", 0) >= q_start
                                and w.get("end", 0) <= q_end)
        word_count_after = sum(1 for w in filtered if w.get("type") == "word")
        removed = word_count_before - word_count_after

        edl = build_edl(filtered, "source_video.mp4", idx, args.silence_threshold, args.pad)

        out = args.edit_dir / f"edl_q{idx:02d}.json"
        out.write_text(json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")

        src_dur = q_end - q_start
        clean_dur = sum(r["end"] - r["start"] for r in edl["ranges"])
        saved = src_dur - clean_dur

        print(f"Q{idx:02d}: {q['title']}")
        print(f"      source: {src_dur/60:.1f} min  →  clean: {clean_dur/60:.1f} min  (saved {saved:.0f}s)")
        print(f"      segments: {len(edl['ranges'])}  |  words removed: {removed}  →  {out.name}")

    print(f"\nDone.")


if __name__ == "__main__":
    main()
