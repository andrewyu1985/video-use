# Q&A Video Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fully automated pipeline that takes a 2-hour Q&A stream, splits it into individual answer videos, removes fillers/pauses/repeats, and composites all overlay плашки in one shot.

**Architecture:** Four new helpers (`parse_questions`, `build_edl_multi`, `apply_overlays`) plus a master `run_pipeline.py` orchestrator that calls existing `transcribe.py`, `pack_transcripts.py`, `render.py` and `crossfade_concat.py` unchanged. Each helper is stateless and restartable; the orchestrator skips steps whose output files already exist.

**Tech Stack:** Python 3.10+, python-docx, ffmpeg, Deepgram Nova-3 (existing), uv for deps.

---

## File Map

| Status | Path | Responsibility |
|--------|------|----------------|
| MODIFY | `pyproject.toml` | add python-docx dependency |
| CREATE | `helpers/parse_questions.py` | docx → questions.json with timestamps |
| CREATE | `helpers/build_edl_multi.py` | questions.json + transcript → per-question EDL files |
| CREATE | `helpers/apply_overlays.py` | clean video + overlays dir → final video with all плашки |
| CREATE | `run_pipeline.py` | orchestrate all steps, skip completed |

**Existing helpers used unchanged:**
- `helpers/transcribe.py` — Deepgram transcription
- `helpers/pack_transcripts.py` — word JSON → takes_packed.md
- `helpers/render.py` — EDL → clip segments
- `edit/crossfade_concat.py` — segments → single video with crossfades

---

## Overlay Rules Reference

Resolved from filenames in `Плашки/`:

| # | File | Format | Start | Duration | Position |
|---|------|--------|-------|----------|----------|
| 01 | Надпись Юнев Вячеслав.mov | ARGB MOV | 1s | file natural (2.4s) | bottom-right, 20px margin |
| 02 | проект Юневерсум.mov | ARGB MOV | 1s | file natural (3.0s) | bottom-left, 20px margin |
| 03 | ...Консультация Chroma key.mp4 | green-screen MP4 | 20s **AND** end−20s | 16.7s each | center |
| 04 | ...Подписаться и комментарий.mp4 | MP4 no-alpha | 60s | 10.2s | center-bottom, scale to 960px wide |
| 05 | Книга Юн1.jpg | JPG static | 70s | 5s | fullscreen scale 1920×1080 |
| 06 | Книга Юн2.png | PNG RGBA | end−70s | until end−20s | right of speaker (x=960, y=center) |
| 07 | Лого Юневерсум.mp4 | MP4 no-alpha | after content | 7.4s | appended as outro (concat) |

---

## Task 0: Setup

**Files:**
- Modify: `pyproject.toml`
- Run in: `C:\Users\andre\Developer\video-use\`

- [ ] **Step 0.1: Add python-docx to pyproject.toml**

Edit `pyproject.toml` dependencies:
```toml
dependencies = [
    "requests",
    "librosa",
    "matplotlib",
    "pillow",
    "numpy",
    "python-docx",
]
```

- [ ] **Step 0.2: Sync dependencies**

```bash
cd C:\Users\andre\Developer\video-use
uv sync
```
Expected: `python-docx` installed with no errors.

- [ ] **Step 0.3: Create edit directory and source hardlink for new video**

```powershell
$src  = "C:\Загрузки\Video test\Ответы на вопросы. Вячеслав Юнев. 14.05.26.mp4"
$edit = "C:\Загрузки\Video test\edit_14_05_26"
New-Item -ItemType Directory -Force $edit
# Hardlink with ASCII path so ffmpeg doesn't choke on Cyrillic
New-Item -ItemType HardLink -Path "$edit\source_video.mp4" -Target $src -ErrorAction SilentlyContinue
if (-not (Test-Path "$edit\source_video.mp4")) {
    Copy-Item $src "$edit\source_video.mp4"
}
New-Item -ItemType Directory -Force "$edit\transcripts"
```
Expected: `edit_14_05_26\source_video.mp4` exists (same size as original).

- [ ] **Step 0.4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "feat: add python-docx dependency for Q&A pipeline"
```

---

## Task 1: parse_questions.py — Question Boundary Detection

**Goal:** Read the docx (questions separated by `***`), find where each question appears in the packed transcript, output `questions.json` with `start_sec` / `end_sec` per question.

**Files:**
- Create: `helpers/parse_questions.py`

- [ ] **Step 1.1: Write parse_questions.py**

