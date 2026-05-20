"""
Django management command: transcribe_youtube

End-to-end pipeline:
  1. Download audio from a YouTube video (yt-dlp + cookies.txt)
  2. Transcribe with Whisper (word-level timestamps)
  3. Summarize ALL chunks via OpenAI Chat Completions
  4. Push parsed chunks straight to Google Sheets (no manual copy/paste)
  5. On any failure -> Slack alert

Usage:
    python manage.py transcribe_youtube --video-id=<VIDEO_ID>
"""

import os
import re
import math
import json
import datetime
import traceback
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests
import yt_dlp
import whisper
from openai import OpenAI
import time
from django.core.management.base import BaseCommand
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
YT_COOKIES_PATH = os.getenv("YT_COOKIES_PATH", "cookies.txt")
WEB_APP_URL = os.getenv("WEB_APP_URL")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "D07MS6QK598")
AUDIO_DIR = os.getenv("AUDIO_DIR", "audio_cache")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")

os.makedirs(AUDIO_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Slack helper (used for failure alerts)
# ---------------------------------------------------------------------------
def send_slack_alert(text):
    """Best-effort Slack notification. Never raises."""
    if not SLACK_BOT_TOKEN:
        print("[slack] SLACK_BOT_TOKEN not set, skipping alert")
        return
    try:
        requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"channel": SLACK_CHANNEL, "text": text},
            timeout=15,
        )
    except Exception as e:
        print(f"[slack] alert failed: {e}")
