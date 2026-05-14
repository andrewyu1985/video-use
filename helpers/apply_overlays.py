"""Composite all overlay плашки onto a clean Q&A answer video.

Overlay rules by плашка number (auto-detected from filenames):
  01: MOV ARGB — bottom-right,  t=1s,       duration=file natural
  02: MOV ARGB — bottom-left,   t=1s,       duration=file natural
  03: MP4 green-screen — center, t=20s AND end-20s, duration=16.7s each
  04: MP4 no-alpha — center-bottom, t=60s,  duration=file natural
  05: JPG static fullscreen — t=70s,        duration=5s (adjustable)
  06: PNG RGBA — right side,    t=end-70s,  until end-20s
  07: MP4 no-alpha — appended as outro (concat after main)

Usage:
    uv run python helpers/apply_overlays.py \\
        --input q01_clean.mp4 \\
        --overlays-dir "C:/Загрузки/Video test/Плашки/" \\
        --output q01_final.mp4 \\
        [--p05-duration 5]  [--chroma-similarity 0.35]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

OVERLAY_PATTERNS: dict[int, str] = {
    1: r"Плашка.*?01|Плашка.*?№\s*01",
    2: r"Плашка.*?02|Плашка.*?№\s*02",
    3: r"Плашка.*?03|Плашка.*?№\s*03",
    4: r"Плашка.*?04|Плашка.*?№\s*04",
    5: r"Плашка.*?05|Плашка.*?№\s*05",
    6: r"Плашка.*?06|Плашка.*?№\s*06",
    7: r"Плашка.*?07|Плашка.*?№\s*07",
}


def find_overlay(overlays_dir: Path, num: int) -> Path:
    pattern = OVERLAY_PATTERNS[num]
    for f in sorted(overlays_dir.iterdir()):
        if re.search(pattern, f.name, re.IGNORECASE):
            return f
    raise FileNotFoundError(
        f"No file matching pattern for плашка {num:02d} in {overlays_dir}\n"
        f"  Files found: {[f.name for f in overlays_dir.iterdir()]}"
    )


def get_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def build_filter_complex(
    overlays: dict[int, Path],
    main_duration: float,
    p05_duration: float,
    chroma_similarity: float,
) -> str:
    """Build the ffmpeg filter_complex string for all overlays.

    Input stream indices:
      0 = main video
      1 = плашка 01 (ARGB MOV)
      2 = плашка 02 (ARGB MOV)
      3 = плашка 03 first occurrence (green-screen)
      4 = плашка 03 second occurrence (same file, separate input)
      5 = плашка 04 (MP4 no-alpha)
      6 = плашка 05 (JPG static)
      7 = плашка 06 (PNG RGBA)
    """
    p01_dur = get_duration(overlays[1])
    p02_dur = get_duration(overlays[2])
    p03_dur = get_duration(overlays[3])
    p04_dur = get_duration(overlays[4])

    e1_start = 20.0
    e1_end   = e1_start + p03_dur
    e2_start = max(e1_end + 1.0, main_duration - 20.0)
    e2_end   = min(e2_start + p03_dur, main_duration)

    p06_start = max(0.0, main_duration - 70.0)
    p06_end   = e2_start  # disappears when p03 re-appears

    p05_end = 70.0 + p05_duration

    parts: list[str] = []
    v = "[0:v]"

    # ── 01: ARGB MOV bottom-right ────────────────────────────────────────
    parts.append(
        f"[1:v]setpts=PTS-STARTPTS[p01];"
        f"{v}[p01]overlay=x=W-w-20:y=H-h-20"
        f":enable='between(t,1,{1 + p01_dur:.3f})'[v1]"
    )
    v = "[v1]"

    # ── 02: ARGB MOV bottom-left ─────────────────────────────────────────
    parts.append(
        f"[2:v]setpts=PTS-STARTPTS[p02];"
        f"{v}[p02]overlay=x=20:y=H-h-20"
        f":enable='between(t,1,{1 + p02_dur:.3f})'[v2]"
    )
    v = "[v2]"

    # ── 03a: green-screen first appearance at t=20s ───────────────────────
    parts.append(
        f"[3:v]colorkey=color=0x00ff00:similarity={chroma_similarity:.2f}:blend=0.05,"
        f"setpts=PTS-STARTPTS+{e1_start}/TB[p03a];"
        f"{v}[p03a]overlay=x=(W-w)/2:y=(H-h)/2"
        f":enable='between(t,{e1_start:.3f},{e1_end:.3f})'[v3a]"
    )
    v = "[v3a]"

    # ── 03b: green-screen second appearance at end-20s ────────────────────
    parts.append(
        f"[4:v]colorkey=color=0x00ff00:similarity={chroma_similarity:.2f}:blend=0.05,"
        f"setpts=PTS-STARTPTS+{e2_start:.3f}/TB[p03b];"
        f"{v}[p03b]overlay=x=(W-w)/2:y=(H-h)/2"
        f":enable='between(t,{e2_start:.3f},{e2_end:.3f})'[v3b]"
    )
    v = "[v3b]"

    # ── 04: MP4 no-alpha center-bottom at t=60s ───────────────────────────
    parts.append(
        f"[5:v]scale=960:-1,setpts=PTS-STARTPTS+60/TB[p04];"
        f"{v}[p04]overlay=x=(W-w)/2:y=H-h-30"
        f":enable='between(t,60,{60 + p04_dur:.3f})'[v4]"
    )
    v = "[v4]"

    # ── 05: JPG fullscreen at t=70s ───────────────────────────────────────
    parts.append(
        f"[6:v]scale=1920:1080,setpts=PTS-STARTPTS+70/TB[p05];"
        f"{v}[p05]overlay=0:0"
        f":enable='between(t,70,{p05_end:.3f})'[v5]"
    )
    v = "[v5]"

    # ── 06: PNG RGBA right side, end-70s until end-20s ────────────────────
    parts.append(
        f"[7:v]scale=480:-1,setpts=PTS-STARTPTS+{p06_start:.3f}/TB[p06];"
        f"{v}[p06]overlay=x=W-w-20:y=(H-h)/2"
        f":enable='between(t,{p06_start:.3f},{p06_end:.3f})'[vfinal]"
    )

    return ";".join(parts)


def apply_overlays(
    main_video: Path,
    overlays_dir: Path,
    output: Path,
    p05_duration: float,
    chroma_similarity: float,
) -> None:
    overlays = {n: find_overlay(overlays_dir, n) for n in range(1, 8)}

    main_dur = get_duration(main_video)
    print(f"Main video: {main_dur:.1f}s ({main_dur/60:.1f} min)")

    if main_dur < 90:
        print(f"WARNING: video is only {main_dur:.1f}s — "
              "overlays at t=60s/70s/end-70s may not trigger")

    p03_file = overlays[3]

    # Input list: main, p01, p02, p03, p03, p04, p05, p06
    inputs: list[str] = []
    for f in [main_video, overlays[1], overlays[2], p03_file, p03_file,
              overlays[4], overlays[5], overlays[6]]:
        inputs += ["-i", str(f)]

    filter_str = build_filter_complex(
        overlays, main_dur, p05_duration, chroma_similarity
    )

    # Step 1: render main content with overlays 1-6 → tmp file
    tmp = output.with_stem(output.stem + "_tmp")
    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_str,
        "-map", "[vfinal]",
        "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(tmp),
    ]
    print(f"Compositing overlays → {tmp.name}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("ffmpeg stderr (last 30 lines):")
        for line in result.stderr.splitlines()[-30:]:
            print(" ", line)
        sys.exit(f"ffmpeg failed (exit {result.returncode})")

    # Step 2: append плашка 07 as outro via concat
    outro = overlays[7]
    concat_list = output.with_stem(output.stem + "_concat")
    concat_list.write_text(
        f"file '{tmp.resolve()}'\nfile '{outro.resolve()}'\n",
        encoding="utf-8",
    )
    cmd2 = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy",
        "-movflags", "+faststart",
        str(output),
    ]
    print(f"Appending outro (плашка 07) → {output.name}")
    subprocess.run(cmd2, check=True)

    tmp.unlink(missing_ok=True)
    concat_list.unlink(missing_ok=True)

    size_mb = output.stat().st_size / 1024 / 1024
    print(f"Done: {output.name}  ({size_mb:.0f} MB)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply overlay плашки to a clean Q&A video")
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--overlays-dir", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--p05-duration", type=float, default=5.0,
                    help="Duration in seconds for плашка 05 (JPG fullscreen), default 5")
    ap.add_argument("--chroma-similarity", type=float, default=0.35,
                    help="Green-screen chroma key similarity (0.1–0.5), default 0.35")
    args = ap.parse_args()

    if not args.input.exists():
        sys.exit(f"Input not found: {args.input}")
    if not args.overlays_dir.exists():
        sys.exit(f"Overlays dir not found: {args.overlays_dir}")

    apply_overlays(
        args.input,
        args.overlays_dir,
        args.output,
        args.p05_duration,
        args.chroma_similarity,
    )


if __name__ == "__main__":
    main()
