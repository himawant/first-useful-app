import os

import json
import sys
from googleapiclient.discovery import build
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta
from googleapiclient.errors import HttpError
from youtube_transcript_api import YouTubeTranscriptApi
import google.generativeai as genai

# --- Configuration ---
YOUTUBE_API_KEY = "API"
GEMINI_API_KEY = "API" # IMPORTANT: Replace with your actual Gemini API key
DATA_FILE = "mindfultube_data.json"
DAILY_VIDEO_LIMIT = 2 # Number of videos to "feed" per day

# Define the order of usefulness for prioritization (lower index = higher priority)
USEFULNESS_ORDER = {
    "highly_useful": 0,
    "useful": 1,
    "review_needed": 2,
    "fluff": 3,
    "outdated": 4,
    "unknown": 5 # Default for videos without analysis
}

# --- Data Management ---
def load_data():
    if not os.path.exists(DATA_FILE):
        return {"playlists": {}, "last_fed_date": None}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

def clean_description(description):
    # Replace common problematic characters for JSON
    description = description.replace("\n", " ") # Replace newlines with spaces
    description = description.replace("\r", " ") # Replace carriage returns with spaces
    # You might need to add more replacements for other control characters
    return description

def normalize_youtube_url(url):
    parsed_url = urlparse(url)
    if parsed_url.netloc in ['www.youtube.com', 'youtube.com']:
        if parsed_url.path == '/watch':
            video_id = parse_qs(parsed_url.query).get('v', [None])[0]
        elif parsed_url.path.startswith('/embed/'):
            video_id = parsed_url.path.split('/')[2]
        else:
            video_id = None
    elif parsed_url.netloc in ['youtu.be']:
        video_id = parsed_url.path.split('/')[1]
    else:
        video_id = None

    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return url # Return original if not a recognized YouTube video URL

# --- YouTube API Interaction ---

def get_youtube_service():
    return build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

def get_playlist_id(url):
    parsed_url = urlparse(url)
    if "youtube.com" in parsed_url.netloc and "list" in parse_qs(parsed_url.query):
        return parse_qs(parsed_url.query)["list"][0]
    elif "youtu.be" in parsed_url.netloc:
        return None
    return None

def get_playlist_items(youtube, playlist_id):
    video_ids = []
    next_page_token = None

    while True:
        request = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=next_page_token
        )
        response = request.execute()

        for item in response["items"]:
            # Check if contentDetails and videoId exist, indicating it's a video
            if "contentDetails" in item and "videoId" in item["contentDetails"]:
                video_ids.append(item["contentDetails"]["videoId"])
            else:
                print(f"WARNING: Skipping non-video item or item with unexpected structure in playlist: {item.get('id', 'Unknown ID')}", file=sys.stderr)

        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break

    videos_data = []
    for i in range(0, len(video_ids), 50):
        batch_ids = video_ids[i:i+50]
        videos_request = youtube.videos().list(
            part="snippet",
            id=",".join(batch_ids)
        )
        videos_response = videos_request.execute()

        for item in videos_response["items"]:
            video_id = item["id"]
            published_at = item["snippet"]["publishedAt"]
            title = item["snippet"]["title"]
            description = clean_description(item["snippet"]["description"])
            videos_data.append({
                "url": normalize_youtube_url(f"https://www.youtube.com/watch?v={video_id}"), # Normalize URL here
                "publishedAt": published_at,
                "title": title,
                "description": description,
                "fed": False # Initial status
            })
    return videos_data

def get_video_transcript(video_id):
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        # Try to get an English transcript first
        transcript = transcript_list.find_transcript(['en', 'en-US'])
        fetched_data = transcript.fetch()
        return " ".join([t.text for t in fetched_data])
    except Exception as e:
        print(f"WARNING: Could not fetch transcript for video {video_id}: {e}", file=sys.stderr)
        return None

