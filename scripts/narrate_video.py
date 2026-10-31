"""Add a voice-over and subtitles to a screen recording of a demo.

    python scripts/narrate_video.py --video mock_data/recordings/demo.mp4 --timeline mock_data/recordings/_voice_cache/timeline.json --out mock_data/recordings/demo_narrated.mp4

The demo (run with --narrate, recorded with scripts/record_screen.py) wrote a timeline: when each step began, and the audio of what is said in it. This script

* finds in the VIDEO the moment the first caption appears in the narration window, and aligns the whole timeline to it (so voice and picture agree whatever the recorder's start-up delay);
* places every spoken step at its moment on one audio track;
* writes the subtitles (one cue per spoken sentence, timed by the speech service's own sentence timings) and burns them into a black strip BELOW the picture, so they never cover the
  application; the same subtitles are saved next to the video as an .srt file (and the .ass file the picture is drawn from).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import imageio_ffmpeg

ap = argparse.ArgumentParser()
ap.add_argument("--video", required=True)
ap.add_argument("--timeline", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--strip", type=int, default=130, help="height of the black strip for the subtitles, in pixels")
ap.add_argument("--voice-lag", type=float, default=0.3, help="seconds between a caption appearing and the voice starting")
ap.add_argument("--font-size", type=int, default=32, help="subtitle height in pixels of the finished video")
args = ap.parse_args()
FF = imageio_ffmpeg.get_ffmpeg_exe()
video, out = Path(args.video), Path(args.out)
timeline = [t for t in json.loads(Path(args.timeline).read_text(encoding="utf-8")) if t.get("audio")]
if not timeline:
    sys.exit("the timeline has no spoken steps: run the demo with --narrate")


def seconds_of(path: Path) -> tuple[float, int, int]:
    err = subprocess.run([FF, "-i", str(path)], capture_output=True, text=True).stderr
    import re
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err).groups()
    w, hh = re.search(r"Video:.*?(\d{3,5})x(\d{3,5})", err).groups()
    return int(h) * 3600 + int(m) * 60 + float(s), int(w), int(hh)


def first_caption_time() -> float:
    """When the first caption shows in the narration window (the right-hand column): the first moment that region changes from the blank window."""
    w, h = 96, 64
    raw = subprocess.run([FF, "-loglevel", "error", "-i", str(video), "-vf", f"fps=10,crop=iw*0.30:ih*0.80:iw*0.70:ih*0.18,scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True).stdout
    n = len(raw) // (w * h)
    frame = lambda i: raw[i * w * h:(i + 1) * w * h]  # noqa: E731
    base = frame(0)
    diffs = [sum(abs(a - b) for a, b in zip(frame(i), base)) / (w * h) for i in range(n)]
    for i in range(n - 3):
        if all(d > 1.5 for d in diffs[i:i + 3]):
            return i / 10
    raise SystemExit("could not find the first caption in the video: is the narration window on the right of the recording?")


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def srt_time(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d},{int(round((t % 1) * 1000)):03d}"


duration, width, height = seconds_of(video)
t_first = first_caption_time()
epoch_first = timeline[0]["epoch"]
print(f"video {duration:.1f} s, {width}x{height}; the first caption appears at {t_first:.1f} s; {len(timeline)} spoken steps", flush=True)

# ---- where every spoken step starts in the video, and the subtitle cues
starts, cues = [], []
for step in timeline:
    voice_at = t_first + (step["epoch"] - epoch_first) + args.voice_lag
    starts.append(voice_at)
    for cue in step["cues"]:
        lines = textwrap.wrap(cue["text"], 60)
        pages = [lines[i:i + 2] for i in range(0, len(lines), 2)]
        total = sum(len(" ".join(p)) for p in pages) or 1
        at = voice_at + cue["start"]
        span = cue["end"] - cue["start"] + 0.15
        for page in pages:
            part = span * len(" ".join(page)) / total
            cues.append((at, at + part, "\n".join(page)))
            at += part
cues = [(s, min(e, cues[i + 1][0] - 0.02) if i + 1 < len(cues) else e, text) for i, (s, e, text) in enumerate(cues)]
out.parent.mkdir(parents=True, exist_ok=True)
srt = out.with_suffix(".srt")
srt.write_text("\n".join(f"{i}\n{srt_time(s)} --> {srt_time(e)}\n{text}\n" for i, (s, e, text) in enumerate(cues, 1)), encoding="utf-8")
print(f"{len(cues)} subtitle cues -> {srt}", flush=True)

# the same cues as an ASS file in REAL pixels (an SRT is drawn on a hidden 384x288 canvas that is scaled up, which makes the size guesswork)
ass = out.with_suffix(".ass")
canvas_w, canvas_h = width, height + args.strip
ass.write_text(
    "[Script Info]\nScriptType: v4.00+\nWrapStyle: 2\nScaledBorderAndShadow: yes\n" f"PlayResX: {canvas_w}\nPlayResY: {canvas_h}\n\n"
    "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
    "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    f"Style: Default,Segoe UI,{args.font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,2,80,80,{max(8, (args.strip - 2 * args.font_size - 12) // 2)},1\n\n"
    "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    + "\n".join(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Default,,0,0,0,,{text.replace(chr(10), chr(92) + 'N')}" for a, b, text in cues) + "\n", encoding="utf-8")

# ---- one audio track: every step's speech at its moment; the subtitles in a strip under the picture
inputs = ["-i", str(video.resolve())]
for step in timeline:
    inputs += ["-i", step["audio"]]
delays = ";".join(f"[{i + 1}:a]adelay={int(max(0, t) * 1000)}|{int(max(0, t) * 1000)}[a{i}]" for i, t in enumerate(starts))
mix = "".join(f"[a{i}]" for i in range(len(timeline))) + f"amix=inputs={len(timeline)}:normalize=0:dropout_transition=0,volume=1.0[aout]"
vf = f"[0:v]pad=iw:ih+{args.strip}:0:0:color=black,ass={ass.name}:fontsdir=C\\\\:/Windows/Fonts[v]"
cmd = [FF, "-y", "-loglevel", "error", *inputs, "-filter_complex", f"{vf};{delays};{mix}", "-map", "[v]", "-map", "[aout]", "-t", f"{duration:.2f}",
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out.resolve())]
print("assembling the video ...", flush=True)
done = subprocess.run(cmd, cwd=str(out.parent), capture_output=True, text=True)
if done.returncode:
    sys.exit("ffmpeg failed:\n" + done.stderr[-1500:])
final, w2, h2 = seconds_of(out)
print(f"done: {out} ({out.stat().st_size / 1048576:.1f} MB, {final:.0f} s, {w2}x{h2})", flush=True)
