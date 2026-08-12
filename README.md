# Chinese Shorts → USA Shorts Converter V1

A simple Streamlit web app that:

1. Accepts a Chinese MP4.
2. Enforces a 15-minute/day source-video quota.
3. Uses Gemini to understand video + Chinese audio.
4. Creates native American English Shorts narration.
5. Generates American English TTS.
6. Creates animated/emphasized ASS captions.
7. Converts the footage to 1080x1920 9:16.
8. Downloads the final MP4.
9. Uses temporary processing files.

## Free-first design

The app is designed for Streamlit Community Cloud + Gemini free access where available.

Important: free-tier model availability and quotas can change. Do not enable billing if your goal is $0.

## Deploy

1. Create a GitHub repository.
2. Upload all files.
3. Go to Streamlit Community Cloud.
4. Create an app from the GitHub repository.
5. Select `app.py`.
6. Deploy.
7. Paste your Gemini API key in the app sidebar.


## Streamlit Secrets

In Streamlit Community Cloud, open your app's Settings → Secrets and paste:

```toml
GEMINI_API_KEY = "YOUR_KEY_HERE"
```

Do not put the API key in GitHub files.
