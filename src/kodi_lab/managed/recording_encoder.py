"""Fixed entry point for an isolated encoder with one mounted output file."""

import signal
import subprocess
import sys

OUTPUT = "/recording.mp4"


def encode(rate: str, seconds: int, byte_limit: str) -> None:
    # The container also exits if an interrupted broker never closes stdin.
    signal.signal(signal.SIGALRM, lambda *_: sys.exit(1))
    signal.alarm(seconds + 15)
    subprocess.run(
        [
            "/usr/bin/ffmpeg",
            "-y",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pixel_format",
            "bgr0",
            "-video_size",
            "640x360",
            "-framerate",
            rate,
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "21",
            "-maxrate",
            "240k",
            "-bufsize",
            "480k",
            "-fs",
            byte_limit,
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            OUTPUT,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "/usr/bin/ffprobe",
            "-v",
            "error",
            "-f",
            "mov",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,avg_frame_rate:format=duration",
            "-of",
            "json",
            OUTPUT,
        ],
        check=True,
        timeout=3,
    )


if __name__ == "__main__":
    encode(sys.argv[1], int(sys.argv[2]), sys.argv[3])
