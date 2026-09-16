#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
video_frame_simplifier.py

Three-layer frame workflow for teaching/screen-recording videos:

1) 所有帧图片/
   FFmpeg decodes the full video and mpdecimate removes compression-noise / near-duplicate
   frames conservatively. Each retained visual state is named START_END.png using the
   ORIGINAL 1-based frame numbers.

2) 简化帧图片/
   Only for long runs of consecutive single-frame states (N_N.png, N+1_N+1.png, ...),
   keep representative ORIGINAL frames at approximately SAMPLE_MS intervals. The first
   and last frame of every run are always kept. Sample files are named FRAME.png.

3) 取代后图片/
   Main working set. Normal state images from 所有帧图片/ are retained, but qualifying
   consecutive single-frame runs are removed and replaced by the samples from 简化帧图片/.

The script also writes CSV indexes connecting original frame numbers with real decoded
PTS times, so subtitles can be aligned by time while exact video lookup still uses frames.

No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_HI = 1536       # 384; conservative vs FFmpeg defaults
DEFAULT_LO = 768       # 256
DEFAULT_FRAC = 0.50
DEFAULT_MIN_SINGLE_RUN = 10
DEFAULT_SAMPLE_MS = 100.0
DEFAULT_PNG_COMPRESSION = 3

ALL_DIR_NAME = "所有帧图片"
SIMPLE_DIR_NAME = "简化帧图片"
REPLACED_DIR_NAME = "取代后图片"

GENERATED_FILES = [
    "frame_index.csv",
    "timeline_all.csv",
    "timeline_simple.csv",
    "timeline_replaced.csv",
    "rapid_bursts.csv",
    "meta.json",
]
GENERATED_DIRS = [ALL_DIR_NAME, SIMPLE_DIR_NAME, REPLACED_DIR_NAME]

FLOAT_RE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
SHOWINFO_RE = re.compile(
    rf"\[showinfo@(?P<tag>before|after)\s+@[^\]]*\]\s+"
    rf"n:\s*(?P<n>\d+)\s+"
    rf"pts:\s*(?P<pts>-?\d+|NOPTS)\s+"
    rf"pts_time:(?P<pts_time>{FLOAT_RE}|N/A)"
    rf"(?:.*?duration_time:(?P<duration_time>{FLOAT_RE}|N/A))?"
)


@dataclass
class FrameInfo:
    frame: int                 # original decoded frame number, 1-based
    pts: Optional[int]
    pts_time_raw_sec: float
    duration_raw_sec: Optional[float]
    time_sec: float = 0.0      # normalized so first decoded frame = 0
    duration_sec: float = 0.0  # derived robustly after the pass


@dataclass
class StateInfo:
    image: str
    start_frame: int
    end_frame: int
    start_time_sec: float
    end_time_exclusive_sec: float

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_time_exclusive_sec - self.start_time_sec)

    @property
    def is_single_frame(self) -> bool:
        return self.start_frame == self.end_frame


@dataclass
class BurstInfo:
    burst_id: int
    state_start_index: int
    state_end_index: int
    start_frame: int
    end_frame: int
    original_single_frame_count: int
    sample_frames: list[int]


class ToolError(RuntimeError):
    pass


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def die(message: str, code: int = 2) -> None:
    eprint(f"ERROR: {message}")
    raise SystemExit(code)


def run_capture(cmd: list[str]) -> str:
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if p.returncode != 0:
        raise ToolError(
            "Command failed:\n"
            + " ".join(cmd)
            + "\n\n"
            + p.stdout[-5000:]
        )
    return p.stdout


def find_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ToolError(
            "ffmpeg was not found in PATH. Install FFmpeg first, then rerun "
            "`python video_frame_simplifier.py doctor`."
        )
    return ffmpeg


