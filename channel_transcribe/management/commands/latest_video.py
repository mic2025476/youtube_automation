import feedparser
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Print the YouTube ID of the latest video for a given channel."

    def add_arguments(self, parser):
        parser.add_argument(
            '--id-only',
            action='store_true',
            help='If set, print only the raw video ID (no prefix).'
        )

    def handle(self, *args, **options):
         CHANNEL_ID = "UCAHr-sT0AjrD3sBwr1eRUNg"
         RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}" 

         feed = feedparser.parse(RSS_URL)
         if not feed.entries:
            self.stderr.write("No videos found.")
            return

         latest = feed.entries[0]
         try:
             video_id = latest.link.split("v=")[1]
         except IndexError:
            self.stderr.write("Could not parse video ID from link.")
            return

        # either print raw ID or a human‐friendly prefix
         if options['id_only']:
            self.stdout.write(video_id)
         else:
            self.stdout.write(f"Latest video ID: {video_id}")