# --- Application Logic ---
def add_playlist(playlist_url):
    if YOUTUBE_API_KEY == "YOUR_YOUTUBE_API_KEY":
        print("ERROR: Please replace 'YOUR_YOUTUBE_API_KEY' in app.py with your actual API key.", file=sys.stderr)
        return

    playlist_id = get_playlist_id(playlist_url)
    if not playlist_id:
        print(f"ERROR: Could not extract playlist ID from URL: {playlist_url}", file=sys.stderr)
        print("Please ensure it's a valid YouTube playlist URL (e.g., https://www.youtube.com/playlist?list=...).", file=sys.stderr)
        return

    data = load_data()
    if playlist_id in data["playlists"]:
        print(f"Playlist '{playlist_url}' is already being tracked.", file=sys.stderr)
        return

    print(f"Fetching videos from playlist: {playlist_url}...")
    try:
        youtube = get_youtube_service()
        video_urls = get_playlist_items(youtube, playlist_id)
    except HttpError as e:
        print(f"ERROR: YouTube API Error: {e.resp.status} - {e.content.decode()}", file=sys.stderr)
        return
    except Exception as e:
        print(f"ERROR: An unexpected error occurred: {e}", file=sys.stderr)
        return

    if not video_urls:
        print(f"No videos found in playlist: {playlist_url}. Please check the URL.", file=sys.stderr)
        return

    data["playlists"][playlist_id] = {
        "url": playlist_url,
        "videos": video_urls,
        "added_date": datetime.now().isoformat()
    }
    save_data(data)
    print(f"Successfully added {len(video_urls)} videos from playlist '{playlist_url}'.")

def parse_youtube_datetime(dt_str):
    return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))

def sync_playlist(playlist_url):
    if YOUTUBE_API_KEY == "YOUR_YOUTUBE_API_KEY":
        print("ERROR: Please replace 'YOUR_YOUTUBE_API_KEY' in app.py with your actual API key.", file=sys.stderr)
        return

    playlist_id = get_playlist_id(playlist_url)
    if not playlist_id:
        print(f"ERROR: Could not extract playlist ID from URL: {playlist_url}", file=sys.stderr)
        print("Please ensure it's a valid YouTube playlist URL (e.g., https://www.youtube.com/playlist?list=...).")
        return

    data = load_data()
    if playlist_id not in data["playlists"]:
        print(f"Playlist '{playlist_url}' is not currently tracked. Please add it first.", file=sys.stderr)
        return

    print(f"Syncing videos from playlist: {playlist_url}...")
    try:
        youtube = get_youtube_service()
        current_youtube_videos = get_playlist_items(youtube, playlist_id)
    except HttpError as e:
        print(f"ERROR: YouTube API Error during sync: {e.resp.status} - {e.content.decode()}", file=sys.stderr)
        return
    except Exception as e:
        print(f"ERROR: An unexpected error occurred during sync: {e}", file=sys.stderr)
        return

    local_playlist = data["playlists"][playlist_id]
    local_video_urls = {v["url"]: v for v in local_playlist["videos"]}

    new_videos_count = 0
    updated_videos_list = []

    for yt_video in current_youtube_videos:
        video_url = yt_video["url"]
        if video_url in local_video_urls:
            existing_video = local_video_urls[video_url]
            existing_video["publishedAt"] = yt_video["publishedAt"]
            existing_video["title"] = yt_video["title"]
            existing_video["description"] = yt_video["description"]
            updated_videos_list.append(existing_video)
        else:
            updated_videos_list.append(yt_video)
            new_videos_count += 1

    local_playlist["videos"] = updated_videos_list
    save_data(data)
    print(f"Sync complete for '{playlist_url}'. Added {new_videos_count} new videos.")