def inspect_ffmpeg(ffmpeg: str) -> dict:
    version_text = run_capture([ffmpeg, "-hide_banner", "-version"])
    first_line = version_text.splitlines()[0] if version_text.splitlines() else version_text.strip()

    filters = run_capture([ffmpeg, "-hide_banner", "-filters"])
    missing_filters = [name for name in ("mpdecimate", "showinfo") if not re.search(rf"\b{name}\b", filters)]
    if missing_filters:
        raise ToolError("FFmpeg is missing required filter(s): " + ", ".join(missing_filters))

    encoders = run_capture([ffmpeg, "-hide_banner", "-encoders"])
    if not re.search(r"\bpng\b", encoders):
        raise ToolError("This FFmpeg build does not contain the PNG encoder.")

    help_full = run_capture([ffmpeg, "-hide_banner", "-h", "full"])
    supports_fps_mode = "-fps_mode" in help_full

    return {
        "path": ffmpeg,
        "version": first_line,
        "mpdecimate": True,
        "showinfo": True,
        "png_encoder": True,
        "fps_mode": supports_fps_mode,
    }


def doctor() -> int:
    try:
        ffmpeg = find_ffmpeg()
        info = inspect_ffmpeg(ffmpeg)
    except ToolError as exc:
        eprint(f"NOT OK: {exc}")
        return 2

    print("OK")
    print(f"FFmpeg : {info['path']}")
    print(f"Version: {info['version']}")
    print("Required filters: mpdecimate=OK, showinfo=OK")
    print("PNG encoder: OK")
    print(f"Output sync option: {'-fps_mode vfr' if info['fps_mode'] else '-vsync vfr'}")
    return 0


def format_time(seconds: float) -> str:
    if not math.isfinite(seconds):
        return ""
    seconds = max(0.0, seconds)
    total_ms = int(round(seconds * 1000.0))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def safe_float(value: Optional[str]) -> Optional[float]:
    if value is None or value == "N/A":
        return None
    return float(value)


def prepare_output(root: Path, overwrite: bool) -> tuple[Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)

    conflicts: list[Path] = []
    for name in GENERATED_DIRS + GENERATED_FILES:
        p = root / name
        if p.exists():
            conflicts.append(p)

    if conflicts and not overwrite:
        names = "\n  ".join(str(p) for p in conflicts)
        raise ToolError(
            "Generated output already exists. Use --overwrite to replace only this tool's outputs:\n  "
            + names
        )

    if overwrite:
        for p in conflicts:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()

    all_dir = root / ALL_DIR_NAME
    simple_dir = root / SIMPLE_DIR_NAME
    replaced_dir = root / REPLACED_DIR_NAME
    all_dir.mkdir(parents=True, exist_ok=False)
    simple_dir.mkdir(parents=True, exist_ok=False)
    replaced_dir.mkdir(parents=True, exist_ok=False)
    return all_dir, simple_dir, replaced_dir


def link_or_copy(src: Path, dst: Path) -> str:
    """Hard-link when possible; otherwise copy. Returns 'hardlink' or 'copy'."""
    try:
        os.link(src, dst)
        return "hardlink"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


def finalize_frame_times(frames: list[FrameInfo]) -> None:
    if not frames:
        raise ToolError("No decoded video frames were observed.")

    base = frames[0].pts_time_raw_sec
    for f in frames:
        f.time_sec = f.pts_time_raw_sec - base

    positive_deltas: list[float] = []
    for a, b in zip(frames, frames[1:]):
        delta = b.pts_time_raw_sec - a.pts_time_raw_sec
        if delta > 0:
            positive_deltas.append(delta)

    fallback = statistics.median(positive_deltas) if positive_deltas else None
    if fallback is None:
        raw_durations = [f.duration_raw_sec for f in frames if f.duration_raw_sec and f.duration_raw_sec > 0]
        fallback = statistics.median(raw_durations) if raw_durations else (1.0 / 25.0)

    for i, f in enumerate(frames):
        duration: Optional[float] = None
        if i + 1 < len(frames):
            delta = frames[i + 1].pts_time_raw_sec - f.pts_time_raw_sec
            if delta > 0:
                duration = delta
        if duration is None and f.duration_raw_sec and f.duration_raw_sec > 0:
            duration = f.duration_raw_sec
        if duration is None or duration <= 0:
            duration = fallback
        f.duration_sec = duration


def frame_end_exclusive(frames: list[FrameInfo], frame_no: int) -> float:
    if frame_no < 1 or frame_no > len(frames):
        raise ToolError(f"Frame out of range: {frame_no}")
    if frame_no < len(frames):
        return frames[frame_no].time_sec  # next frame; list is zero-based
    last = frames[-1]
    return last.time_sec + last.duration_sec


