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
from pathlib import Path

from docx import Document

STOP_WORDS: frozenset[str] = frozenset({
    "что", "как", "это", "для", "того", "есть", "быть", "когда",
    "который", "которая", "которые", "если", "можно", "нужно",
    "очень", "такой", "такое", "такая", "такие", "тоже", "себя",
    "своей", "своего", "своем", "только", "более", "менее",
    "чтобы", "также", "хотя", "потому", "поэтому", "однако",
})


def parse_docx(docx_path: Path) -> list[dict]:
    doc = Document(str(docx_path))
    full_text = "\n".join(p.text for p in doc.paragraphs)
    blocks = re.split(r"\*{3,}", full_text)
    questions: list[dict] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if "//" in block:
            text_part, title = block.rsplit("//", 1)
            title = title.strip()
        else:
            text_part = block
            title = f"Вопрос {len(questions) + 1}"
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


def extract_keywords(text: str, n: int = 5) -> list[str]:
    """Pick n unique, specific Cyrillic words from the question text.

    Skips the opening greeting (before first sentence boundary) and
    prefers longer words (>=7 chars) as they're more specific.
    """
    # Try to skip greeting: take text after first ';' or sentence boundary
    parts = re.split(r"[;?!]", text, maxsplit=2)
    body = text
    if len(parts) >= 2:
        first = parts[0].strip()
        remainder = " ".join(p for p in parts[1:] if p.strip()).strip()
        # Only use remainder if it's actually non-empty and first looks like a greeting
        if len(first) < 80 and remainder:
            body = remainder

    # Prefer long words (>=7 chars) — they're specific; fall back to >=5
    for min_len in (7, 5):
        words = re.findall(rf"[а-яёА-ЯЁ]{{{min_len},}}", body)
        seen: set[str] = set()
        result: list[str] = []
        for w in words:
            wl = w.lower()
            if wl not in STOP_WORDS and wl not in seen:
                seen.add(wl)
                result.append(wl)
            if len(result) >= n:
                return result

    return result


def parse_packed_md(packed_path: Path) -> list[tuple[float, str]]:
    """Returns list of (start_sec, line_text) from takes_packed.md.

    Format: '  [000.00-015.63] S0 text content here'
    Leading spaces and speaker tag (S0/S1/...) are stripped.
    """
    result: list[tuple[float, str]] = []
    for line in packed_path.read_text(encoding="utf-8").splitlines():
        # Match: optional spaces, [start-end], speaker tag, text
        m = re.search(r"\[(\d+\.\d+)-[\d.]+\]\s+\S+\s+(.*)", line)
        if m:
            result.append((float(m.group(1)), m.group(2).strip()))
    return result


def find_start(
    keywords: list[str],
    packed: list[tuple[float, str]],
    after: float = 0.0,
) -> float | None:
    """Return timestamp of earliest packed line (after `after` seconds) with >= 2 keywords.

    Uses sequential constraint so each question is found strictly after the previous one.
    """
    candidates = [(ts, text) for ts, text in packed if ts > after]

    for ts, text in candidates:
        text_lower = text.lower()
        hits = sum(1 for kw in keywords if kw in text_lower)
        if hits >= 2:
            return ts
    # Fallback: single keyword match (with sequential constraint)
    for ts, text in candidates:
        text_lower = text.lower()
        if any(kw in text_lower for kw in keywords):
            return ts
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Find Q&A boundaries from docx keywords in transcript")
    ap.add_argument("--docx", required=True, type=Path)
    ap.add_argument("--packed", required=True, type=Path)
    ap.add_argument("--video-duration", required=True, type=float)
    ap.add_argument("--edit-dir", required=True, type=Path)
    args = ap.parse_args()

    questions = parse_docx(args.docx)
    packed = parse_packed_md(args.packed)

    print(f"Parsed {len(questions)} questions from docx")
    print(f"Loaded {len(packed)} lines from transcript\n")

    prev_start: float = 0.0
    for q in questions:
        kw = extract_keywords(q["question_text"])
        q["keywords"] = kw
        q["start_sec"] = find_start(kw, packed, after=prev_start)
        if q["start_sec"] is not None:
            prev_start = q["start_sec"]
        status = f"{q['start_sec']:.1f}s" if q["start_sec"] is not None else "NOT FOUND"
        print(f"  Q{q['idx']:02d} [{status}]  {q['title']}")
        if q["start_sec"] is None:
            print(f"         keywords tried: {kw}")

    # First: fill None start_sec with previous found start as fallback
    last_found: float = 0.0
    for i, q in enumerate(questions):
        if q["start_sec"] is None:
            q["start_sec"] = last_found
            print(f"\n  WARNING Q{q['idx']}: start not found, using fallback {last_found:.1f}s")
        else:
            last_found = q["start_sec"]

    # Then: assign end_sec = next question start - 1s buffer
    for i, q in enumerate(questions):
        if i + 1 < len(questions):
            q["end_sec"] = questions[i + 1]["start_sec"] - 1.0
        else:
            q["end_sec"] = args.video_duration

    out = args.edit_dir / "questions.json"
    out.write_text(json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'─'*60}")
    print(f"Saved → {out}")
    print(f"{'─'*60}")
    for q in questions:
        s = q["start_sec"]
        e = q["end_sec"]
        if s is not None and e is not None:
            dur = e - s
            print(f"  Q{q['idx']:02d}  {s:7.1f}s – {e:7.1f}s  ({dur/60:4.1f} min)  {q['title']}")
        else:
            print(f"  Q{q['idx']:02d}  {'?':>8} – {'?':>8}              {q['title']}")


if __name__ == "__main__":
    main()