def sync_playlist(playlist_url):
    if YOUTUBE_API_KEY == "YOUR_YOUTUBE_API_KEY":
        print("ERROR: Please replace 'YOUR_YOUTUBE_API_KEY' in app.py with your actual API key.", file=sys.stderr)
        return

    playlist_id = get_playlist_id(playlist_url)
    if not playlist_id:
        print(f"ERROR: Could not extract playlist ID from URL: {playlist_url}", file=sys.stderr)
        print("Please ensure it's a valid YouTube playlist URL (e.g., https://www.youtube.com/playlist?list=...).", file=sys.stderr)
        return

    data = load_data()
    if playlist_id not in data["playlists"]:
        print(f"Playlist '{playlist_url}' is not currently tracked. Please add it first.", file=sys.stderr)
        return

    print(f"Syncing videos from playlist: {playlist_url}...")
    try:
        youtube = get_youtube_service()
        current_youtube_videos = get_playlist_items(youtube, playlist_id)
    except HttpError as e:
        print(f"ERROR: YouTube API Error during sync: {e.resp.status} - {e.content.decode()}", file=sys.stderr)
        return
    except Exception as e:
        print(f"ERROR: An unexpected error occurred during sync: {e}", file=sys.stderr)
        return

    local_playlist = data["playlists"][playlist_id]
    local_video_urls = {v["url"]: v for v in local_playlist["videos"]}

    new_videos_count = 0
    updated_videos_list = []

    for yt_video in current_youtube_videos:
        video_url = yt_video["url"]
        if video_url in local_video_urls:
            existing_video = local_video_urls[video_url]
            existing_video["publishedAt"] = yt_video["publishedAt"]
            existing_video["title"] = yt_video["title"]
            existing_video["description"] = yt_video["description"]
            updated_videos_list.append(existing_video)
        else:
            updated_videos_list.append(yt_video)
            new_videos_count += 1

    local_playlist["videos"] = updated_videos_list
    save_data(data)
    print(f"Sync complete for '{playlist_url}'. Added {new_videos_count} new videos.")

def analyze_video(video_url, summary, usefulness_rating, actionable_points_str):
    video_url = normalize_youtube_url(video_url) # Normalize input URL
    data = load_data()
    found_video = False
    for playlist_id, playlist_info in data["playlists"].items():
        for video in playlist_info["videos"]:
            if video["url"] == video_url:
                video["summary"] = summary
                video["usefulness_rating"] = usefulness_rating
                video["actionable_points"] = [ap.strip() for ap in actionable_points_str.split(';') if ap.strip()]
                found_video = True
                break
        if found_video:
            break

    if found_video:
        save_data(data)
        print(f"Analysis for {video_url} saved successfully.")
    else:
        print(f"Error: Video {video_url} not found in any tracked playlist.", file=sys.stderr)

def auto_analyze_video_with_llm(video_url):
    video_url = normalize_youtube_url(video_url) # Normalize input URL
    if GEMINI_API_KEY == "YOUR_GEMINI_API_KEY":
        print("ERROR: Please replace 'YOUR_GEMINI_API_KEY' in app.py with your actual Gemini API key.", file=sys.stderr)
        return

    data = load_data()
    video_data = None
    for playlist_id, playlist_info in data["playlists"].items():
        for video in playlist_info["videos"]:
            if video["url"] == video_url:
                video_data = video
                break
        if video_data:
            break

    if not video_data:
        print(f"Error: Video {video_url} not found in any tracked playlist. Please add it first.", file=sys.stderr)
        return

    print(f"Auto-analyzing video: {video_data.get('title', video_url)}...")

    # Extract video_id from the normalized URL
    parsed_normalized_url = urlparse(video_url)
    video_id = parse_qs(parsed_normalized_url.query).get('v', [None])[0]

    if not video_id:
        print(f"ERROR: Could not extract video ID from normalized URL: {video_url}", file=sys.stderr)
        return

    transcript = get_video_transcript(video_id)

    if not transcript:
        print(f"WARNING: No transcript available for {video_url}. Analyzing based on title/description only.", file=sys.stderr)
        content_for_llm = f"Title: {video_data.get('title', '')}\nDescription: {video_data.get('description', '')}"
    else:
        content_for_llm = f"Title: {video_data.get('title', '')}\nDescription: {video_data.get('description', '')}\nTranscript: {transcript}"

    prompt = f"""Analyze the following YouTube video content and provide a summary, a usefulness rating, and actionable points. The usefulness rating should be one of: 'highly_useful', 'useful', 'fluff', 'outdated', 'review_needed'. Provide the output in JSON format. If no actionable points, return an empty array.

Content:
{content_for_llm}

JSON Output Example:
{{
  "summary": "A concise summary of the video.",
  "usefulness_rating": "useful",
  "actionable_points": [
    "Point 1",
    "Point 2"
  ]
}}
"""

    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('models/gemini-2.5-flash')
        response = model.generate_content(prompt)
        llm_output = response.text
        # Strip markdown code block fences if present
        if llm_output.startswith('```json') and llm_output.endswith('```'):
            llm_output = llm_output[len('```json'):-len('```')].strip()

        # Attempt to parse JSON output from LLM
        llm_analysis = json.loads(llm_output)

        # Update video data with LLM analysis
        video_data["summary"] = llm_analysis.get("summary", "")
        video_data["usefulness_rating"] = llm_analysis.get("usefulness_rating", "unknown")
        video_data["actionable_points"] = llm_analysis.get("actionable_points", [])

        save_data(data)
        print(f"Successfully auto-analyzed {video_url}.")

    except json.JSONDecodeError as e:
        print(f"ERROR: Failed to parse LLM JSON response for {video_url}: {e}\nLLM Output: {llm_output}", file=sys.stderr)
    except Exception as e:
        print(f"ERROR: Failed to auto-analyze {video_url}: {e}", file=sys.stderr)