def decode_first_pass(
    ffmpeg: str,
    supports_fps_mode: bool,
    video: Path,
    all_dir: Path,
    hi: int,
    lo: int,
    frac: float,
    png_compression: int,
) -> tuple[list[FrameInfo], list[StateInfo], int]:
    """
    One full decode pass:
      showinfo@before -> mpdecimate -> showinfo@after -> PNG sequence

    `before` records every original decoded frame and its PTS.
    `after` records the PTS of frames kept by mpdecimate.
    Matching those PTS values recovers exact original 1-based frame numbers.
    """
    tmp_dir = all_dir.parent / ".video_frame_simplifier_tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=False)
    tmp_pattern = str(tmp_dir / "%09d.png")

    vf = f"showinfo@before,mpdecimate=hi={hi}:lo={lo}:frac={frac},showinfo@after"
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-nostats",
        "-loglevel", "info",
        "-i", str(video),
        "-map", "0:v:0",
        "-vf", vf,
        "-an",
    ]
    if supports_fps_mode:
        cmd += ["-fps_mode", "vfr"]
    else:
        cmd += ["-vsync", "vfr"]
    cmd += [
        "-c:v", "png",
        "-compression_level", str(png_compression),
        "-start_number", "1",
        tmp_pattern,
    ]

    eprint(f"[1/4] Full decode + noise-tolerant first pass: hi={hi}, lo={lo}, frac={frac}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert proc.stderr is not None

    frames: list[FrameInfo] = []
    pts_to_latest_frame: dict[int, int] = {}
    kept_frames: list[int] = []
    tail: list[str] = []

    for line in proc.stderr:
        tail.append(line.rstrip())
        if len(tail) > 100:
            tail.pop(0)

        m = SHOWINFO_RE.search(line)
        if not m:
            continue

        tag = m.group("tag")
        pts_s = m.group("pts")
        pts_time_s = m.group("pts_time")
        duration_time_s = m.group("duration_time")

        if pts_time_s == "N/A":
            proc.kill()
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise ToolError("A decoded frame had no pts_time; cannot build a reliable time index.")

        pts = None if pts_s == "NOPTS" else int(pts_s)
        pts_time = float(pts_time_s)
        duration_time = safe_float(duration_time_s)

        if tag == "before":
            frame_no = int(m.group("n")) + 1
            # showinfo before should be strictly sequential. Refuse silent corruption.
            if frame_no != len(frames) + 1:
                proc.kill()
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise ToolError(
                    f"Unexpected decoded frame numbering: got {frame_no}, expected {len(frames)+1}."
                )
            frames.append(
                FrameInfo(
                    frame=frame_no,
                    pts=pts,
                    pts_time_raw_sec=pts_time,
                    duration_raw_sec=duration_time,
                )
            )
            if pts is not None:
                pts_to_latest_frame[pts] = frame_no

        else:  # showinfo@after
            if pts is None:
                proc.kill()
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise ToolError("A kept frame had no PTS; cannot map it to an original frame number.")
            original_frame = pts_to_latest_frame.get(pts)
            if original_frame is None:
                proc.kill()
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise ToolError(f"Could not map kept PTS {pts} back to an original frame.")
            kept_frames.append(original_frame)

    rc = proc.wait()
    if rc != 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise ToolError("FFmpeg failed. Last log lines:\n" + "\n".join(tail[-40:]))

    if not frames or not kept_frames:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise ToolError("No video frames were decoded/kept.")

    if kept_frames[0] != 1:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise ToolError(f"Unexpected first kept frame {kept_frames[0]} (expected 1).")

    if any(b <= a for a, b in zip(kept_frames, kept_frames[1:])):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise ToolError("Kept original frame numbers are not strictly increasing.")

    finalize_frame_times(frames)

    tmp_pngs = sorted(tmp_dir.glob("*.png"))
    if len(tmp_pngs) != len(kept_frames):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise ToolError(
            f"PNG count ({len(tmp_pngs)}) != kept-frame count ({len(kept_frames)})."
        )

    total_frames = len(frames)
    width = max(6, len(str(total_frames)))
    states: list[StateInfo] = []

    for i, (src, start_frame) in enumerate(zip(tmp_pngs, kept_frames)):
        end_frame = kept_frames[i + 1] - 1 if i + 1 < len(kept_frames) else total_frames
        image = f"{start_frame:0{width}d}_{end_frame:0{width}d}.png"
        dst = all_dir / image
        os.replace(src, dst)
        states.append(
            StateInfo(
                image=image,
                start_frame=start_frame,
                end_frame=end_frame,
                start_time_sec=frames[start_frame - 1].time_sec,
                end_time_exclusive_sec=frame_end_exclusive(frames, end_frame),
            )
        )

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return frames, states, width


def detect_single_frame_bursts(states: list[StateInfo], min_run: int) -> list[BurstInfo]:
    """Strictly detect runs of consecutive one-frame states only."""
    bursts: list[BurstInfo] = []
    i = 0
    next_id = 1

    while i < len(states):
        if not states[i].is_single_frame:
            i += 1
            continue

        start_i = i
        prev_frame = states[i].start_frame
        i += 1

        while i < len(states):
            s = states[i]
            if not s.is_single_frame or s.start_frame != prev_frame + 1:
                break
            prev_frame = s.start_frame
            i += 1

        end_i = i - 1
        count = end_i - start_i + 1
        if count >= min_run:
            bursts.append(
                BurstInfo(
                    burst_id=next_id,
                    state_start_index=start_i,
                    state_end_index=end_i,
                    start_frame=states[start_i].start_frame,
                    end_frame=states[end_i].end_frame,
                    original_single_frame_count=count,
                    sample_frames=[],
                )
            )
            next_id += 1

    return bursts


def nearest_frame_for_time(
    frame_times: list[float],
    target: float,
    lo_frame: int,
    hi_frame: int,
) -> int:
    """Return original frame number nearest to target, constrained to [lo_frame, hi_frame]."""
    lo_idx = lo_frame - 1
    hi_idx = hi_frame - 1
    pos = bisect.bisect_left(frame_times, target, lo=lo_idx, hi=hi_idx + 1)
    candidates: list[int] = []
    if pos <= hi_idx:
        candidates.append(pos)
    if pos - 1 >= lo_idx:
        candidates.append(pos - 1)
    if not candidates:
        return lo_frame
    best_idx = min(candidates, key=lambda idx: (abs(frame_times[idx] - target), idx))
    return best_idx + 1


def sample_burst_frames(
    burst: BurstInfo,
    frames: list[FrameInfo],
    sample_ms: float,
) -> list[int]:
    step = sample_ms / 1000.0
    times = [f.time_sec for f in frames]
    start_time = frames[burst.start_frame - 1].time_sec
    end_time = frames[burst.end_frame - 1].time_sec

    chosen = {burst.start_frame, burst.end_frame}
    target = start_time + step
    # Strictly interior targets; the last frame is always added separately.
    while target < end_time:
        chosen.add(nearest_frame_for_time(times, target, burst.start_frame, burst.end_frame))
        target += step

    return sorted(chosen)


def build_simple_and_replaced(
    all_dir: Path,
    simple_dir: Path,
    replaced_dir: Path,
    states: list[StateInfo],
    frames: list[FrameInfo],
    bursts: list[BurstInfo],
    width: int,
    sample_ms: float,
) -> tuple[list[dict], list[dict], dict[str, int]]:
    eprint(f"[2/4] Materializing {len(bursts)} qualifying strict single-frame run(s)")
    eprint(f"[3/4] Sampling those runs at ~{sample_ms:g} ms, forcing first/last frames")

    simple_rows: list[dict] = []
    replaced_rows: list[dict] = []
    link_counts = {"hardlink": 0, "copy": 0}

    # Sample each burst, and materialize 简化帧图片.
    for burst in bursts:
        burst.sample_frames = sample_burst_frames(burst, frames, sample_ms)
        for frame_no in burst.sample_frames:
            source_name = f"{frame_no:0{width}d}_{frame_no:0{width}d}.png"
            src = all_dir / source_name
            if not src.is_file():
                raise ToolError(
                    f"Expected single-frame source image does not exist: {src}\n"
                    "This should never happen for a strict single-frame burst."
                )
            image = f"{frame_no:0{width}d}.png"
            mode = link_or_copy(src, simple_dir / image)
            link_counts[mode] += 1
            f = frames[frame_no - 1]
            simple_rows.append(
                {
                    "burst_id": burst.burst_id,
                    "image": image,
                    "frame": frame_no,
                    "time": format_time(f.time_sec),
                    "time_sec": f"{f.time_sec:.6f}",
                    "burst_start_frame": burst.start_frame,
                    "burst_end_frame": burst.end_frame,
                    "source_image": source_name,
                }
            )

    # Map state-index -> burst for replacement pass.
    burst_by_start_index = {b.state_start_index: b for b in bursts}
    covered_state_indices: set[int] = set()
    for b in bursts:
        covered_state_indices.update(range(b.state_start_index, b.state_end_index + 1))

    i = 0
    order = 1
    while i < len(states):
        burst = burst_by_start_index.get(i)
        if burst is not None:
            for frame_no in burst.sample_frames:
                image = f"{frame_no:0{width}d}.png"
                src = simple_dir / image
                mode = link_or_copy(src, replaced_dir / image)
                link_counts[mode] += 1
                f = frames[frame_no - 1]
                replaced_rows.append(
                    {
                        "order": order,
                        "type": "sample",
                        "image": image,
                        "start_frame": frame_no,
                        "end_frame": frame_no,
                        "frame": frame_no,
                        "start_time": format_time(f.time_sec),
                        "end_time_exclusive": format_time(f.time_sec),
                        "time": format_time(f.time_sec),
                        "start_time_sec": f"{f.time_sec:.6f}",
                        "end_time_exclusive_sec": f"{f.time_sec:.6f}",
                        "burst_id": burst.burst_id,
                        "source": "100ms_sample_replacement",
                    }
                )
                order += 1
            i = burst.state_end_index + 1
            continue

        if i in covered_state_indices:
            # Defensive; normally skipped by jump above.
            i += 1
            continue

        s = states[i]
        src = all_dir / s.image
        mode = link_or_copy(src, replaced_dir / s.image)
        link_counts[mode] += 1
        replaced_rows.append(
            {
                "order": order,
                "type": "state",
                "image": s.image,
                "start_frame": s.start_frame,
                "end_frame": s.end_frame,
                "frame": "",
                "start_time": format_time(s.start_time_sec),
                "end_time_exclusive": format_time(s.end_time_exclusive_sec),
                "time": "",
                "start_time_sec": f"{s.start_time_sec:.6f}",
                "end_time_exclusive_sec": f"{s.end_time_exclusive_sec:.6f}",
                "burst_id": "",
                "source": "first_pass_state",
            }
        )
        order += 1
        i += 1

    return simple_rows, replaced_rows, link_counts


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_indexes(
    root: Path,
    video: Path,
    frames: list[FrameInfo],
    states: list[StateInfo],
    simple_rows: list[dict],
    replaced_rows: list[dict],
    bursts: list[BurstInfo],
    ffmpeg_info: dict,
    params: dict,
    link_counts: dict[str, int],
) -> None:
    eprint("[4/4] Writing CSV indexes and metadata")

    frame_rows = []
    for f in frames:
        frame_rows.append(
            {
                "frame": f.frame,
                "time": format_time(f.time_sec),
                "time_sec": f"{f.time_sec:.6f}",
                "pts": "" if f.pts is None else f.pts,
                "pts_time_raw_sec": f"{f.pts_time_raw_sec:.6f}",
                "duration_ms": f"{f.duration_sec * 1000.0:.3f}",
            }
        )
    write_csv(
        root / "frame_index.csv",
        ["frame", "time", "time_sec", "pts", "pts_time_raw_sec", "duration_ms"],
        frame_rows,
    )

    all_rows = []
    for s in states:
        all_rows.append(
            {
                "type": "state",
                "image": s.image,
                "start_frame": s.start_frame,
                "end_frame": s.end_frame,
                "start_time": format_time(s.start_time_sec),
                "end_time_exclusive": format_time(s.end_time_exclusive_sec),
                "start_time_sec": f"{s.start_time_sec:.6f}",
                "end_time_exclusive_sec": f"{s.end_time_exclusive_sec:.6f}",
                "duration_ms": f"{s.duration_sec * 1000.0:.3f}",
            }
        )
    write_csv(
        root / "timeline_all.csv",
        [
            "type", "image", "start_frame", "end_frame", "start_time", "end_time_exclusive",
            "start_time_sec", "end_time_exclusive_sec", "duration_ms",
        ],
        all_rows,
    )

    write_csv(
        root / "timeline_simple.csv",
        [
            "burst_id", "image", "frame", "time", "time_sec",
            "burst_start_frame", "burst_end_frame", "source_image",
        ],
        simple_rows,
    )

    write_csv(
        root / "timeline_replaced.csv",
        [
            "order", "type", "image", "start_frame", "end_frame", "frame",
            "start_time", "end_time_exclusive", "time", "start_time_sec",
            "end_time_exclusive_sec", "burst_id", "source",
        ],
        replaced_rows,
    )

    burst_rows = []
    for b in bursts:
        start_t = frames[b.start_frame - 1].time_sec
        end_t = frames[b.end_frame - 1].time_sec
        burst_rows.append(
            {
                "burst_id": b.burst_id,
                "start_frame": b.start_frame,
                "end_frame": b.end_frame,
                "start_time": format_time(start_t),
                "end_frame_time": format_time(end_t),
                "original_single_frame_count": b.original_single_frame_count,
                "sample_count": len(b.sample_frames),
                "sample_ms": params["sample_ms"],
                "sample_frames": " ".join(str(x) for x in b.sample_frames),
            }
        )
    write_csv(
        root / "rapid_bursts.csv",
        [
            "burst_id", "start_frame", "end_frame", "start_time", "end_frame_time",
            "original_single_frame_count", "sample_count", "sample_ms", "sample_frames",
        ],
        burst_rows,
    )

    meta = {
        "tool": "video_frame_simplifier.py",
        "tool_version": "2.0.0",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "input_video": str(video.resolve()),
        "ffmpeg": ffmpeg_info,
        "parameters": params,
        "counts": {
            "decoded_original_frames": len(frames),
            "first_pass_state_images": len(states),
            "strict_single_frame_bursts": len(bursts),
            "simple_sample_images": len(simple_rows),
            "replaced_working_images": len(replaced_rows),
        },
        "file_semantics": {
            ALL_DIR_NAME: (
                "START_END.png: FFmpeg mpdecimate regarded frames START..END as one "
                "noise-tolerant visual state. This does NOT mean byte/pixel-identical frames."
            ),
            SIMPLE_DIR_NAME: (
                "FRAME.png: exact original frame selected at approximately sample_ms intervals "
                "inside qualifying consecutive single-frame runs; first and last are forced."
            ),
            REPLACED_DIR_NAME: (
                "Main working set: normal first-pass state images plus sampled FRAME.png files "
                "that replace qualifying consecutive single-frame runs."
            ),
            "time": (
                "time/time_sec are based on decoded frame PTS and normalized so the first decoded "
                "video frame is 00:00:00.000. pts_time_raw_sec preserves raw decoded PTS time."
            ),
        },
        "storage": link_counts,
    }
    with (root / "meta.json").open("w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def run_pipeline(args: argparse.Namespace) -> int:
    video: Path = args.video
    root: Path = args.output_dir

    if not video.is_file():
        raise ToolError(f"Input video does not exist: {video}")
    if args.hi < 0 or args.lo < 0:
        raise ToolError("--hi and --lo must be >= 0")
    if not (0.0 <= args.frac <= 1.0):
        raise ToolError("--frac must be between 0 and 1")
    if args.min_single_run < 2:
        raise ToolError("--min-single-run must be >= 2")
    if args.sample_ms <= 0:
        raise ToolError("--sample-ms must be > 0")

    ffmpeg = find_ffmpeg()
    ffmpeg_info = inspect_ffmpeg(ffmpeg)
    all_dir, simple_dir, replaced_dir = prepare_output(root, args.overwrite)

    try:
        frames, states, width = decode_first_pass(
            ffmpeg=ffmpeg,
            supports_fps_mode=bool(ffmpeg_info["fps_mode"]),
            video=video,
            all_dir=all_dir,
            hi=args.hi,
            lo=args.lo,
            frac=args.frac,
            png_compression=args.png_compression,
        )

        bursts = detect_single_frame_bursts(states, args.min_single_run)
        eprint(
            f"      First-pass states: {len(states)}; qualifying strict single-frame runs: {len(bursts)}"
        )

        simple_rows, replaced_rows, link_counts = build_simple_and_replaced(
            all_dir=all_dir,
            simple_dir=simple_dir,
            replaced_dir=replaced_dir,
            states=states,
            frames=frames,
            bursts=bursts,
            width=width,
            sample_ms=args.sample_ms,
        )

        params = {
            "mpdecimate_hi": args.hi,
            "mpdecimate_lo": args.lo,
            "mpdecimate_frac": args.frac,
            "min_single_run": args.min_single_run,
            "sample_ms": args.sample_ms,
            "png_compression": args.png_compression,
            "frame_numbering": "1-based original decoded video frames",
            "burst_rule": (
                "strict run of consecutive states where every state is exactly one original frame"
            ),
        }

        write_indexes(
            root=root,
            video=video,
            frames=frames,
            states=states,
            simple_rows=simple_rows,
            replaced_rows=replaced_rows,
            bursts=bursts,
            ffmpeg_info=ffmpeg_info,
            params=params,
            link_counts=link_counts,
        )

    except Exception:
        # Keep completed outputs for debugging/recovery, but remove temp decode directory.
        shutil.rmtree(root / ".video_frame_simplifier_tmp", ignore_errors=True)
        raise

    print("DONE")
    print(f"Input video              : {video}")
    print(f"Decoded original frames  : {len(frames)}")
    print(f"First-pass state images  : {len(states)}")
    print(f"Single-frame bursts      : {len(bursts)}")
    print(f"Simplified sample images : {len(simple_rows)}")
    print(f"Replaced working images  : {len(replaced_rows)}")
    print(f"Output root              : {root}")
    print(f"Main model input         : {root / REPLACED_DIR_NAME}")
    print(f"Main timeline            : {root / 'timeline_replaced.csv'}")
    print(f"Fallback / detailed look : {root / ALL_DIR_NAME}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Create original, simplified, and replaced frame sets from a teaching video."
    )
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check the installed FFmpeg capabilities")

    run = sub.add_parser("run", help="process one video")
    run.add_argument("video", type=Path, help="input video file")
    run.add_argument("output_dir", type=Path, help="output root directory")
    run.add_argument(
        "--min-single-run",
        type=int,
        default=DEFAULT_MIN_SINGLE_RUN,
        help=(
            "minimum number of strictly consecutive one-frame states required before replacing "
            f"that run with timed samples (default: {DEFAULT_MIN_SINGLE_RUN})"
        ),
    )
    run.add_argument(
        "--sample-ms",
        type=float,
        default=DEFAULT_SAMPLE_MS,
        help=(
            "sampling interval inside qualifying one-frame runs, in milliseconds; "
            f"first/last frames are always kept (default: {DEFAULT_SAMPLE_MS:g})"
        ),
    )
    run.add_argument(
        "--hi", type=int, default=DEFAULT_HI,
        help=f"FFmpeg mpdecimate hi threshold (default: {DEFAULT_HI})",
    )
    run.add_argument(
        "--lo", type=int, default=DEFAULT_LO,
        help=f"FFmpeg mpdecimate lo threshold (default: {DEFAULT_LO})",
    )
    run.add_argument(
        "--frac", type=float, default=DEFAULT_FRAC,
        help=f"FFmpeg mpdecimate frac threshold (default: {DEFAULT_FRAC})",
    )
    run.add_argument(
        "--png-compression",
        type=int,
        choices=range(0, 10),
        default=DEFAULT_PNG_COMPRESSION,
        metavar="0..9",
        help=f"PNG compression level; lossless at every value (default: {DEFAULT_PNG_COMPRESSION})",
    )
    run.add_argument(
        "--overwrite",
        action="store_true",
        help="replace this tool's existing generated directories/files in output_dir",
    )
    return ap


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            raise SystemExit(doctor())
        if args.command == "run":
            raise SystemExit(run_pipeline(args))
        parser.error("unknown command")
    except ToolError as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
