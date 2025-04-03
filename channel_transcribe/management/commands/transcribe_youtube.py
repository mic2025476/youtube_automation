import os
import tempfile
import time
import whisper
import yt_dlp
from django.core.management.base import BaseCommand
import requests
import datetime
import re
from webdriver_manager.chrome import ChromeDriverManager
from glob import glob
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

# Load environment variables from .env file
load_dotenv()

# Retrieve sensitive data from environment variables.
WEB_APP_URL = os.getenv("WEB_APP_URL")
# OPENAI_API_KEY will be used in the summarize_text function

def get_youtube_cookies(cookie_file='youtube_cookies.txt'):
    # Configure Selenium to run in headless mode (useful in CI environments)
    chrome_options = Options()
    chrome_options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/85.0.4183.121 Safari/537.36")
    
    # Initialize the Chrome driver
    chromedriver_path = os.getenv('CHROMEDRIVER_PATH')
    service = Service(chromedriver_path)
    
    # Initialize the WebDriver
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    # Navigate to YouTube
    driver.get("https://www.youtube.com")
    time.sleep(5)  # Wait for the page to load and cookies to be set
    
    # Extract cookies from the driver
    cookies = driver.get_cookies()
    driver.quit()
    
    # Write cookies to file in Netscape format (required by yt-dlp)
    with open(cookie_file, 'w') as f:
        f.write("# Netscape HTTP Cookie File\n")
        for cookie in cookies:
            domain = cookie.get('domain', '')
            flag = "TRUE" if domain.startswith('.') else "FALSE"
            path = cookie.get('path', '/')
            secure = "TRUE" if cookie.get('secure', False) else "FALSE"
            expiry = str(cookie.get('expiry', 0))
            name = cookie.get('name', '')
            value = cookie.get('value', '')
            # Write each cookie line (fields separated by tabs)
            f.write("\t".join([domain, flag, path, secure, expiry, name, value]) + "\n")
    
    print(f"Cookies saved to {cookie_file}")
    return cookie_file

def download_audio(video_url, output_dir):
    # Get cookies from a real browser session using Selenium
    cookie_file = get_youtube_cookies()
    
    # Define yt-dlp options, now including the cookies
    ydl_opts = {
        'cookiefile': cookie_file,
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(output_dir, '%(id)s.%(ext)s'),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'quiet': True,
        'geo_bypass': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36',
            'Referer': 'https://www.youtube.com'
        }
    }

    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # First extract info to get video duration
        info_dict = ydl.extract_info(video_url, download=False)
        duration = info_dict.get('duration', None)
        
        if duration:
            minutes, seconds = divmod(duration, 60)
            hours, minutes = divmod(minutes, 60)
            duration_str = f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}"
            print(f"Video duration: {duration_str}")
        
        # Now download the audio
        info_dict = ydl.extract_info(video_url, download=True)
        video_id = info_dict.get("id", None)
        filename = os.path.join(output_dir, f"{video_id}.mp3")
        
        if os.path.exists(filename):
            file_size = os.path.getsize(filename) / (1024 * 1024)  # MB
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

def summarize_text(segments, total_duration):
    import google.generativeai as genai
    
    # Configure Gemini (set your API key in environment variables)
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
    # Initialize the model
    model = genai.GenerativeModel('gemini-1.5-pro-latest')
    
    # Combine segments into a single text with timestamp markers
    text_with_timestamps = "\n".join(
        f"[{seg['start']:.1f}-{seg['end']:.1f}] {seg['text']}"
        for seg in segments
    )
    
    # Calculate chunk size (5 minute chunks)
    chunk_size = 300  # seconds
    num_chunks = max(1, int(total_duration // chunk_size))
    
    prompt = f"""
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
    {text_with_timestamps}
    """
    
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"Gemini API error: {str(e)}")
        return None

def update_google_sheet(summary_text, video_link, web_app_url):
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
        
        self.clear_previous_files()
        with tempfile.TemporaryDirectory() as temp_dir:
            print("Downloading audio...")
            audio_file, duration = download_audio(VIDEO_URL, temp_dir)
            
            print("Transcribing with timestamps...")
            segments = transcribe_audio(audio_file)
            
            summary_file = "summary.txt"

            if os.path.exists(summary_file) and os.path.getsize(summary_file) > 0:
                print("Summary file exists. Loading from summary.txt...")
                with open(summary_file, "r", encoding="utf-8") as f:
                    summary = f.read()
            else:
                print("Generating summary chunks...")
                print(f'duration {duration}')
                summary = summarize_text(segments, 3724)
                with open(summary_file, "w", encoding="utf-8") as f:
                    f.write(summary)
            
            print("Updating Google Sheet...")
            update_google_sheet(summary, VIDEO_URL, WEB_APP_URL)