# ---------------------------------------------------------------------------
# Step 1: download audio
# ---------------------------------------------------------------------------
def download_audio(video_url, output_dir):
    """Download audio as mp3. Tries multiple format selectors as a fallback."""
    selectors = ["bestaudio/best", "bestaudio", "best"]

    last_error = None
    for fmt in selectors:
        ydl_opts = {
            "cookiefile": YT_COOKIES_PATH,
            "format": fmt,
            "outtmpl": os.path.join(output_dir, "%(id)s.%(ext)s"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "quiet": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
                video_id = info.get("id")
                filename = os.path.join(output_dir, f"{video_id}.mp3")
                if os.path.exists(filename):
                    size_mb = os.path.getsize(filename) / (1024 * 1024)
                    print(f"[download] format={fmt!r} -> {filename} ({size_mb:.2f} MB)")
                    return filename, info.get("duration")
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            print(f"[download] format={fmt!r} failed: {e}")
            continue

    raise RuntimeError(
        f"All format selectors failed. Last error: {last_error}. "
        "Cookies may be stale — try running `python manage.py refresh_cookies`."
    )


# ---------------------------------------------------------------------------
# Step 2: transcribe
# ---------------------------------------------------------------------------
def transcribe_audio(audio_path, transcript_file):
    """
    Transcribe with Whisper. Writes a per-video transcript file (so different
    videos don't reuse each other's transcripts — fixing the bug in the
    previous version that used a single shared 'transcript.txt').
    """
    if os.path.exists(transcript_file) and os.path.getsize(transcript_file) > 0:
        print(f"[transcribe] reusing cached {transcript_file}")
        segments = []
        with open(transcript_file, "r", encoding="utf-8") as f:
            for line in f:
                m = re.match(
                    r"\[(?P<start>[\d\.]+)-(?P<end>[\d\.]+)\]\s*(?P<text>.*)",
                    line,
                )
                if m:
                    segments.append({
                        "start": float(m.group("start")),
                        "end": float(m.group("end")),
                        "text": m.group("text"),
                    })
        if segments:
            return segments

    print(f"[transcribe] loading Whisper model: {WHISPER_MODEL}")
    model = whisper.load_model(WHISPER_MODEL)
    print("[transcribe] running transcription…")
    result = model.transcribe(audio_path, word_timestamps=True)

    with open(transcript_file, "w", encoding="utf-8") as f:
        for seg in result["segments"]:
            f.write(f"[{seg['start']:.1f}-{seg['end']:.1f}] {seg['text']}\n")

    print(f"[transcribe] wrote {transcript_file}")
    return result["segments"]


# ---------------------------------------------------------------------------
# Step 3: summarize ALL chunks
# ---------------------------------------------------------------------------
def summarize_text(segments, total_duration, video_url):
    """
    Summarize the full transcript in ~5-minute chunks using OpenAI Chat Completions.
    Returns the concatenated summary text in the same format your sheet parser expects.
    """
    if not GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY not set")

    client = OpenAI(
            api_key=GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1",
        )
    chunk_size = 300  # 5 minutes
    num_chunks = max(1, math.ceil(total_duration / chunk_size))
    print(f"[summary] producing {num_chunks} chunks for a {total_duration}s video")

    all_chunks = []
    for idx in range(num_chunks):
        start_sec = idx * chunk_size
        end_sec = min(total_duration, (idx + 1) * chunk_size)

        slice_segments = [
            seg for seg in segments
            if seg["start"] < end_sec and seg["end"] > start_sec
        ]
        if not slice_segments:
            print(f"[summary] chunk {idx+1}: no segments, skipping")
            continue

        transcript_block = "\n".join(
            f"[{seg['start']:.1f}-{seg['end']:.1f}] {seg['text']}"
            for seg in slice_segments
        )

        prompt = f"""
Analyze this timestamped transcript and create a summary chunk
for the {start_sec}-{end_sec} second window of a {total_duration} second video.

Requirements:
1. Produce exactly ONE chunk for this window
2. Include 5-6 bullet points with enough detail that the reader does NOT need to watch the video
3. Maintain original timestamps

Output format (exactly):
Chunk {idx+1} (MM:SS-MM:SS) - [Title]
• Market insight 1
• Market insight 2
• Market insight 3
• Market insight 4
• Market insight 5
Link: {video_url}?t=[SS]

Transcript:
{transcript_block}
""".strip()

        try:
                    resp = client.chat.completions.create(
                        model=GROQ_MODEL,
                        messages=[
                            {"role": "system", "content": "You summarize timestamped video transcripts into structured chunks."},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.3,
                        max_tokens=2000,
                    )
                    content = resp.choices[0].message.content
                    all_chunks.append(content)
                    print(f"[summary] chunk {idx+1}/{num_chunks} done")
                    # Groq free tier: 30 RPM = 1/2s. Sleep 3s to be safe.
                    time.sleep(3)
        except Exception as e:
            print(f"[summary] chunk {idx+1} failed: {e}")
            send_slack_alert(f":warning: Summary chunk {idx+1} failed for {video_url}: {e}")
            if "429" in str(e) or "rate" in str(e).lower():
                print("[summary] rate limited, backing off 60s")
                time.sleep(60)
    if not all_chunks:
        raise RuntimeError("All summary chunks failed")

    return "\n\n".join(all_chunks)


# ---------------------------------------------------------------------------
# Step 4: push to Google Sheets
# ---------------------------------------------------------------------------
def update_google_sheet(summary_text, video_link, web_app_url):
    """Parse the summary into rows and POST to the Apps Script web app."""
    if not web_app_url:
        raise RuntimeError("WEB_APP_URL not set")

    # Normalize dashes
    summary_text = summary_text.replace("–", "-").replace("—", "-")

    def time_to_seconds(t):
        parts = t.strip().split(":")
        try:
            if len(parts) == 2:
                m, s = parts
                return int(m) * 60 + int(s)
            if len(parts) == 3:
                h, m, s = parts
                return int(h) * 3600 + int(m) * 60 + int(s)
        except Exception:
            pass
        return 0

    def seconds_to_hms(seconds):
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        parts = []
        if h:
            parts.append(f"{h}h")
        if m or h:
            parts.append(f"{m}m")
        parts.append(f"{s}s")
        return "".join(parts)

    def add_timestamp_param(url, seconds):
        split = urlsplit(url)
        base = split._replace(fragment="")
        q = [(k, v) for k, v in parse_qsl(base.query, keep_blank_values=True)
             if k.lower() != "t"]
        q.append(("t", seconds_to_hms(seconds)))
        return urlunsplit((base.scheme, base.netloc, base.path, urlencode(q), ""))

    clean = summary_text.replace("**", "")
    parts = re.split(
        r"(?=^Chunk\s+\d+\s+\([\d:]+-[\d:]+\)\s*-\s*.+$)",
        clean,
        flags=re.MULTILINE,
    )
    chunks = [p.strip() for p in parts if p.strip()]

    rows = []
    date_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    for chunk in chunks:
        lines = chunk.splitlines()
        header = lines[0]
        m = re.match(r"^Chunk\s+\d+\s+\(([\d:]+)-([\d:]+)\)\s*-\s*(.+)$", header)
        if not m:
            continue
        start_time, end_time, title = m.groups()

        bullets = []
        for line in lines[1:]:
            line = line.strip()
            if line.startswith("•"):
                text = re.sub(r"\s*\(\d{2}:\d{2}-\d{2}:\d{2}\)\s*$", "", line[1:].strip())
                bullets.append(text)

        if not bullets:
            continue

        start_sec = time_to_seconds(start_time)
        link = add_timestamp_param(video_link, start_sec)
        rows.append([
            date_str,
            video_link,
            title,
            "\n".join(bullets),
            f"{start_time}-{end_time}",
            link,
        ])
        print(f"[sheet] parsed chunk: {title} ({start_time}-{end_time})")

    if not rows:
        raise RuntimeError("No valid chunks parsed from summary")

# === TEST MODE: don't actually write to the sheet ===
    resp = requests.post(web_app_url, json={"rows": rows}, timeout=30)
    if not resp.ok:
            raise RuntimeError(f"Sheet update failed: {resp.status_code} {resp.text}")
    print(f"[sheet] added {len(rows)} rows")
    return len(rows)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------
class Command(BaseCommand):
    help = "Download, transcribe, summarize, and publish a YouTube video to Google Sheets"

    def add_arguments(self, parser):
        parser.add_argument(
            "--video-id",
            type=str,
            required=True,
            help="YouTube video ID to process",
        )

    def handle(self, *args, **options):
        video_id = options["video_id"]
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        self.stdout.write(f"Processing video: {video_id}")

        # Per-video files so different videos don't collide
        transcript_file = f"transcript_{video_id}.txt"
        summary_file = f"summary_{video_id}.txt"
        audio_path = os.path.join(AUDIO_DIR, f"{video_id}.mp3")

        try:
            # 1. Download (or reuse cached audio)
            if os.path.exists(audio_path):
                self.stdout.write(f"Using cached audio: {audio_path}")
                with yt_dlp.YoutubeDL({"quiet": True, "cookiefile": YT_COOKIES_PATH}) as ydl:
                    info = ydl.extract_info(video_url, download=False)
                duration = info.get("duration")
            else:
                self.stdout.write("Downloading audio…")
                audio_path, duration = download_audio(video_url, AUDIO_DIR)

            self.stdout.write(f"Duration: {duration}s")

            # 2. Transcribe
            self.stdout.write("Transcribing with Whisper…")
            segments = transcribe_audio(audio_path, transcript_file)

            # 3. Summarize
            # 3. Summarize (reuse cached summary if available)
            if os.path.exists(summary_file) and os.path.getsize(summary_file) > 0:
                self.stdout.write(f"Reusing cached summary: {summary_file}")
                with open(summary_file, "r", encoding="utf-8") as f:
                    summary = f.read()
            else:
                self.stdout.write("Generating summary chunks…")
                summary = summarize_text(segments, duration, video_url)
                with open(summary_file, "w", encoding="utf-8") as f:
                    f.write(summary)
                self.stdout.write(f"Saved summary -> {summary_file}")

            # 4. Push to sheet
            self.stdout.write("Updating Google Sheet…")
            num_rows = update_google_sheet(summary, video_url, WEB_APP_URL)

            self.stdout.write(self.style.SUCCESS(
                f"Done. {num_rows} chunks published for {video_id}."
            ))

        except Exception as e:
            tb = traceback.format_exc()
            err_msg = (
                f":rotating_light: Transcribe pipeline FAILED for {video_url}\n"
                f"Error: `{e}`\n"
                f"```{tb[-1500:]}```"
            )
            send_slack_alert(err_msg)
            # Re-raise so the caller (check_youtube) sees the failure and
            # does NOT advance the last-processed video ID.
            raise