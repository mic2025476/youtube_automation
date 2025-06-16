import os
import tempfile
import whisper
import yt_dlp
from django.core.management.base import BaseCommand
import requests
import datetime
import re
import openai
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Retrieve sensitive data from environment variables.
WEB_APP_URL = os.getenv("WEB_APP_URL")
# OPENAI_API_KEY will be used in the summarize_text function

def download_audio(video_url, output_dir):
    ydl_opts = {
        #'cookiefile': os.getenv('YT_COOKIES'),
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(output_dir, '%(id)s.%(ext)s'),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'quiet': True
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # Extract info first to get duration
        info_dict = ydl.extract_info(video_url, download=False)
        duration = info_dict.get('duration', None)
        
        if duration:
            # Convert duration in seconds to HH:MM:SS format
            minutes, seconds = divmod(duration, 60)
            hours, minutes = divmod(minutes, 60)
            duration_str = f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}"
            print(f"Video duration: {duration_str}")
        
        # Now download the audio
        info_dict = ydl.extract_info(video_url, download=True)
        video_id = info_dict.get("id", None)
        filename = os.path.join(output_dir, f"{video_id}.mp3")
        
        # Verify file exists and print size
        if os.path.exists(filename):
            file_size = os.path.getsize(filename) / (1024 * 1024)  # Size in MB
            print(f"Audio file saved: {filename}")
            print(f"File size: {file_size:.2f} MB")
        
    return filename, duration

def transcribe_audio(audio_path):
    transcript_file = "transcript.txt"
    
    # If transcript file exists and has content, parse and return its segments
    if os.path.exists(transcript_file) and os.path.getsize(transcript_file) > 0:
        segments = []
        with open(transcript_file, "r", encoding="utf-8") as f:
            for line in f:
                # Expecting each line in the format: "[start-end] text"
                match = re.match(r'\[(?P<start>[\d\.]+)-(?P<end>[\d\.]+)\]\s*(?P<text>.*)', line)
                if match:
                    segments.append({
                        "start": float(match.group("start")),
                        "end": float(match.group("end")),
                        "text": match.group("text")
                    })
        return segments
    # Otherwise, load the Whisper model and transcribe the audio with word-level timestamps.
    model = whisper.load_model("base")
    result = model.transcribe(audio_path, word_timestamps=True)
    
    # Save the transcript to a text file with timestamps.
    with open(transcript_file, "w", encoding="utf-8") as f:
        for segment in result["segments"]:
            f.write(f"[{segment['start']:.1f}-{segment['end']:.1f}] {segment['text']}\n")
    
    return result["segments"]

def summarize_text(segments, total_duration, video_url=None):
    import openai, os, math

    openai.api_key = os.getenv("OPENAI_API_KEY")

    # chunk size in seconds (300 s = 5 min)
    chunk_size = 300
    num_chunks = max(1, math.ceil(total_duration / chunk_size))

    def format_prompt(slice_segments, chunk_idx):
        # Reuse your exact multi-chunk prompt, just change the transcript block.
        transcript_block = "\n".join(
            f"[{seg['start']:.1f}-{seg['end']:.1f}] {seg['text']}"
            for seg in slice_segments
        )

        return f"""
Analyze this timestamped transcript and create {num_chunks} summary chunks 
covering the entire {total_duration} second video. Each chunk should be about 5 minutes.

Requirements:
1. Create exactly {num_chunks} chunks
2. Each chunk must cover ~{chunk_size} seconds of video
3. Include 5-6 bullet points and I need information such that I dont need to go and understand video
4. Maintain original timestamps

Output format for each chunk:
Chunk [N] (MM:SS-MM:SS) - [Title]
• Market insight 1
• Market insight 2
Link: [video_url]?t=[SS]

Transcript:
{transcript_block}
"""

    all_chunks = []
    for idx in range(num_chunks):
        start_sec = idx * chunk_size
        end_sec   = min(total_duration, (idx + 1) * chunk_size)

        # pick only segments overlapping this window
        slice_segments = [
            seg for seg in segments
            if seg["start"] < end_sec and seg["end"] > start_sec
        ]

        prompt = format_prompt(slice_segments, idx+1)
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You summarize timestamped video transcripts into structured chunks."},
                {"role": "user",   "content": prompt}
            ],
            temperature=0.3,
            max_tokens=2000
        )

        all_chunks.append(resp.choices[0].message.content)
        print(f'all_chunks {all_chunks}')
        break

    # join all chunk-outputs together
    return "\n\n".join(all_chunks)

