"""Full Q&A video pipeline orchestrator.

Steps (skipped automatically if output already exists):
  1. transcribe      → transcripts/source_video.json
  2. pack            → takes_packed.md
  3. parse_questions → questions.json
  4. build_edl_multi → edl_q01.json ... edl_qNN.json
  5. render (per Q)  → qNN_clean.mp4   (render.py)
  6. concat (per Q)  → qNN_clean.mp4   (crossfade_concat.py)
  7. overlays (per Q)→ qNN_final.mp4   (apply_overlays.py)

Usage:
    uv run python run_pipeline.py \\
        --video "C:/Загрузки/Video test/Ответы на вопросы. Вячеслав Юнев. 14.05.26.mp4" \\
        --questions-docx "C:/Загрузки/Video test/ЮН 14.05.26.docx" \\
        --overlays-dir "C:/Загрузки/Video test/Плашки" \\
        --edit-dir "C:/Загрузки/Video test/edit_14_05_26" \\
        [--only-question 1]   # process a single question (for testing) \\
        [--from-step 5]       # skip to step N (earlier steps skipped if output exists) \\
        [--language ru]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPERS = HERE / "helpers"


def uv_run(script: Path, *args: str) -> None:
    """Run a Python helper script via uv run."""
    cmd = ["uv", "run", "python", str(script), *args]
    short = " ".join(str(c) for c in cmd[:7])
    print(f"  $ {short}{' …' if len(cmd) > 7 else ''}")
    subprocess.run(cmd, check=True)


def run_step(label: str, output_path: Path, fn) -> None:
    """Run fn() only if output_path does not already exist."""
    if output_path.exists():
        print(f"  [skip] {label}  ({output_path.name} exists)")
        return
    print(f"  [run]  {label}")
    fn()
    if not output_path.exists():
        sys.exit(f"ERROR: {label} produced no output at {output_path}")
    print(f"  [ok]   {label}")


def get_video_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def ensure_source_link(video: Path, edit: Path) -> Path:
    """Create ASCII-named hardlink for the source video (ffmpeg Cyrillic fix)."""
    link = edit / "source_video.mp4"
    if not link.exists():
        try:
            link.hardlink_to(video.resolve())
            print(f"  hardlinked source_video.mp4")
        except (OSError, NotImplementedError):
            shutil.copy2(video, link)
            print(f"  copied source_video.mp4 (hardlink not supported)")
    return link


def ensure_crossfade_concat(edit: Path) -> Path:
    """Ensure crossfade_concat.py is in the edit dir (copy from old edit if needed)."""
    dest = edit / "crossfade_concat.py"
    if dest.exists():
        return dest
    candidates = [
        HERE / "edit" / "crossfade_concat.py",
        edit.parent / "edit" / "crossfade_concat.py",
    ]
    for src in candidates:
        if src.exists():
            shutil.copy2(src, dest)
            print(f"  copied crossfade_concat.py from {src}")
            return dest
    sys.exit(
        f"crossfade_concat.py not found. "
        f"Copy it to {dest} (original is in C:/Загрузки/Video test/edit/)"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Full Q&A video pipeline")
    ap.add_argument("--video", required=True, type=Path,
                    help="Path to the source video file")
    ap.add_argument("--questions-docx", required=True, type=Path,
                    help="Path to the .docx file with questions list")
    ap.add_argument("--overlays-dir", required=True, type=Path,
                    help="Directory containing the плашки files")
    ap.add_argument("--edit-dir", required=True, type=Path,
                    help="Working directory for all generated files")
    ap.add_argument("--only-question", type=int, default=None,
                    help="Process only question N (for per-question testing)")
    ap.add_argument("--from-step", type=int, default=1,
                    help="Force re-run from step N (1=transcribe … 7=overlays)")
    ap.add_argument("--language", default="ru",
                    help="Transcription language code (default: ru)")
    ap.add_argument("--silence-threshold", type=float, default=0.8,
                    help="Pause length (s) that triggers a segment break in EDL")
    ap.add_argument("--p05-duration", type=float, default=5.0,
                    help="Duration (s) for плашка 05 JPG fullscreen (default 5)")
    args = ap.parse_args()

    edit = args.edit_dir.resolve()
    edit.mkdir(parents=True, exist_ok=True)
    (edit / "transcripts").mkdir(exist_ok=True)

    def force(n: int, path: Path) -> None:
        """Delete output if user requested re-run from step <= n."""
        if args.from_step <= n and path.exists():
            path.unlink()

    # ── Prepare source video ────────────────────────────────────────────
    source = ensure_source_link(args.video.resolve(), edit)
    video_duration = get_video_duration(source)
    print(f"\nSource: {source.name}  ({video_duration/60:.1f} min)\n")

    transcript_path = edit / "transcripts" / "source_video.json"
    packed_path     = edit / "takes_packed.md"
    questions_path  = edit / "questions.json"

    force(1, transcript_path)
    force(2, packed_path)
    force(3, questions_path)

    # ── Step 1: Transcribe ──────────────────────────────────────────────
    print("─── Step 1: Transcribe ───────────────────────────────────────")
    run_step("transcribe", transcript_path, lambda: uv_run(
        HELPERS / "transcribe.py",
        str(source),
        "--edit-dir", str(edit),
        "--language", args.language,
    ))

    # ── Step 2: Pack transcript ─────────────────────────────────────────
    print("─── Step 2: Pack transcript ──────────────────────────────────")
    run_step("pack_transcripts", packed_path, lambda: uv_run(
        HELPERS / "pack_transcripts.py",
        "--edit-dir", str(edit),
    ))

    # ── Step 3: Parse question boundaries ──────────────────────────────
    print("─── Step 3: Parse questions ──────────────────────────────────")
    run_step("parse_questions", questions_path, lambda: uv_run(
        HELPERS / "parse_questions.py",
        "--docx", str(args.questions_docx),
        "--packed", str(packed_path),
        "--video-duration", str(video_duration),
        "--edit-dir", str(edit),
    ))

    # ── Load questions ───────────────────────────────────────────────────
    questions = json.loads(questions_path.read_text(encoding="utf-8"))
    targets = [q for q in questions
               if args.only_question is None or q["idx"] == args.only_question]

    if not targets:
        sys.exit(f"No questions matching --only-question {args.only_question}")

    # ── Step 4: Build EDL files ─────────────────────────────────────────
    print("─── Step 4: Build EDL files ──────────────────────────────────")
    for q in targets:
        edl_path = edit / f"edl_q{q['idx']:02d}.json"
        force(4, edl_path)
    first_edl = edit / f"edl_q{targets[0]['idx']:02d}.json"
    run_step("build_edl_multi", first_edl, lambda: uv_run(
        HELPERS / "build_edl_multi.py",
        "--questions", str(questions_path),
        "--transcript", str(transcript_path),
        "--edit-dir", str(edit),
        "--silence-threshold", str(args.silence_threshold),
        *(["--only", str(args.only_question)] if args.only_question else []),
    ))

    # ── Per-question steps 5-7 ──────────────────────────────────────────
    crossfade_script = ensure_crossfade_concat(edit)

    for q in targets:
        idx   = q["idx"]
        title = q["title"]
        edl_path   = edit / f"edl_q{idx:02d}.json"
        clean_path = edit / f"q{idx:02d}_clean.mp4"
        final_path = edit / f"q{idx:02d}_final.mp4"

        force(5, clean_path)
        force(6, clean_path)
        force(7, final_path)

        print(f"\n─── Q{idx:02d}: {title} ─────────────────────────────────")

        # Step 5+6: Render segments then crossfade-concat
        run_step(f"render+concat Q{idx:02d}", clean_path, lambda e=edl_path, c=clean_path: uv_run(
            crossfade_script, str(e), "-o", str(c),
        ))

        # Step 7: Apply overlays
        run_step(f"overlays Q{idx:02d}", final_path, lambda c=clean_path, f=final_path: uv_run(
            HELPERS / "apply_overlays.py",
            "--input", str(c),
            "--overlays-dir", str(args.overlays_dir),
            "--output", str(f),
            "--p05-duration", str(args.p05_duration),
        ))

    # ── Summary ─────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print("Pipeline complete.")
    finals = sorted(edit.glob("q*_final.mp4"))
    for f in finals:
        size_mb = f.stat().st_size / 1024 / 1024
        print(f"  {f.name}  ({size_mb:.0f} MB)")


if __name__ == "__main__":
    main()
