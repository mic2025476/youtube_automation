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
        """Gets latest non-live video from a channel's 'Videos' tab."""
        channel_id = 'UCAHr-sT0AjrD3sBwr1eRUNg'  # Example channel ID
        uploads_playlist_id = f"UU{channel_id[2:]}"  # Convert channel ID to uploads playlist ID
        
        self.stdout.write(f"Using uploads playlist ID: {uploads_playlist_id}")
        feed_url = f"https://www.youtube.com/feeds/videos.xml?playlist_id={uploads_playlist_id}"
        feed = feedparser.parse(feed_url)
        
        if not feed.entries:
            raise ValueError("No videos found in RSS feed")
        
        latest = feed.entries[0]
        return {
            'id': latest.yt_videoid,
            'title': latest.title,
            'link': latest.link,
            'published': latest.published
        }

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
                f"Link: {video_info['link']}*\n"
                f"Summary link: https://docs.google.com/spreadsheets/d/1zrSaXieCV8GDmMNqsWQT1VR3Q-LKAjdnPo_EfJy3DFY/edit?gid=0#gid=0"
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