```python
"""Find question boundaries in a packed transcript from a DOCX questions file.

The DOCX format uses *** separators between questions. Each block:
  Line 1:  Author name
  Lines 2+: Question text // Short title

Outputs edit/questions.json:
  [{"idx": 1, "author": "...", "title": "...", "question_text": "...",
    "start_sec": 123.4, "end_sec": 456.7, "keywords": [...]}]

Usage:
    uv run python helpers/parse_questions.py \\
        --docx "C:/Загрузки/Video test/ЮН 14.05.26.docx" \\
        --packed edit_14_05_26/takes_packed.md \\
        --video-duration 6973.0 \\
        --edit-dir edit_14_05_26/
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from docx import Document

STOP_WORDS = {
    "что", "как", "это", "для", "того", "есть", "быть", "когда",
    "который", "которая", "которые", "если", "можно", "нужно",
    "очень", "такой", "такое", "такая", "такие", "тоже", "себя",
    "своей", "своего", "своем", "только", "более", "менее",
}


def parse_docx(docx_path: Path) -> list[dict]:
    doc = Document(str(docx_path))
    full_text = "\n".join(p.text for p in doc.paragraphs)
    blocks = re.split(r"\*{3,}", full_text)
    questions = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if "//" in block:
            text_part, title = block.rsplit("//", 1)
            title = title.strip()
        else:
            text_part, title = block, f"Вопрос {len(questions)+1}"
        lines = [l.strip() for l in text_part.strip().splitlines() if l.strip()]
        if not lines:
            continue
        author = lines[0]
        question_text = " ".join(lines[1:]) if len(lines) > 1 else lines[0]
        questions.append({
            "idx": len(questions) + 1,
            "author": author,
            "title": title.strip(),
            "question_text": question_text,
        })
    return questions


def extract_keywords(text: str, n: int = 6) -> list[str]:
    """Pick n longest unique non-stop Cyrillic words from text."""
    words = re.findall(r"[а-яёА-ЯЁ]{5,}", text)
    seen: set[str] = set()
    result: list[str] = []
    for w in words:
        wl = w.lower()
        if wl not in STOP_WORDS and wl not in seen:
            seen.add(wl)
            result.append(wl)
        if len(result) >= n:
            break
    return result


def parse_packed_md(packed_path: Path) -> list[tuple[float, str]]:
    """Returns list of (start_sec, line_text) from takes_packed.md."""
    result: list[tuple[float, str]] = []
    for line in packed_path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\[(\d+\.\d+)-\d+\.\d+\]\s+(.*)", line)
        if m:
            result.append((float(m.group(1)), m.group(2)))
    return result


def find_start(keywords: list[str], packed: list[tuple[float, str]]) -> float | None:
    """Return timestamp of earliest packed line containing >= 2 keywords."""
    for ts, text in packed:
        text_lower = text.lower()
        hits = sum(1 for kw in keywords if kw in text_lower)
        if hits >= 2:
            return ts
    # Fallback: any single keyword match
    for ts, text in packed:
        text_lower = text.lower()
        if any(kw in text_lower for kw in keywords):
            return ts
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docx", required=True, type=Path)
    ap.add_argument("--packed", required=True, type=Path)
    ap.add_argument("--video-duration", required=True, type=float)
    ap.add_argument("--edit-dir", required=True, type=Path)
    args = ap.parse_args()

    questions = parse_docx(args.docx)
    packed = parse_packed_md(args.packed)

    for q in questions:
        kw = extract_keywords(q["question_text"])
        q["keywords"] = kw
        q["start_sec"] = find_start(kw, packed)
        if q["start_sec"] is None:
            print(f"  WARNING Q{q['idx']} not found: {q['title']}")
            print(f"    keywords: {kw}")

    # Assign end_sec = next question start - 1s buffer
    for i, q in enumerate(questions):
        if i + 1 < len(questions):
            nxt = questions[i + 1]["start_sec"]
            q["end_sec"] = (nxt - 1.0) if nxt else None
        else:
            q["end_sec"] = args.video_duration

    # Fill None start_sec with previous end
    for i, q in enumerate(questions):
        if q["start_sec"] is None:
            q["start_sec"] = questions[i - 1]["end_sec"] if i > 0 else 0.0
            print(f"  Using fallback start Q{q['idx']}: {q['start_sec']:.1f}s")

    out = args.edit_dir / "questions.json"
    out.write_text(json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{len(questions)} questions → {out}")
    for q in questions:
        s, e = q["start_sec"], q["end_sec"]
        dur = (e - s) if e else 0
        print(f"  Q{q['idx']:02d} [{s:7.1f}–{e:7.1f}s = {dur:5.1f}s]  {q['title']}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 1.2: Run transcription on new video first (required input)**

```powershell
cd C:\Users\andre\Developer\video-use
$env:PYTHONUTF8 = "1"
uv run python helpers/transcribe.py `
    "C:\Загрузки\Video test\edit_14_05_26\source_video.mp4" `
    --edit-dir "C:\Загрузки\Video test\edit_14_05_26" `
    --language ru
```
Expected: `edit_14_05_26\transcripts\source_video.json` created (~hours, results cached).

