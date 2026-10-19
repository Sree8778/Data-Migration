"""Turn a raw screen recording plus the spoken sentences into the final narrated video, the voice track, subtitles and a script.

Used by record_demo.py at the end of a recording. It can also rebuild everything from a recording log, without recording again:

    python scripts/build_recording.py --log %TEMP%/record_demo.log --video mock_data/recordings/raw/page@xxxx.webm [--out mock_data/recordings] [--no-fast-forward]

The log lines "   [voice @  12.3s] sentence" give the start of each sentence and "[step n/N @12.3s]" the moment each step caption went up; the sound
files are the ones the recording already synthesised (mock_data/recordings/voice/<hash>.wav), found again from the sentence text, so the voice
and the rate must be the ones used for the recording.

Two things are done to the picture:
  * LEAD: the video file starts a little before the clock the sentences are timed on (about 1.6 s, drifting by a few tenths over ten minutes).
    It is measured, not assumed: the moment each step caption appears in the video is found and compared with when the script put it up.
  * FAST-FORWARD: stretches in which nobody is speaking and the screen is only waiting (the AI mapping, a pipeline run, a big file landing)
    are sped up, with a "Fast-forward 20x" label, so the viewer does not sit through the wait. Speech is never sped up or cut: every
    fast-forwarded stretch lies between two sentences, and the sentences are moved to where they fall on the shortened timeline.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import wave
from pathlib import Path

Event = tuple[float, float, Path, str]                              # (start s, duration s, wav, text) on the recording clock

DEFAULT_LEAD = 1.6                                                  # used only when no step marks are available to measure it
KEEP_AFTER_SPEECH, KEEP_BEFORE_SPEECH = 1.5, 1.0                    # seconds left at normal speed on both sides of a fast-forwarded stretch
MIN_STRETCH = 4.0                                                   # a stretch shorter than this is not worth speeding up
MAX_SPEED, MIN_SPEED, TARGET_SECONDS = 40.0, 2.0, 3.0               # a stretch is squeezed to about TARGET_SECONDS, within these speeds
FONT = "C\\:/Windows/Fonts/segoeuib.ttf"


def wav_for(out: Path, voice: str, rate: int, text: str) -> Path:
    return out / "voice" / (hashlib.sha1(f"{voice}|{rate}|{text}".encode()).hexdigest()[:16] + ".wav")


def duration(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def stamp(seconds: float) -> str:
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s % 1) * 1000):03d}"


def video_seconds(ffmpeg: str, video: Path) -> float:
    err = subprocess.run([ffmpeg, "-i", str(video)], capture_output=True, text=True).stderr
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def measure_lead(ffmpeg: str, video: Path, marks: list[float]) -> tuple[float, float]:
    """(a, b) so that a caption put up at script time t appears in the video at t + a + b*t. Falls back to (DEFAULT_LEAD, 0)."""
    import numpy as np
    pairs: list[tuple[float, float]] = []
    for m in marks:
        cmd = [ffmpeg, "-v", "error", "-ss", f"{max(0.0, m + 0.4):.2f}", "-t", "3.2", "-i", str(video),
               "-vf", "crop=1120:70:240:0,fps=20,scale=280:18,format=gray", "-f", "rawvideo", "-"]
        raw = subprocess.run(cmd, capture_output=True).stdout
        if len(raw) < 2 * 18 * 280:
            continue
        frames = np.frombuffer(raw[: len(raw) // (18 * 280) * 18 * 280], np.uint8).reshape(-1, 18 * 280).astype(int)
        diff = np.abs(np.diff(frames, axis=0)).mean(axis=1)
        hit = np.flatnonzero(diff > 3)
        if len(hit):
            pairs.append((m, m + 0.4 + (hit[0] + 1) / 20 - m))        # lead measured at script time m
    if len(pairs) < 4:
        return DEFAULT_LEAD, 0.0
    ts = np.array([p[0] for p in pairs])
    leads = np.array([p[1] for p in pairs])
    keep = np.abs(leads - np.median(leads)) < 0.6                  # drop a step whose caption change was hidden by other motion
    if keep.sum() < 4:
        return float(np.median(leads)), 0.0
    slope, intercept = np.polyfit(ts[keep], leads[keep], 1)
    return float(intercept), float(slope)


def plan_segments(spans: list[tuple[float, float]], total: float, fast_forward: bool) -> list[tuple[float, float, float]]:
    """Cover [0, total] (video seconds) with (start, end, speed) pieces; speed > 1 only between two spoken sentences (or after the last)."""
    if not fast_forward or not spans:
        return [(0.0, total, 1.0)]
    cuts: list[tuple[float, float, float]] = []
    gaps = [(spans[i][1], spans[i + 1][0]) for i in range(len(spans) - 1)] + [(spans[-1][1], total)]
    for end, nxt in gaps:
        a = end + KEEP_AFTER_SPEECH
        b = nxt - (KEEP_BEFORE_SPEECH if nxt < total else 0.0)
        if b - a >= MIN_STRETCH:
            cuts.append((a, b, min(MAX_SPEED, max(MIN_SPEED, (b - a) / TARGET_SECONDS))))
    pieces, at = [], 0.0
    for a, b, speed in cuts:
        if a > at:
            pieces.append((at, a, 1.0))
        pieces.append((a, b, speed))
        at = b
    if at < total:
        pieces.append((at, total, 1.0))
    return pieces


def build(events: list[Event], video: Path, out: Path, marks: list[float] | None = None, fast_forward: bool = True) -> Path:
    """Write migration_demo.mp4 (+ _voice.wav, .srt, _script.md) into `out`; returns the mp4 path."""
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    stem = out / "migration_demo"
    work = out / "voice"
    total = video_seconds(ffmpeg, video)

    a, b = measure_lead(ffmpeg, video, marks) if marks else (DEFAULT_LEAD, 0.0)
    print(f"audio/video lead: {a:.2f}s + {b * 600:.2f}s per ten minutes ({len(marks or [])} step captions measured)")
    spans = [(s + a + b * s, s + a + b * s + d) for s, d, _, _ in events]          # each sentence on the video clock
    pieces = plan_segments(spans, total, fast_forward)

    # ---- the picture: every piece is cut from the original, sped up if it is a waiting stretch, and joined
    seg_files, mapped_from = [], []                                   # mapped_from: (video start, output start, speed) of each piece
    out_clock = 0.0
    for i, (p0, p1, speed) in enumerate(pieces):
        seg = work / f"seg_{i:03d}.mp4"
        vf = f"setpts=PTS/{speed:.4f},fps=25"
        if speed > 1.0:
            label = f"Fast-forward {speed:.0f}x"
            vf += (f",drawtext=fontfile='{FONT}':text='{label}':x=w-tw-28:y=h-th-40:fontsize=34:fontcolor=white:"
                   f"box=1:boxcolor=0x1e1b4b@0.85:boxborderw=14")
        subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", f"{p0:.3f}", "-t", f"{p1 - p0:.3f}", "-i", str(video), "-vf", vf, "-an",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(seg)], check=True)
        seg_files.append(seg)
        mapped_from.append((p0, out_clock, speed))
        out_clock += (p1 - p0) / speed
    listing = work / "segments.txt"
    listing.write_text("".join(f"file '{s.as_posix()}'\n" for s in seg_files), encoding="utf-8")
    joined = work / "joined.mp4"
    subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(joined)], check=True)

    def on_output(t: float) -> float:
        """A moment of the video clock -> the same moment on the shortened timeline."""
        for p0, o0, speed in reversed(mapped_from):
            if t >= p0:
                return o0 + (t - p0) / speed
        return 0.0

    placed = [(on_output(sp[0]), d, wav, text) for sp, (_, d, wav, text) in zip(spans, events)]

    # ---- the voice track, each sentence at its place on the shortened timeline
    inputs: list[str] = []
    chains, labels = [], []
    for i, (start, _, wav, _) in enumerate(placed):
        inputs += ["-i", str(wav)]
        ms = int(start * 1000)
        chains.append(f"[{i}:a]aresample=44100,adelay={ms}|{ms}[a{i}]")
        labels.append(f"[a{i}]")
    chains.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0,volume=1.6[aout]")
    graph = work / "filter.txt"
    graph.write_text(";\n".join(chains), encoding="utf-8")
    voice_wav = out / "migration_demo_voice.wav"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", *inputs, "-filter_complex_script", str(graph), "-map", "[aout]", str(voice_wav)], check=True)
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(joined), "-i", str(voice_wav), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", f"{stem}.mp4"], check=True)
    for f in [*seg_files, joined, listing]:
        f.unlink(missing_ok=True)

    srt = [f"{i}\n{stamp(s)} --> {stamp(s + d)}\n{t}\n" for i, (s, d, _, t) in enumerate(placed, start=1)]
    Path(f"{stem}.srt").write_text("\n".join(srt), encoding="utf-8")
    Path(f"{stem}_script.md").write_text("# Voice-over script\n\n" + "\n".join(f"- **{int(s // 60)}:{int(s % 60):02d}** {t}" for s, _, _, t in placed) + "\n", encoding="utf-8")

    skipped = sum((p1 - p0) - (p1 - p0) / sp for p0, p1, sp in pieces)
    ffs = [p for p in pieces if p[2] > 1.0]
    print(f"recording {total / 60:.1f} min -> video {out_clock / 60:.1f} min; {len(ffs)} waiting stretches fast-forwarded, {skipped / 60:.1f} min saved "
          f"({', '.join(f'{p1 - p0:.0f}s at {sp:.0f}x' for p0, p1, sp in ffs)})")
    return Path(f"{stem}.mp4")


def events_from_log(log: Path, out: Path, voice: str, rate: int) -> tuple[list[Event], list[float]]:
    events: list[Event] = []
    marks: list[float] = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"\s*\[voice @\s*([\d.]+)s\] (.*)$", line)
        if m:
            wav = wav_for(out, voice, rate, m.group(2))
            events.append((float(m.group(1)), duration(wav), wav, m.group(2)))
        k = re.match(r"\[step \d+/\d+ @\s*([\d.]+)s\]", line)
        if k:
            marks.append(float(k.group(1)))
    return events, marks


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "mock_data" / "recordings"))
    ap.add_argument("--voice", default="Microsoft Zira Desktop")
    ap.add_argument("--rate", type=int, default=0)
    ap.add_argument("--no-fast-forward", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    evs, marks = events_from_log(Path(a.log), out, a.voice, a.rate)
    print(f"{len(evs)} sentences; video: {build(evs, Path(a.video), out, marks, not a.no_fast_forward)}")
