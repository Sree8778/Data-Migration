"""Record the whole screen to an mp4 while a command runs (a live demo, for instance), then stop cleanly.

    python scripts/record_screen.py --out mock_data/recordings/demo.mp4 --start-marker SCREEN_START --stop-marker SCREEN_END -- python scripts/demo_firestore_to_sqlserver.py --key-file KEY

What is recorded is what is on the primary screen. WITHOUT markers the recording runs for the whole command, including the preparation before the windows open and the clean-up after
they close, when your own desktop is on screen: with the markers the recording starts when the command prints the start marker and stops when it prints the stop marker. The demo
scripts print SCREEN_START just before they open their windows and SCREEN_END right after they close them. Leave the screen alone while it records and close anything private first.
There is no sound. ffmpeg comes with the `imageio-ffmpeg` package. The video is H.264 (plays in any browser, VLC, Windows Media Player).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

import imageio_ffmpeg

ap = argparse.ArgumentParser()
ap.add_argument("--out", required=True)
ap.add_argument("--fps", type=int, default=15)
ap.add_argument("--lead", type=float, default=1.0, help="seconds recorded before the start marker / the command")
ap.add_argument("--tail", type=float, default=3.0, help="seconds recorded after the stop marker / the end of the command")
ap.add_argument("--start-marker", default="", help="start recording when the command prints this text (default: at once)")
ap.add_argument("--stop-marker", default="", help="stop recording when the command prints this text (default: when the command ends)")
ap.add_argument("--crop-bottom", type=int, default=0, help="pixels to cut off the bottom of the video (the taskbar), e.g. 64")
ap.add_argument("command", nargs=argparse.REMAINDER)
args = ap.parse_args()
command = args.command[1:] if args.command and args.command[0] == "--" else args.command
if not command:
    sys.exit("give the command to run after --")
if Path(command[0]).name.lower() in ("python", "python.exe", "python3"):
    command[0] = sys.executable                       # Windows does not resolve a relative interpreter path such as .venv/Scripts/python.exe

out = Path(args.out)
out.parent.mkdir(parents=True, exist_ok=True)
log = out.with_suffix(".ffmpeg.log")
ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
filters = ["pad=ceil(iw/2)*2:ceil(ih/2)*2"]
if args.crop_bottom:
    filters = [f"crop=iw:ih-{args.crop_bottom}:0:0", *filters]


def start_recording():
    logfile = open(log, "wb")
    rec = subprocess.Popen(
        [ffmpeg, "-y", "-f", "gdigrab", "-framerate", str(args.fps), "-draw_mouse", "1", "-i", "desktop", "-vf", ",".join(filters),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=logfile)
    time.sleep(args.lead)
    if rec.poll() is not None:
        sys.exit(f"the recorder stopped at once; see {log}")
    print(f"[recording to {out}]", flush=True)
    return rec


def stop_recording(rec) -> None:
    time.sleep(args.tail)
    try:
        rec.stdin.write(b"q")                      # ffmpeg finishes the file when it is told to quit
        rec.stdin.flush()
    except OSError:
        pass
    try:
        rec.wait(timeout=120)
    except subprocess.TimeoutExpired:
        rec.kill()
    print("[recording stopped]", flush=True)


started = time.time()
rec = None if args.start_marker else start_recording()
stopped = False
proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
try:
    for line in proc.stdout:
        print(line, end="", flush=True)
        if rec is None and args.start_marker and args.start_marker in line:
            rec = start_recording()
        if rec is not None and not stopped and args.stop_marker and args.stop_marker in line:
            stop_recording(rec)
            stopped = True
    code = proc.wait()
finally:
    if proc.poll() is None:
        proc.kill()
    if rec is not None and not stopped:
        stop_recording(rec)
if rec is None:
    sys.exit("the start marker never appeared: nothing was recorded")
text = log.read_text(errors="ignore")
size = re.findall(r"Video:.*?(\d{3,5}x\d{3,5})", text)
print(f"the command ended with exit code {code}; video {out} ({out.stat().st_size / 1048576:.1f} MB, {size[-1] if size else '?'})")
sys.exit(code)