- [ ] **Step 1.3: Pack transcript**

```powershell
uv run python helpers/pack_transcripts.py `
    --edit-dir "C:\Загрузки\Video test\edit_14_05_26"
```
Expected: `edit_14_05_26\takes_packed.md` created.

- [ ] **Step 1.4: Run parse_questions.py**

```powershell
uv run python helpers/parse_questions.py `
    --docx "C:\Загрузки\Video test\ЮН 14.05.26.docx" `
    --packed "C:\Загрузки\Video test\edit_14_05_26\takes_packed.md" `
    --video-duration 6973.077 `
    --edit-dir "C:\Загрузки\Video test\edit_14_05_26"
```
Expected output (9 questions with timestamps):
```
9 questions → edit_14_05_26/questions.json
  Q01 [  XXX.X–  YYY.Ys = ZZZ.Zs]  Как относиться к тяжелым испытаниям судьбы
  ...
```
If any question shows "not found", check `takes_packed.md` manually and adjust keywords.

- [ ] **Step 1.5: Verify questions.json looks reasonable**

Open `edit_14_05_26/questions.json`. Check:
- 9 entries
- `start_sec` values increase monotonically
- Each segment duration is > 60s (short answers would be suspicious)
- No `null` in `start_sec` or `end_sec`

- [ ] **Step 1.6: Commit**

```bash
git add helpers/parse_questions.py
git commit -m "feat(parse_questions): find Q&A boundaries from docx keywords in transcript"
```

---

## Task 2: build_edl_multi.py — Multi-Question EDL Builder

**Goal:** For each question in `questions.json`, read the Deepgram transcript, filter out fillers/pauses/verbal-repeats, and produce an EDL file compatible with `render.py`.

**Files:**
- Create: `helpers/build_edl_multi.py`

- [ ] **Step 2.1: Write build_edl_multi.py**

```python
"""Build per-question EDL files from a Deepgram transcript.

For each question boundary:
  1. Extract words in [start_sec, end_sec]
  2. Remove filler words (мм, эм, ну, э, ах, гм, ...)
  3. Remove verbal repeats (same word twice in a row)
  4. Split into segments at pauses >= SILENCE_THRESHOLD
  5. Write edl_q01.json ... edl_q09.json

EDL format is identical to existing edl_q1.json so render.py works unchanged.

Usage:
    uv run python helpers/build_edl_multi.py \\
        --questions edit_14_05_26/questions.json \\
        --transcript edit_14_05_26/transcripts/source_video.json \\
        --edit-dir edit_14_05_26/ \\
        [--silence-threshold 0.8] [--pad 0.05]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

FILLER_WORDS: frozenset[str] = frozenset({
    "мм", "ммм", "мммм", "эм", "эмм", "эммм",
    "э", "эх", "ах", "ой", "гм", "хм", "ыы", "ым",
    "ну",   # standalone filler "ну" only (not part of longer phrase)
    "аа", "оо", "уу", "да",  # only if isolated
})

# Words that are fillers only when very short (< 0.15s)
SHORT_FILLER_DURATION = 0.15


def is_filler(word: dict) -> bool:
    text = word.get("text", "").lower().strip(".,!?;:—-«»")
    if text in FILLER_WORDS:
        return True
    # Very short word utterance with no semantic content
    dur = word.get("end", 0) - word.get("start", 0)
    if dur < SHORT_FILLER_DURATION and len(text) <= 2:
        return True
    return False


def is_verbal_repeat(words: list[dict], idx: int) -> bool:
    """True if words[idx] is the same word as the previous kept word."""
    if idx == 0:
        return False
    cur = words[idx].get("text", "").lower().strip(".,!?;:—-«»")
    # Look back at previous non-spacing entry
    for j in range(idx - 1, -1, -1):
        if words[j].get("type") == "spacing":
            continue
        prev = words[j].get("text", "").lower().strip(".,!?;:—-«»")
        return cur == prev and len(cur) >= 3
    return False


def filter_words(words: list[dict], q_start: float, q_end: float) -> list[dict]:
    in_range = [
        w for w in words
        if w.get("start", 0) >= q_start and w.get("end", q_end + 1) <= q_end
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True, type=Path)
    ap.add_argument("--transcript", required=True, type=Path)
    ap.add_argument("--edit-dir", required=True, type=Path)
    ap.add_argument("--silence-threshold", type=float, default=0.8)
    ap.add_argument("--pad", type=float, default=0.05)
    args = ap.parse_args()

    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    raw = json.loads(args.transcript.read_text(encoding="utf-8-sig"))
    all_words: list[dict] = raw.get("words", [])

    for q in questions:
        idx = q["idx"]
        q_start = float(q["start_sec"])
        q_end = float(q["end_sec"])

        filtered = filter_words(all_words, q_start, q_end)
        edl = build_edl(filtered, "source_video.mp4", idx, args.silence_threshold, args.pad)

        out = args.edit_dir / f"edl_q{idx:02d}.json"
        out.write_text(json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")

        total_src = sum(r["end"] - r["start"] for r in edl["ranges"])
        print(f"  Q{idx:02d}: {len(edl['ranges'])} segments, {total_src/60:.1f} min clean  → {out.name}")

    print(f"\nDone. {len(questions)} EDL files in {args.edit_dir}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2.2: Run build_edl_multi.py**

```powershell
cd C:\Users\andre\Developer\video-use
$env:PYTHONUTF8 = "1"
uv run python helpers/build_edl_multi.py `
    --questions "C:\Загрузки\Video test\edit_14_05_26\questions.json" `
    --transcript "C:\Загрузки\Video test\edit_14_05_26\transcripts\source_video.json" `
    --edit-dir "C:\Загрузки\Video test\edit_14_05_26"
```
Expected: 9 files `edl_q01.json` ... `edl_q09.json`, each showing segment count and duration.

- [ ] **Step 2.3: Spot-check one EDL**

```powershell
python -c "
import json; from pathlib import Path
edl = json.loads(Path('C:/Загрузки/Video test/edit_14_05_26/edl_q01.json').read_text())
segs = edl['ranges']
total = sum(s['end']-s['start'] for s in segs)
print(f'Q01: {len(segs)} segments, {total/60:.1f} min, first: {segs[0][\"quote\"][:60]}')
"
```

- [ ] **Step 2.4: Commit**

```bash
git add helpers/build_edl_multi.py
git commit -m "feat(build_edl_multi): filler/pause/repeat removal for multi-question EDL"
```

---

## Task 3: apply_overlays.py — Overlay Compositor

**Goal:** Apply all 7 плашки to a finished clean video in a single ffmpeg pass. Plashka 07 is appended as an outro; all others are filter_complex overlays.

**Files:**
- Create: `helpers/apply_overlays.py`

- [ ] **Step 3.1: Write apply_overlays.py**

```python
"""Composite all overlay плашки onto a clean Q&A answer video.

