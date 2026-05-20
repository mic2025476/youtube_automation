import json
import os
import re
import feedparser
import requests
from django.core.management.base import BaseCommand
from dotenv import load_dotenv
from django.core.management import call_command

load_dotenv()
# Configuration - Set these in your environment or Django settings
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")  # e.g. "xoxb-..."
SLACK_CHANNEL = 'C0845F94RD0' #"C07NQ9E7G5S - for testing bot"  # Change to your Slack channel ID
# Google Apps Script URL for persisting the last video ID
G_SCRIPT_URL = os.getenv("G_SCRIPT_URL")  # e.g. "https://script.google.com/macros/s/your_deployment_id/exec"

class Command(BaseCommand):
    help = "Checks for new YouTube videos and notifies Slack using persistent state from Google Apps Script"

    def handle(self, *args, **options):
        try:
            self.check_for_new_video()
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error: {str(e)}"))

    def get_latest_video(self):
        channel_id = "UCAHr-sT0AjrD3sBwr1eRUNg"
        uploads_playlist_id = f"UU{channel_id[2:]}"

        feed_urls = [
            f"https://www.youtube.com/feeds/videos.xml?playlist_id={uploads_playlist_id}",
            f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
        ]

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        }

        # 1) Try RSS first
        for feed_url in feed_urls:
            try:
                self.stdout.write(f"Trying RSS feed: {feed_url}")

                response = requests.get(feed_url, headers=headers, timeout=20)

                self.stdout.write(f"RSS HTTP status: {response.status_code}")
                self.stdout.write(f"RSS content type: {response.headers.get('Content-Type')}")

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

                self.stdout.write(
                    self.style.WARNING(
                        f"RSS did not return valid feed. Status={response.status_code}"
                    )
                )

            except Exception as e:
                self.stdout.write(self.style.WARNING(f"RSS failed: {str(e)}"))

        # 2) Fallback to yt-dlp
        self.stdout.write(self.style.WARNING("RSS failed. Trying yt-dlp fallback..."))

        import yt_dlp

        channel_url = "https://www.youtube.com/@MarkMeldrum/videos"

        ydl_opts = {
            "quiet": True,
            "extract_flat": True,
            "playlistend": 1,
            "skip_download": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(channel_url, download=False)

            entries = info.get("entries", [])

            if not entries:
                raise ValueError("yt-dlp found no videos")

            latest = entries[0]
            video_id = latest.get("id")

            if not video_id:
                raise ValueError("yt-dlp found latest video but no video ID")

            return {
                "id": video_id,
                "title": latest.get("title") or "Untitled video",
                "link": f"https://www.youtube.com/watch?v={video_id}",
                "published": latest.get("upload_date"),
            }

        except Exception as e:
            raise ValueError(f"Both RSS and yt-dlp failed. Last error: {str(e)}")
    def read_last_video(self):
        """Retrieves the last processed video ID from the Google Apps Script persistent store."""
        if not G_SCRIPT_URL:
            raise ValueError("G_SCRIPT_URL environment variable not set")
        # GET request with ?action=get to the Google Apps Script web app.
        url = f"{G_SCRIPT_URL}?action=get"
        response = requests.get(url)
        if not response.ok:
            raise ValueError(f"Error retrieving last video ID: {response.text}")
        data = response.json()
        # Expecting a JSON payload like: {"lastVideoId": "XYZ"}
        return data.get("lastVideoId", None)

    def write_last_video(self, video_id):
        """Updates the last processed video ID via the Google Apps Script persistent store."""
        if not G_SCRIPT_URL:
            raise ValueError("G_SCRIPT_URL environment variable not set")
        payload = {"videoId": video_id}
        response = requests.post(G_SCRIPT_URL, json=payload)
        if not response.ok:
            raise ValueError(f"Error updating last video ID: {response.text}")


    def send_slack_notification(self, video_info):
        """Sends notification to Slack about the new video."""
        try:
            message = (
                f"New video uploaded: *{video_info['title']}*\n"
                f"Link: {video_info['link']}\n"
                f"Summary link: https://docs.google.com/spreadsheets/d/1abxJkam3ySfQKtOznEGyIrHKdG_2BBjzuOK1UV4CkaI/edit?gid=550384304#gid=550384304"
            )
            print(f"\n📢 Sending Slack Notification")
            print(f"Message:\n{message}")

            headers = {
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json"
            }

            payload = {
                "channel": SLACK_CHANNEL,  # should be like "#general" or "C12345678"
                "text": message,
                "unfurl_links": True
            }

            print(f"\n🔍 Payload being sent:")
            print(json.dumps(payload, indent=2))

            response = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers=headers,
                json=payload
            )

            print(f"\n🔁 Response status: {response.status_code}")
            print(f"Response text:\n{response.text}")

            # Parse response
            response_data = response.json()

            if not response.ok or not response_data.get("ok"):
                print("❌ Slack API responded with an error.")
                raise ValueError(f"Slack API Error: {response_data.get('error', 'Unknown error')}")

            print("✅ Notification sent successfully.")

        except Exception as e:
            print(f"\n🚨 Exception occurred while sending Slack message:")
            print(e)

    def check_for_new_video(self):
        """Main logic to check for new videos and notify Slack."""
        self.stdout.write("Checking for new videos...")
        
        # Get video info from the RSS feed.
        latest_video = self.get_latest_video()
        last_video_id = self.read_last_video()
        
        self.stdout.write(f"Latest video ID: {latest_video['id']}")
        self.stdout.write(f"Last processed ID: {last_video_id or 'None'}")
        
        # Compare with last processed video
        if latest_video['id'] != last_video_id:
            self.stdout.write(self.style.SUCCESS("New video found!"))
            #call_command('transcribe_youtube', f'--video-id={latest_video["id"]}')
            self.send_slack_notification(latest_video)
            self.write_last_video(latest_video['id'])
            self.stdout.write("Notification sent and video ID updated in persistent store")
        else:
            self.stdout.write("No new videos found")