def mark_video_watched(video_url):
    video_url = normalize_youtube_url(video_url) # Normalize input URL
    data = load_data()
    found_video = False
    for playlist_id, playlist_info in data["playlists"].items():
        for video in playlist_info["videos"]:
            if video["url"] == video_url:
                video["fed"] = True
                found_video = True
                break
        if found_video:
            break

    if found_video:
        save_data(data)
        print(f"Video {video_url} marked as watched.")
    else:
        print(f"Error: Video {video_url} not found in any tracked playlist.", file=sys.stderr)

def skip_video(video_url):
    video_url = normalize_youtube_url(video_url) # Normalize input URL
    data = load_data()
    found_video = False
    for playlist_id, playlist_info in data["playlists"].items():
        for video in playlist_info["videos"]:
            if video["url"] == video_url:
                video["fed"] = True
                found_video = True
                break
        if found_video:
            break

    if found_video:
        save_data(data)
        print(f"Video {video_url} skipped for today.")
    else:
        print(f"Error: Video {video_url} not found in any tracked playlist.", file=sys.stderr)

def list_playlists():
    data = load_data()
    if not data["playlists"]:
        print("No playlists are currently being tracked.")
        return

    print("--- Tracked Playlists ---")
    for playlist_id, playlist_info in data["playlists"].items():
        total_videos = len(playlist_info["videos"])
        fed_videos = sum(1 for v in playlist_info["videos"] if v["fed"])
        unfed_videos = total_videos - fed_videos
        print(f"URL: {playlist_info['url']}")
        print(f"  Videos: {total_videos} (Fed: {fed_videos}, Unfed: {unfed_videos})")
        print(f"  Added: {datetime.fromisoformat(playlist_info['added_date']).strftime('%Y-%m-%d')}")
        print("-" * 25)

