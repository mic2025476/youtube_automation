import feedparser
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Print the YouTube ID of the latest video for a given channel."

    def handle(self, *args, **options):
        CHANNEL_ID = "UCAHr-sT0AjrD3sBwr1eRUNg"
        RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

        feed = feedparser.parse(RSS_URL)
        if not feed.entries:
            self.stdout.write("No videos found.")
            return

        latest = feed.entries[0]
        # entry.link looks like "https://www.youtube.com/watch?v=VIDEO_ID"
        try:
            video_id = latest.link.split("v=")[1]
        except IndexError:
            self.stderr.write("Could not parse video ID from link.")
            return

        self.stdout.write(f"Latest video ID: {video_id}")
