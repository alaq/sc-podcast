# SoundCloud Podcast Feed Generator

This application generates podcast RSS feeds from SoundCloud tracks and playlists, with intelligent timestamp tracking for different feed types.

## Features

- Convert SoundCloud user tracks and likes to podcast RSS feeds
- Smart timestamp tracking for likes, reposts, and playlists: uses the time when a track first appeared in the feed rather than the original publication time
- Regular tracks feeds use original publication timestamps
- Caches first-seen timestamps using Vercel KV storage
- Support for individual track streaming
- Automatic fallback for missing metadata

## Installation

```bash
pip install -r requirements.txt
```

## Local Development

```bash
python local_server.py
```

The server will start on `http://localhost:8000`

## Usage

### Get a user's tracks feed (uses original timestamps):
`http://localhost:8000/username/tracks`

### Get a user's likes feed (uses smart timestamps):
`http://localhost:8000/username/likes`

### Get a user's reposts feed (uses smart timestamps):
`http://localhost:8000/username/reposts`

### Get a user's playlists/sets (uses smart timestamps):
`http://localhost:8000/username/sets/playlist-name`

### Get a user's tracks (default, uses original timestamps):
`http://localhost:8000/username`

## Vercel KV Setup (Optional but Recommended)

The application uses Vercel KV to store timestamps of when tracks first appear in specific feeds. This ensures that the podcast feed shows the correct "publication date" based on when a track was added to that specific feed (e.g., when someone liked it), not when the track was originally published.

### Environment Variables

Set these environment variables in your Vercel project or `.env` file:

```
KV_REST_API_URL=your_vercel_kv_rest_api_url
KV_REST_API_TOKEN=your_vercel_kv_rest_api_token
```

### How to get Vercel KV credentials:

1. Go to your Vercel dashboard
2. Navigate to your project
3. Go to Settings → Storage
4. Create or connect a KV database
5. Copy the `KV_REST_API_URL` and `KV_REST_API_TOKEN` from the connection details

### Without Vercel KV

If you don't set up Vercel KV, the application will fall back to using the current time ("now") for all tracks. The application will still work, but won't have the smart timestamp tracking feature.

## How Smart Timestamps Work

**Smart timestamps only apply to:**
- Likes feeds (`/username/likes`)
- Reposts feeds (`/username/reposts`) 
- Playlists/sets (`/username/sets/playlist-name`)

**For these feeds:**
- **First time seeing a track**: Records the current timestamp and uses it for the RSS feed
- **Subsequent requests**: Uses the stored timestamp from when the track was first seen in that specific feed
- **Per-feed tracking**: A track liked by `user-a` and also in `user-b`'s likes will have different "first seen" times for each feed
- **Fallback**: If KV storage is unavailable, uses the current time ("now")

**For regular tracks feeds (`/username/tracks` or `/username`):**
- Always uses the original track publication timestamp
- No KV storage needed or used

This is particularly useful for "likes" and "reposts" feeds where you want to know when someone interacted with a track, not when the track was originally published.

## Deployment

Deploy to Vercel with the included `vercel.json` configuration file.