Overlay rules are hard-coded by плашка number. The overlays directory
must contain files matching the OVERLAY_PATTERNS dict below.

Плашка 07 is appended as a separate outro segment via concat.
All others are applied in a single filter_complex pass.

Usage:
    uv run python helpers/apply_overlays.py \\
        --input q01_clean.mp4 \\
        --overlays-dir "C:/Загрузки/Video test/Плашки/" \\
        --output q01_final.mp4
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Pattern to find each плашка file by number
OVERLAY_PATTERNS: dict[int, str] = {
    1: r"Плашка.*?01",
    2: r"Плашка.*?02",
    3: r"Плашка.*?03",
    4: r"Плашка.*?04",
    5: r"Плашка.*?05",
    6: r"Плашка.*?06",
    7: r"Плашка.*?07",
}


def find_overlay(overlays_dir: Path, pattern: str) -> Path:
    for f in overlays_dir.iterdir():
        if re.search(pattern, f.name):
            return f
    raise FileNotFoundError(f"No file matching '{pattern}' in {overlays_dir}")


def get_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def get_video_info(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries",
         "stream=width,height,codec_name,pix_fmt",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    streams = json.loads(out.stdout)["streams"]
    video = next(s for s in streams if s.get("codec_name") not in ("aac", "mp3", "pcm_s16le", None))
    return video


def build_filter_complex(
    overlays: dict[int, Path],
    main_duration: float,
) -> tuple[list[str], str, str]:
    """Build ffmpeg -filter_complex, video output label, audio output label.

    Returns (filter_parts, video_out_label, audio_out_label).
    Input indices: 0=main, 1=p01, 2=p02, 3=p03a, 4=p03b, 5=p04, 6=p05, 7=p06
    """
    parts: list[str] = []
    v = "[0:v]"   # current video chain label
    a = "[0:a]"   # audio passthrough (we don't mix overlay audio)

    p01_dur = get_duration(overlays[1])
    p02_dur = get_duration(overlays[2])
    p03_dur = get_duration(overlays[3])
    p04_dur = get_duration(overlays[4])
    p06_end = main_duration - 20.0  # disappears when p03 appears at end

    # ── Плашка 01: ARGB MOV, bottom-right, start=1s ──────────────────────
    parts.append(
        f"[1:v]setpts=PTS-STARTPTS[p01];"
        f"{v}[p01]overlay="
        f"x=W-w-20:y=H-h-20:"
        f"enable='between(t,1,{1+p01_dur:.3f})'[v1]"
    )
    v = "[v1]"

    # ── Плашка 02: ARGB MOV, bottom-left, start=1s ───────────────────────
    parts.append(
        f"[2:v]setpts=PTS-STARTPTS[p02];"
        f"{v}[p02]overlay="
        f"x=20:y=H-h-20:"
        f"enable='between(t,1,{1+p02_dur:.3f})'[v2]"
    )
    v = "[v2]"

    # ── Плашка 03: green-screen, appears at t=20s AND t=end-20s ──────────
    # Input 3 = first occurrence, input 4 = second occurrence (same file, two inputs)
    e1_start = 20.0
    e1_end   = 20.0 + p03_dur
    e2_start = main_duration - 20.0
    e2_end   = main_duration

    parts.append(
        f"[3:v]colorkey=color=0x00ff00:similarity=0.35:blend=0.05,"
        f"setpts=PTS-STARTPTS+{e1_start}/TB[p03a];"
        f"{v}[p03a]overlay="
        f"x=(W-w)/2:y=(H-h)/2:"
        f"enable='between(t,{e1_start},{e1_end:.3f})'[v3a]"
    )
    v = "[v3a]"
    parts.append(
        f"[4:v]colorkey=color=0x00ff00:similarity=0.35:blend=0.05,"
        f"setpts=PTS-STARTPTS+{e2_start}/TB[p03b];"
        f"{v}[p03b]overlay="
        f"x=(W-w)/2:y=(H-h)/2:"
        f"enable='between(t,{e2_start:.3f},{e2_end:.3f})'[v3b]"
    )
    v = "[v3b]"

    # ── Плашка 04: MP4 no-alpha, center-bottom, t=60s ────────────────────
    parts.append(
        f"[5:v]scale=960:-1,setpts=PTS-STARTPTS+60/TB[p04];"
        f"{v}[p04]overlay="
        f"x=(W-w)/2:y=H-h-30:"
        f"enable='between(t,60,{60+p04_dur:.3f})'[v4]"
    )
    v = "[v4]"

    # ── Плашка 05: JPG static fullscreen, t=70s for 5s ───────────────────
    parts.append(
        f"[6:v]scale=1920:1080,setpts=PTS-STARTPTS+70/TB[p05];"
        f"{v}[p05]overlay=0:0:enable='between(t,70,75)'[v5]"
    )
    v = "[v5]"

    # ── Плашка 06: PNG RGBA, right side, end-70s until end-20s ───────────
    p06_start = main_duration - 70.0
    parts.append(
        f"[7:v]scale=480:-1,setpts=PTS-STARTPTS+{p06_start}/TB[p06];"
        f"{v}[p06]overlay="
        f"x=W-w-20:y=(H-h)/2:"
        f"enable='between(t,{p06_start:.3f},{p06_end:.3f})'[vfinal]"
    )
    v = "[vfinal]"

    filter_str = ";".join(parts)
    return filter_str, v, a


def apply_overlays(
    main_video: Path,
    overlays_dir: Path,
    output: Path,
    outro: Path,
) -> None:
    overlays = {n: find_overlay(overlays_dir, pat) for n, pat in OVERLAY_PATTERNS.items()}

    main_dur = get_duration(main_video)
    if main_dur < 90:
        print(f"WARNING: video is only {main_dur:.1f}s — some overlays may not trigger")

    p03_file = overlays[3]  # used twice

    # Build input list: [main, p01, p02, p03, p03, p04, p05, p06]
    inputs: list[str] = []
    for f in [main_video, overlays[1], overlays[2], p03_file, p03_file,
              overlays[4], overlays[5], overlays[6]]:
        inputs += ["-i", str(f)]

    filter_str, v_out, _ = build_filter_complex(overlays, main_dur)

    # Step 1: render main content with overlays → temp file
    tmp = output.with_suffix(".tmp.mp4")
    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_str,
        "-map", v_out,
        "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(tmp),
    ]
    print(f"Compositing overlays → {tmp.name}")
    subprocess.run(cmd, check=True)

    # Step 2: append плашка 07 as outro via concat
    concat_list = tmp.with_suffix(".concat.txt")
    concat_list.write_text(
        f"file '{tmp.resolve()}'\nfile '{outro.resolve()}'\n",
        encoding="utf-8"
    )
    cmd2 = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy",
        "-movflags", "+faststart",
        str(output),
    ]
    print(f"Appending outro → {output.name}")
    subprocess.run(cmd2, check=True)

    tmp.unlink(missing_ok=True)
    concat_list.unlink(missing_ok=True)
    print(f"Done: {output}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--overlays-dir", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()

    outro = find_overlay(args.overlays_dir, OVERLAY_PATTERNS[7])
    apply_overlays(args.input, args.overlays_dir, args.output, outro)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3.2: Test apply_overlays on Q1 output from previous project**

Use the existing `q1_crossfade.mp4` to test overlay application without waiting for full pipeline:
```powershell
cd C:\Users\andre\Developer\video-use
$env:PYTHONUTF8 = "1"
uv run python helpers/apply_overlays.py `
    --input "C:\Загрузки\Video test\edit\q1_crossfade.mp4" `
    --overlays-dir "C:\Загрузки\Video test\Плашки" `
    --output "C:\Загрузки\Video test\edit\q1_with_overlays.mp4"
```
Expected: `q1_with_overlays.mp4` created, ~95MB + outro appended.

- [ ] **Step 3.3: QC the overlay test video**

Open `q1_with_overlays.mp4` in VLC or Windows player and verify:
- [ ] t=1s: bottom-right имя и проект появились
- [ ] t=20s: консультация плашка (зелёный фон убран, видно лого)
- [ ] t=60s: подписка плашка появилась снизу
- [ ] t=70s: книга на весь экран 5 секунд
- [ ] last 70s: книга png появилась справа
- [ ] last 20s: консультация плашка второй раз
- [ ] after content: лого Юневерсум (7.4s)

If chroma key color is off: adjust `similarity=0.35` in `build_filter_complex` (higher = more aggressive).

- [ ] **Step 3.4: Commit**

```bash
git add helpers/apply_overlays.py
git commit -m "feat(apply_overlays): composite all 7 плашки in single ffmpeg pass + outro concat"
```

---

## Task 4: run_pipeline.py — Full Pipeline Orchestrator

**Goal:** Single entry point that runs all steps for all questions, skipping completed steps, logging progress.

**Files:**
- Create: `run_pipeline.py`

- [ ] **Step 4.1: Write run_pipeline.py**

```python
"""Full Q&A video pipeline orchestrator.

Steps (skipped if output already exists):
  1. transcribe     → transcripts/source_video.json
  2. pack           → takes_packed.md
  3. parse_questions → questions.json
  4. build_edl_multi → edl_q01.json ... edl_qNN.json
  5. render (per Q) → q01_clips/ ...
  6. concat (per Q) → q01_clean.mp4 ...
  7. overlays (per Q) → q01_final.mp4 ...

Usage:
    uv run python run_pipeline.py \\
        --video "C:/Загрузки/Video test/Ответы на вопросы. Вячеслав Юнев. 14.05.26.mp4" \\
        --questions-docx "C:/Загрузки/Video test/ЮН 14.05.26.docx" \\
        --overlays-dir "C:/Загрузки/Video test/Плашки" \\
        --edit-dir "C:/Загрузки/Video test/edit_14_05_26"
        [--from-step 5]   # skip to per-question rendering
        [--only-question 3]  # process only Q03
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
HELPERS = HERE / "helpers"


def uv_run(script: Path, *args: str) -> None:
    cmd = ["uv", "run", "python", str(script), *args]
    print(f"\n>>> {' '.join(cmd[:6])}")
    subprocess.run(cmd, check=True)


def run_step(label: str, output_path: Path, fn) -> None:
    if output_path.exists():
        print(f"  [skip] {label} — {output_path.name} exists")
        return
    print(f"  [run]  {label}")
    fn()
    if not output_path.exists():
        sys.exit(f"ERROR: {label} produced no output at {output_path}")
    print(f"  [done] {label}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Full Q&A video pipeline")
    ap.add_argument("--video", required=True, type=Path)
    ap.add_argument("--questions-docx", required=True, type=Path)
    ap.add_argument("--overlays-dir", required=True, type=Path)
    ap.add_argument("--edit-dir", required=True, type=Path)
    ap.add_argument("--from-step", type=int, default=1,
                    help="Start from step N (1-7). Earlier steps skipped if output exists.")
    ap.add_argument("--only-question", type=int, default=None,
                    help="Process only question N (skips steps 1-4 if outputs exist).")
    ap.add_argument("--language", default="ru")
    args = ap.parse_args()

    edit = args.edit_dir.resolve()
    edit.mkdir(parents=True, exist_ok=True)

    # Hardlink source video to ASCII path
    source_link = edit / "source_video.mp4"
    if not source_link.exists():
        try:
            source_link.hardlink_to(args.video.resolve())
        except (OSError, NotImplementedError):
            import shutil
            shutil.copy2(args.video, source_link)

    transcript_path = edit / "transcripts" / "source_video.json"
    packed_path = edit / "takes_packed.md"
    questions_path = edit / "questions.json"

    import json
    video_duration = float(
        subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(source_link)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    )

    # ── Step 1: Transcribe ────────────────────────────────────────────────
    run_step("transcribe", transcript_path, lambda: uv_run(
        HELPERS / "transcribe.py",
        str(source_link),
        "--edit-dir", str(edit),
        "--language", args.language,
    ))

    # ── Step 2: Pack transcript ───────────────────────────────────────────
    run_step("pack_transcripts", packed_path, lambda: uv_run(
        HELPERS / "pack_transcripts.py",
        "--edit-dir", str(edit),
    ))

    # ── Step 3: Parse questions ───────────────────────────────────────────
    run_step("parse_questions", questions_path, lambda: uv_run(
        HELPERS / "parse_questions.py",
        "--docx", str(args.questions_docx),
        "--packed", str(packed_path),
        "--video-duration", str(video_duration),
        "--edit-dir", str(edit),
    ))

    # ── Step 4: Build EDLs ────────────────────────────────────────────────
    questions = json.loads(questions_path.read_text(encoding="utf-8"))
    first_edl = edit / f"edl_q{questions[0]['idx']:02d}.json"
    run_step("build_edl_multi", first_edl, lambda: uv_run(
        HELPERS / "build_edl_multi.py",
        "--questions", str(questions_path),
        "--transcript", str(transcript_path),
        "--edit-dir", str(edit),
    ))

    # ── Steps 5-7: Per-question render + concat + overlays ───────────────
    targets = [q for q in questions
               if args.only_question is None or q["idx"] == args.only_question]

    for q in targets:
        idx = q["idx"]
        title = q["title"]
        edl_path = edit / f"edl_q{idx:02d}.json"
        clips_dir = edit / f"q{idx:02d}_clips"
        clean_path = edit / f"q{idx:02d}_clean.mp4"
        final_path = edit / f"q{idx:02d}_final.mp4"

        print(f"\n{'='*60}")
        print(f"Q{idx:02d}: {title}")
        print(f"{'='*60}")

        # Step 5: Render clips
        run_step(f"render Q{idx:02d}", clean_path.with_name(f"q{idx:02d}_base.mp4"),
                 lambda e=edl_path, c=clean_path: uv_run(
                     HELPERS / "render.py",
                     str(e), "-o", str(c),
                 ))

        # Step 6: Crossfade concat (crossfade_concat.py lives in edit dir)
        concat_script = edit / "crossfade_concat.py"
        if not concat_script.exists():
            # Copy from previous project's edit dir
            import shutil
            old_concat = edit.parent / "edit" / "crossfade_concat.py"
            if old_concat.exists():
                shutil.copy2(old_concat, concat_script)
            else:
                sys.exit(f"crossfade_concat.py not found. Copy it to {concat_script}")

        run_step(f"crossfade Q{idx:02d}", clean_path, lambda e=edl_path, c=clean_path: uv_run(
            concat_script,
            str(e), "-o", str(c),
        ))

        # Step 7: Apply overlays
        run_step(f"overlays Q{idx:02d}", final_path, lambda c=clean_path, f=final_path: uv_run(
            HELPERS / "apply_overlays.py",
            "--input", str(c),
            "--overlays-dir", str(args.overlays_dir),
            "--output", str(f),
        ))

    print(f"\n{'='*60}")
    print("Pipeline complete.")
    finals = sorted(edit.glob("q*_final.mp4"))
    for f in finals:
        size_mb = f.stat().st_size / 1024 / 1024
        print(f"  {f.name}  ({size_mb:.0f} MB)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4.2: Test pipeline dry run for Q01 only**

```powershell
cd C:\Users\andre\Developer\video-use
$env:PYTHONUTF8 = "1"
uv run python run_pipeline.py `
    --video "C:\Загрузки\Video test\Ответы на вопросы. Вячеслав Юнев. 14.05.26.mp4" `
    --questions-docx "C:\Загрузки\Video test\ЮН 14.05.26.docx" `
    --overlays-dir "C:\Загрузки\Video test\Плашки" `
    --edit-dir "C:\Загрузки\Video test\edit_14_05_26" `
    --only-question 1
```
Expected: `edit_14_05_26/q01_final.mp4` created. Steps that already completed (transcript, pack, parse, EDL) are skipped.

- [ ] **Step 4.3: QC q01_final.mp4**

Verify in player:
- [ ] Clean cuts (no мычание, no stutters)
- [ ] Crossfade transitions smooth
- [ ] All 7 overlay rules triggered at correct times
- [ ] Outro appended (Лого Юневерсум, 7.4s)
- [ ] No encoding artifacts

- [ ] **Step 4.4: Run full pipeline for all 9 questions**

```powershell
uv run python run_pipeline.py `
    --video "C:\Загрузки\Video test\Ответы на вопросы. Вячеслав Юнев. 14.05.26.mp4" `
    --questions-docx "C:\Загрузки\Video test\ЮН 14.05.26.docx" `
    --overlays-dir "C:\Загрузки\Video test\Плашки" `
    --edit-dir "C:\Загрузки\Video test\edit_14_05_26"
```
Expected: 9 × `q01_final.mp4` ... `q09_final.mp4`, each 5–25 min.

- [ ] **Step 4.5: Commit and push**

```bash
git add run_pipeline.py
git commit -m "feat(run_pipeline): full Q&A pipeline orchestrator with skip-completed steps"
git push fork main
```

---

## Known Issues & Adjustments

### Question boundary not found
If `parse_questions.py` warns "not found" for a question:
1. Open `takes_packed.md`, manually search for a unique phrase from that question
2. Edit `questions.json` directly: set `start_sec` to the correct timestamp
3. Re-run from `--from-step 4`

### Filler word false-positive  
If "ну" or "да" cuts legitimate speech, narrow the filter:
- Edit `FILLER_WORDS` in `build_edl_multi.py`: remove "ну" or "да"
- Re-run from `--from-step 4`

### Chroma key fringe (green halo on плашка 03)
Adjust in `apply_overlays.py`:
- Decrease `similarity` (0.35 → 0.25) to be less aggressive
- Increase `blend` (0.05 → 0.1) for softer edges

### Плашка 05 duration (JPG fullscreen)
Currently hardcoded to 5s. To change, edit line in `build_filter_complex`:
```python
f"enable='between(t,70,75)'"   # change 75 to 70+N
```

### crossfade_concat.py location
The orchestrator expects it in the edit dir. If not there, copy from `edit/crossfade_concat.py`.

---

## Self-Review

**Spec coverage:**
- ✅ Transcription → parse_questions (steps 1-2)
- ✅ Q&A splitting by questions file (step 3)
- ✅ Filler/pause/repeat removal (step 4, `build_edl_multi`)
- ✅ Per-question render + crossfade (steps 5-6, existing tools)
- ✅ Overlay application with all 7 плашки (step 7)
- ✅ Single-run orchestrator (run_pipeline.py)
- ✅ Restartable / skip-completed

**Gaps:**
- Плашка 05 duration not in spec → defaulted to 5s, documented as adjustable
- Плашка 06 end time not in spec → defaulted to end-20s (before p03 reappears)
