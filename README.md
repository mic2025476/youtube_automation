# YouTube Pipeline — Mark Meldrum Weekly

## What it does
Every Sunday, Mark Meldrum uploads a Market Outlook video.
This pipeline:
1. Finds the latest video on the channel
2. Downloads the audio
3. Transcribes with Whisper
4. Summarizes into 5-minute chunks via Groq (free tier)
5. Writes a new tab to the existing Google Sheet
6. Sends a Slack notification on completion or failure

## How to run (manual, weekly)

```bash
cd /Users/anirudhchawla/Downloads/youtube_automation
source env/bin/activate
python manage.py check_youtube
```

If a new video is found, it processes automatically.
If "No new video. Nothing to do." appears, just wait until Monday.

## Troubleshooting

**"Sign in to confirm you're not a bot"** → cookies expired. Refresh:
1. Install "Get cookies.txt LOCALLY" Chrome extension
2. Open incognito window, log into YouTube
3. Visit https://www.youtube.com/robots.txt
4. Export cookies, save as `cookies.txt` in this folder
5. Close incognito without revisiting youtube.com
6. Re-run the command

**To process a specific video** (e.g. to re-run or test):
```bash
python manage.py transcribe_youtube --video-id=VIDEO_ID_HERE
```

## Configuration
All secrets live in `.env` (not committed):
- `GROQ_API_KEY` — Groq API for summarization
- `WEB_APP_URL` — Apps Script endpoint for writing to sheet
- `G_SCRIPT_URL` — Apps Script endpoint for last-video-ID state
- `SLACK_BOT_TOKEN` and `SLACK_CHANNEL` — failure/success notifications