def get_next_video():
    data = load_data()
    today = datetime.now().date()

    if data["last_fed_date"]:
        last_fed = datetime.fromisoformat(data["last_fed_date"]).date()
        if last_fed == today:
            print(f"You've already been fed {DAILY_VIDEO_LIMIT} video(s) today. Come back tomorrow!")
            return

    all_unfed_videos = []
    for playlist_id, playlist_info in data["playlists"].items():
        for video in playlist_info["videos"]:
            if not video["fed"]:
                all_unfed_videos.append(video)

    if not all_unfed_videos:
        print("No new unfed videos available across all tracked playlists. Add more playlists or sync existing ones!")
        return

    # Sort unfed videos by usefulness (highest priority first) then by publishedAt (newest first)
    all_unfed_videos.sort(key=lambda x: (
        USEFULNESS_ORDER.get(x.get("usefulness_rating", "unknown"), len(USEFULNESS_ORDER)), # Ascending for usefulness
        -parse_youtube_datetime(x["publishedAt"]).timestamp() # Descending for publishedAt (by negating timestamp)
    ))

    fed_count_today = 0
    for video_to_feed in all_unfed_videos:
        if fed_count_today >= DAILY_VIDEO_LIMIT:
            break

        found_and_fed = False
        for playlist_id, playlist_info in data["playlists"].items():
            for video in playlist_info["videos"]:
                if video["url"] == video_to_feed["url"] and not video["fed"]:
                    print(f"Here's your next mindful video from {playlist_info['url']}:")
                    print(f"Title: {video.get('title', 'N/A')}")
                    print(f"URL: {video['url']}")
                    if "summary" in video:
                        print(f"Summary: {video['summary']}")
                    if "usefulness_rating" in video:
                        print(f"Usefulness: {video['usefulness_rating']}")
                    if "actionable_points" in video and video["actionable_points"]:
                        print("Actionable Points:")
                        for ap in video["actionable_points"]:
                            print(f"  - {ap}")
                    print("\n") # Add a newline for better readability

                    video["fed"] = True
                    fed_count_today += 1
                    found_and_fed = True
                    break
            if found_and_fed:
                break

    if fed_count_today == 0:
        print("No new unfed videos available across all tracked playlists. Add more playlists or sync existing ones!")

    data["last_fed_date"] = today.isoformat()
    save_data(data)

# --- Main CLI Entry Point ---
def main():
    if len(sys.argv) < 2:
        print("Usage: python app.py <command> [args]")
        print("Commands:")
        print("  add <playlist_url> - Add a YouTube playlist to track")
        print("  list               - List all tracked playlists and their status")
        print("  next               - Get your next mindful video (limited per day)")
        print("  sync <playlist_url> - Sync a tracked playlist to get new videos")
        print("  analyze <video_url> <summary> <rating> <actionable_points> - Manually add analysis for a video")
        print("  watch <video_url>  - Mark a video as watched without feeding it")
        print("  skip <video_url>   - Mark a video as skipped for today")
        print("  auto_analyze <video_url> - Automatically analyze a video using LLM")
        return

    command = sys.argv[1]

    if command == "add":
        if len(sys.argv) < 3:
            print("Usage: python app.py add <playlist_url>")
            return
        playlist_url = sys.argv[2]
        add_playlist(playlist_url)
    elif command == "list":
        list_playlists()
    elif command == "next":
        get_next_video()
    elif command == "sync":
        if len(sys.argv) < 3:
            print("Usage: python app.py sync <playlist_url>")
            return
        playlist_url = sys.argv[2]
        sync_playlist(playlist_url)
    elif command == "analyze":
        if len(sys.argv) < 6:
            print("Usage: python app.py analyze <video_url> <summary> <rating> <actionable_points>")
            print("  Rating options: highly_useful, useful, fluff, outdated")
            print("  Actionable points should be separated by semicolons (;)")
            return
        video_url = sys.argv[2]
        summary = sys.argv[3]
        rating = sys.argv[4]
        actionable_points_str = sys.argv[5]
        analyze_video(video_url, summary, rating, actionable_points_str)
    elif command == "watch":
        if len(sys.argv) < 3:
            print("Usage: python app.py watch <video_url>")
            return
        video_url = sys.argv[2]
        mark_video_watched(video_url)
    elif command == "skip":
        if len(sys.argv) < 3:
            print("Usage: python app.py skip <video_url>")
            return
        video_url = sys.argv[2]
        skip_video(video_url)
    elif command == "auto_analyze":
        if len(sys.argv) < 3:
            print("Usage: python app.py auto_analyze <video_url>")
            return
        video_url = sys.argv[2]
        auto_analyze_video_with_llm(video_url)
    else:
        print(f"Unknown command: {command}")

if __name__ == "__main__":
    main()
