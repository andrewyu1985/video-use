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


def extract_keywords(text: str, n: int = 6) -> list[str]:
    """Pick n longest unique non-stop Cyrillic words (>=5 chars) from text."""
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

    for q in questions:
        kw = extract_keywords(q["question_text"])
        q["keywords"] = kw
        q["start_sec"] = find_start(kw, packed)
        status = f"{q['start_sec']:.1f}s" if q["start_sec"] is not None else "NOT FOUND"
        print(f"  Q{q['idx']:02d} [{status}]  {q['title']}")
        if q["start_sec"] is None:
            print(f"         keywords tried: {kw}")

    # Assign end_sec = next question start - 1s buffer
    for i, q in enumerate(questions):
        if i + 1 < len(questions):
            nxt = questions[i + 1]["start_sec"]
            q["end_sec"] = (nxt - 1.0) if nxt is not None else None
        else:
            q["end_sec"] = args.video_duration

    # Fill None start_sec with previous end as fallback
    for i, q in enumerate(questions):
        if q["start_sec"] is None:
            fallback = questions[i - 1]["end_sec"] if i > 0 else 0.0
            q["start_sec"] = fallback or 0.0
            print(f"\n  WARNING Q{q['idx']}: using fallback start {q['start_sec']:.1f}s")

    out = args.edit_dir / "questions.json"
    out.write_text(json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'─'*60}")
    print(f"Saved → {out}")
    print(f"{'─'*60}")
    for q in questions:
        s = q["start_sec"]
        e = q["end_sec"]
        dur = (e - s) if (s is not None and e is not None) else 0
        print(f"  Q{q['idx']:02d}  {s:7.1f}s – {e:7.1f}s  ({dur/60:4.1f} min)  {q['title']}")


if __name__ == "__main__":
    main()
