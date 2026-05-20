"""
Django management command: check_youtube

Run weekly (Sunday night). Checks the configured YouTube channel for a new
video. If one exists, runs the full transcribe pipeline AND THEN notifies
Slack with the spreadsheet link. State (last processed video ID) is only
advanced on success, so a failed Sunday run will retry on the next run.

Usage:
    python manage.py check_youtube
"""

import json
import os
import re
import traceback

import feedparser
import requests
from django.core.management import call_command
from django.core.management.base import BaseCommand
from dotenv import load_dotenv

load_dotenv()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "D07MS6QK598")
G_SCRIPT_URL = os.getenv("G_SCRIPT_URL")
YT_CHANNEL_ID = os.getenv("YT_CHANNEL_ID", "UCAHr-sT0AjrD3sBwr1eRUNg")
YT_CHANNEL_HANDLE = os.getenv("YT_CHANNEL_HANDLE", "https://www.youtube.com/@MarkMeldrum/videos")
SUMMARY_SHEET_URL = os.getenv(
    "SUMMARY_SHEET_URL",
    "https://docs.google.com/spreadsheets/d/1abxJkam3ySfQKtOznEGyIrHKdG_2BBjzuOK1UV4CkaI/edit?gid=550384304#gid=550384304",
)


def send_slack(text):
    """Send a Slack message. Returns True on success."""
    if not SLACK_BOT_TOKEN:
        print("[slack] SLACK_BOT_TOKEN missing")
        return False
    try:
        resp = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"channel": SLACK_CHANNEL, "text": text, "unfurl_links": True},
            timeout=15,
        )
        ok = resp.ok and resp.json().get("ok")
        if not ok:
            print(f"[slack] error: {resp.text}")
        return bool(ok)
    except Exception as e:
        print(f"[slack] exception: {e}")
        return False


class Command(BaseCommand):
    help = "Check the YouTube channel for a new upload and run the full transcribe pipeline."

    def handle(self, *args, **options):
        try:
            self.check_for_new_video()
        except Exception as e:
            tb = traceback.format_exc()
            self.stdout.write(self.style.ERROR(f"check_youtube failed: {e}"))
            send_slack(
                f":rotating_light: check_youtube failed\n"
                f"Error: `{e}`\n```{tb[-1500:]}```"
            )

    # -------------------------------------------------------------------
    # Discovery
    # -------------------------------------------------------------------
    def get_latest_video(self):
        uploads_playlist_id = f"UU{YT_CHANNEL_ID[2:]}"
        feed_urls = [
            f"https://www.youtube.com/feeds/videos.xml?playlist_id={uploads_playlist_id}",
            f"https://www.youtube.com/feeds/videos.xml?channel_id={YT_CHANNEL_ID}",
        ]
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        }

        for feed_url in feed_urls:
            try:
                self.stdout.write(f"Trying RSS: {feed_url}")
                response = requests.get(feed_url, headers=headers, timeout=20)
                if response.ok and "<feed" in response.text[:1000]:
                    feed = feedparser.parse(response.content)
                    if feed.entries:
                        latest = feed.entries[0]
                        video_id = latest.get("yt_videoid")
                        if not video_id:
                            link = latest.get("link", "")
                            match = re.search(r"v=([^&]+)", link)
                            video_id = match.group(1) if match else None
                        if video_id:
                            return {
                                "id": video_id,
                                "title": latest.get("title"),
                                "link": latest.get("link") or f"https://www.youtube.com/watch?v={video_id}",
                                "published": latest.get("published"),
                            }
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"RSS failed: {e}"))

        # yt-dlp fallback
        self.stdout.write(self.style.WARNING("RSS failed — falling back to yt-dlp"))
        import yt_dlp
        ydl_opts = {
            "quiet": True,
            "extract_flat": True,
            "playlistend": 1,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(YT_CHANNEL_HANDLE, download=False)
        entries = info.get("entries", [])
        if not entries:
            raise ValueError("yt-dlp found no videos")
        latest = entries[0]
        video_id = latest.get("id")
        if not video_id:
            raise ValueError("yt-dlp found entry but no video ID")
        return {
            "id": video_id,
            "title": latest.get("title") or "Untitled video",
            "link": f"https://www.youtube.com/watch?v={video_id}",
            "published": latest.get("upload_date"),
        }

    # -------------------------------------------------------------------
    # Persistent state via Apps Script
    # -------------------------------------------------------------------
    def read_last_video(self):
        if not G_SCRIPT_URL:
            raise ValueError("G_SCRIPT_URL not set")
        resp = requests.get(f"{G_SCRIPT_URL}?action=get", timeout=20)
        if not resp.ok:
            raise ValueError(f"read state failed: {resp.text}")
        return resp.json().get("lastVideoId")

    def write_last_video(self, video_id):
        if not G_SCRIPT_URL:
            raise ValueError("G_SCRIPT_URL not set")
        resp = requests.post(G_SCRIPT_URL, json={"videoId": video_id}, timeout=20)
        if not resp.ok:
            raise ValueError(f"write state failed: {resp.text}")

    # -------------------------------------------------------------------
    # Orchestration
    # -------------------------------------------------------------------
    def notify_success(self, video_info):
        msg = (
            f":tada: New video processed: *{video_info['title']}*\n"
            f"Video: {video_info['link']}\n"
            f"Summary: {SUMMARY_SHEET_URL}"
        )
        send_slack(msg)

    def check_for_new_video(self):
        self.stdout.write("Checking for new videos…")
        latest = self.get_latest_video()
        last_id = self.read_last_video()

        self.stdout.write(f"Latest:  {latest['id']} ({latest['title']})")
        self.stdout.write(f"Last:    {last_id or 'None'}")

        if latest["id"] == last_id:
            self.stdout.write("No new video. Nothing to do.")
            return

        self.stdout.write(self.style.SUCCESS("New video found — running transcribe pipeline…"))

        # Run the full pipeline. If it raises, we DO NOT advance state,
        # so the next scheduled run will retry the same video.
        try:
            call_command("transcribe_youtube", f"--video-id={latest['id']}")
        except Exception as e:
            # transcribe_youtube has already alerted Slack with the traceback.
            self.stdout.write(self.style.ERROR(f"Pipeline failed: {e}"))
            raise

        # Pipeline succeeded — notify and advance the cursor
        self.notify_success(latest)
        self.write_last_video(latest["id"])
        self.stdout.write(self.style.SUCCESS("State advanced. Done."))