def update_google_sheet(summary_text, video_link, web_app_url):
    summary_text = summary_text.replace("–", "-")
    def time_to_seconds(time_str):
        """Convert time in MM:SS format to seconds"""
        try:
            m, s = time_str.split(':')
            return int(m) * 60 + int(s)
        except:
            return 0  # fallback if time format is invalid

    def seconds_to_hms(seconds):
        """Convert seconds to YouTube's HhMmSs format (e.g., 3600s -> 1h0m0s)"""
        #m, s = divmod(seconds, 60)
        h, m = divmod(seconds, 60)
        parts = []
        if h > 0:
            parts.append(f"{int(h)}h")
        if m > 0 or h > 0:  # Include minutes even if 0 if hours exist
            parts.append(f"{int(m)}m")
        parts.append(f"{int(m)}s")
        return "".join(parts)
    
    # First split the text into chunks based on "Chunk X" pattern
    chunk_pattern = re.compile(r'^Chunk \d+ \(\d+:\d+-\d+:\d+\) - .+', re.MULTILINE)
    chunk_matches = list(chunk_pattern.finditer(summary_text))
    
    chunks = []
    for i, match in enumerate(chunk_matches):
        chunk_start = match.start()
        # The chunk ends at the next chunk or end of text
        chunk_end = chunk_matches[i+1].start() if i+1 < len(chunk_matches) else len(summary_text)
        chunk = summary_text[chunk_start:chunk_end].strip()
        chunks.append(chunk)
    
    rows = []
    
    for chunk in chunks:
        try:
            # Split into lines and process
            lines = chunk.split('\n')
            header = lines[0]
            
            # Extract time range and title
            time_match = re.search(r'\((\d+:\d+)-(\d+:\d+)\) - (.+)', header)
            if not time_match:
                continue
                
            start_time = time_match.group(1)
            end_time = time_match.group(2)
            title = time_match.group(3).strip()
            
            # Process content lines (skip header and link)
            bullet_points = []
            for line in lines[1:]:
                line = line.strip()
                if line.startswith('•'):
                    # Remove any trailing timestamps like (00:31-00:59)
                    clean_line = re.sub(r'\s*\(\d+:\d+-\d+:\d+\)\s*$', '', line)
                    bullet_points.append(clean_line)
                elif line.startswith('Link:'):
                    continue
            
            if not bullet_points:
                continue
                
            bullet_text = '\n'.join(bullet_points)
            
            # Create timestamped link
            start_seconds = time_to_seconds(start_time)
            video_link_with_time = f"{video_link}#t={seconds_to_hms(start_seconds)}"
            
            # Create row data
            row = [
                datetime.datetime.now().strftime("%Y-%m-%d"),  # Date
                video_link,  # Original video link
                title,  # Chunk title
                bullet_text,  # Bullet points
                f"{start_time}-{end_time}",  # Duration
                video_link_with_time  # Timestamped link
            ]
            rows.append(row)
            
            print(f"Processed chunk: {title} ({start_time}-{end_time})")
            
        except Exception as e:
            print(f"Error processing chunk: {str(e)}")
            print(f"Problematic chunk content:\n{chunk[:200]}...")
            continue
    
    if not rows:
        print("No valid chunks found in the summary text.")
        return
    
    # Send data to Google Sheets
    try:
        payload = {"rows": rows}
        response = requests.post(web_app_url, json=payload)
        
        if response.ok:
            print(f"Successfully added {len(rows)} chunks to Google Sheet")
        else:
            print(f"Failed to update sheet. Status: {response.status_code}, Response: {response.text}")
    except Exception as e:
        print(f"Failed to send data to Google Sheets: {str(e)}")

AUDIO_DIR = os.getenv("AUDIO_DIR", "audio_cache")
os.makedirs(AUDIO_DIR, exist_ok=True)

class Command(BaseCommand):
    help = "Process YouTube video with accurate timestamped chunks"

    def add_arguments(self, parser):
        parser.add_argument(
            '--video-id',
            type=str,
            help='YouTube video ID to process',
            default=os.getenv("VIDEO_URL", "https://www.youtube.com/watch?v=kMb7mM_vxWo").split('v=')[-1]
        )

    def clear_previous_files(self):
        """Clear previous output files"""
        files_to_clear = ["summary.txt", "transcript.txt"]
        for file in files_to_clear:
            try:
                if os.path.exists(file):
                    with open(file, "w") as f:
                        f.write("")  # Empty the file
                    print(f"Cleared {file}")
                else:
                    print(f"{file} doesn't exist - will be created")
            except Exception as e:
                print(f"Error clearing {file}: {str(e)}")

    def handle(self, *args, **options):
        video_id = options['video_id']
        VIDEO_URL = f"https://www.youtube.com/watch?v={video_id}"
        self.stdout.write(f'Processing video: {video_id}')

        # Path where we expect the .mp3 to live
        audio_path = os.path.join(AUDIO_DIR, f"{video_id}.mp3")

        if os.path.exists(audio_path):
            # Audio is already downloaded—just probe its duration
            self.stdout.write(f"Found existing audio: {audio_path}")
            # Optionally you can re-extract duration from the file, but
            # simplest is to store metadata next to it or re-run yt_dlp in no-download mode:
            with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
                info = ydl.extract_info(VIDEO_URL, download=False)
                duration = info.get('duration')
        else:
            # First time: download into that persistent folder
            self.stdout.write("Downloading audio…")
            audio_path, duration = download_audio(VIDEO_URL, AUDIO_DIR)

        # Now you have audio_path and duration, whether cached or fresh.
        self.stdout.write(f"Duration: {duration}s")

        # Transcribe & summarize exactly as before:
        self.stdout.write("Transcribing with Whisper…")
        segments = transcribe_audio(audio_path)

        self.stdout.write("Generating summary chunks…")
        summary = summarize_text(segments, duration, video_url=VIDEO_URL)

        # Save + upload…
        with open("summary.txt", "w", encoding="utf-8") as f:
            f.write(summary)
        self.stdout.write("Updating Google Sheet…")
        #update_google_sheet(summary, VIDEO_URL, WEB_APP_URL)
        self.stdout.write("Done.")