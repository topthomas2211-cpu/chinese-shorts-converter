
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
from google import genai
from google.genai import types


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "usage.db"
DAILY_LIMIT = 15 * 60
MAX_UPLOAD_MB = 500
MAX_VIDEO_SECONDS = 15 * 60
ANALYSIS_MODEL = "gemini-2.5-flash"
TTS_MODEL = "gemini-2.5-flash-preview-tts"
TTS_VOICE = "Kore"


st.set_page_config(
    page_title="Chinese → USA Shorts Converter",
    page_icon="🎬",
    layout="centered",
)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_usage (
            usage_date TEXT PRIMARY KEY,
            seconds INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def today_key():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_used_seconds():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT seconds FROM daily_usage WHERE usage_date = ?",
        (today_key(),),
    ).fetchone()
    conn.close()
    return int(row[0]) if row else 0


def reserve_seconds(seconds):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO daily_usage(usage_date, seconds)
        VALUES (?, ?)
        ON CONFLICT(usage_date)
        DO UPDATE SET seconds = seconds + excluded.seconds
        """,
        (today_key(), int(seconds)),
    )
    conn.commit()
    conn.close()


def run_cmd(cmd):
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-4000:])
    return result.stdout


def probe_video(path):
    out = run_cmd([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ])
    duration = float(out.strip())
    return duration


def has_audio(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            str(path)
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return bool(result.stdout.strip())


def clean_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_json(text):
    try:
        return json.loads(clean_json(text))
    except Exception:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            return json.loads(match.group(0))
        raise


def analyze_and_write_script(client, video_path, duration):
    st.write("🧠 Gemini is understanding the video...")

    uploaded = client.files.upload(file=str(video_path))

    # File API state can take a moment to become active.
    for _ in range(60):
        info = client.files.get(name=uploaded.name)
        state = getattr(info.state, "name", str(info.state))
        if state.upper() == "ACTIVE":
            break
        if state.upper() == "FAILED":
            raise RuntimeError("Gemini could not process the uploaded video.")
        time.sleep(2)
    else:
        raise RuntimeError("Gemini video processing timed out.")

    prompt = f"""
You are creating a YouTube Short for a mainstream American audience.

The attached video is a Chinese short-form video. Understand BOTH:
1) the visual action in the video
2) the Chinese spoken audio/dialogue

Do NOT produce a literal translation.

First understand what actually happens, then create a native American English
narration that matches the footage.

Video duration: {duration:.1f} seconds.

Requirements:
- Strong first-line hook.
- Create curiosity immediately.
- Explain what is happening naturally.
- Keep attention throughout.
- Give a satisfying payoff near the end.
- Do not invent facts that are not supported by the video.
- Do not mention that the source language is Chinese.
- Sound like a confident American YouTube Shorts narrator.
- Target roughly 125-155 spoken words per minute.
- Keep the narration close enough to the source video duration that the footage
  remains useful.
- Use short caption chunks, normally 3-7 words.
- Select 1-3 important words in each caption chunk for emphasis.

Return ONLY valid JSON in this exact shape:
{{
  "hook": "string",
  "script": "string",
  "captions": [
    {{
      "text": "short caption chunk",
      "highlight": ["important", "words"]
    }}
  ]
}}
"""

    response = client.models.generate_content(
        model=ANALYSIS_MODEL,
        contents=[uploaded, prompt],
        config=types.GenerateContentConfig(
            temperature=0.8,
            response_mime_type="application/json",
        ),
    )

    data = parse_json(response.text)

    if not data.get("script"):
        raise RuntimeError("Gemini did not return a usable script.")

    captions = data.get("captions") or []
    if not captions:
        words = data["script"].split()
        captions = []
        for i in range(0, len(words), 6):
            captions.append({
                "text": " ".join(words[i:i+6]),
                "highlight": [],
            })

    return data, captions


def pcm_to_wav(pcm_bytes, wav_path):
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm_bytes)


def make_voice(client, script, wav_path):
    st.write("🎙️ Generating American English narration...")

    prompt = f"""
Read the following YouTube Shorts narration in natural American English.

Voice direction:
- native American English
- energetic, conversational, confident
- fast enough for Shorts, but very clear
- strong emphasis on the hook and important words
- believable human narrator
- no intro, no outro, no extra words

NARRATION:
{script}
"""

    response = client.models.generate_content(
        model=TTS_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=TTS_VOICE
                    )
                )
            ),
        ),
    )

    try:
        pcm = response.candidates[0].content.parts[0].inline_data.data
    except Exception as exc:
        raise RuntimeError("Gemini TTS did not return audio.") from exc

    pcm_to_wav(pcm, wav_path)


def srt_time(seconds):
    ms = int(round(seconds * 1000))
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def ass_time(seconds):
    cs = int(round(seconds * 100))
    h = cs // 360000
    cs %= 360000
    m = cs // 6000
    cs %= 6000
    s = cs // 100
    cs %= 100
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def make_ass(captions, audio_duration, ass_path):
    # Timing is proportional to text length. This keeps V1 dependency-free.
    total_chars = sum(max(1, len(c.get("text", ""))) for c in captions)
    cursor = 0.0

    header = r"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Shorts,Arial,74,&H00FFFFFF,&H00FFFFFF,&H00111111,&H80000000,1,0,0,0,100,100,0,0,1,5,2,2,90,90,260,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = [header]

    for c in captions:
        text = str(c.get("text", "")).strip()
        if not text:
            continue

        weight = max(1, len(text))
        span = max(0.35, audio_duration * weight / total_chars)
        start = cursor
        end = min(audio_duration, cursor + span)
        cursor = end

        highlighted = set(
            str(x).strip().lower()
            for x in c.get("highlight", [])
            if str(x).strip()
        )

        rendered_words = []
        for word in text.split():
            clean = re.sub(r"[^A-Za-z0-9'-]", "", word).lower()
            if clean in highlighted:
                rendered_words.append(r"{\c&H00FFFF&\fscx115\fscy115}" + word + r"{\c&HFFFFFF&\fscx100\fscy100}")
            else:
                rendered_words.append(word)

        rendered = " ".join(rendered_words)
        rendered = rendered.replace("{", "\\{").replace("}", "\\}")
        # Restore ASS override tags after escaping.
        rendered = rendered.replace(r"\{", "{").replace(r"\}", "}")

        lines.append(
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Shorts,,0,0,0,,{rendered}"
        )

    ass_path.write_text("\n".join(lines), encoding="utf-8")


def render_video(input_path, wav_path, ass_path, output_path):
    st.write("🎬 Rendering 1080×1920 Short...")

    # Scale to cover 9:16, then crop the excess.
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        "setsar=1,"
        f"subtitles={ass_path.as_posix()}"
    )

    run_cmd([
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-i", str(wav_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-shortest",
        str(output_path),
    ])


def process_video(uploaded_file):
    if not uploaded_file.name.lower().endswith(".mp4"):
        raise ValueError("Please upload an MP4 file.")

    size_mb = uploaded_file.size / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        raise ValueError(f"Maximum upload size is {MAX_UPLOAD_MB} MB.")

    used = get_used_seconds()
    remaining = DAILY_LIMIT - used

    with tempfile.TemporaryDirectory(prefix="shorts_") as tmp:
        tmp = Path(tmp)
        input_path = tmp / "input.mp4"
        wav_path = tmp / "narration.wav"
        ass_path = tmp / "captions.ass"
        output_path = tmp / "final_usa_short.mp4"

        input_path.write_bytes(uploaded_file.getbuffer())

        duration = probe_video(input_path)

        if duration <= 0:
            raise ValueError("Could not read video duration.")

        if duration > MAX_VIDEO_SECONDS:
            raise ValueError("This V1 accepts videos up to 15 minutes.")

        if duration > remaining:
            raise ValueError(
                f"Daily limit exceeded. Remaining today: "
                f"{int(remaining)//60}m {int(remaining)%60:02d}s."
            )

        # Reserve quota before expensive processing.
        reserve_seconds(int(duration))

        client = genai.Client(api_key=st.session_state["gemini_api_key"])

        data, captions = analyze_and_write_script(
            client, input_path, duration
        )

        st.success("✓ Story understood and US script created.")
        with st.expander("View generated script"):
            st.write(data.get("script", ""))

        make_voice(client, data["script"], wav_path)

        voice_duration = probe_video(wav_path) if wav_path.exists() else 0
        if voice_duration <= 0:
            # ffprobe may not report WAV through format duration consistently.
            voice_duration = duration

        make_ass(captions, voice_duration, ass_path)
        render_video(input_path, wav_path, ass_path, output_path)

        return output_path.read_bytes(), data["script"]


init_db()

st.title("🎬 Chinese → USA Shorts Converter")
st.caption("Turn a Chinese MP4 into a narrated American-style YouTube Short.")

with st.sidebar:
    st.header("Settings")

    secret_key = ""
    try:
        secret_key = st.secrets.get("GEMINI_API_KEY", "")
    except Exception:
        secret_key = ""

    if secret_key:
        api_key = secret_key
        st.success("Gemini API key loaded from Streamlit Secrets.")
    else:
        api_key = st.text_input(
            "Gemini API key",
            type="password",
            help="Temporary fallback. For deployment, use Streamlit Secrets instead.",
        )

    st.session_state["gemini_api_key"] = api_key

    used = get_used_seconds()
    remaining = max(0, DAILY_LIMIT - used)

    st.metric(
        "Today's remaining processing",
        f"{remaining//60}m {remaining%60:02d}s",
    )

    st.info(
        "V1 limit: 15 minutes of source video per day. "
        "Files are processed in temporary storage and removed after processing."
    )

if not api_key:
    st.warning("Enter your Gemini API key in the left sidebar to begin.")

uploaded = st.file_uploader(
    "Upload Chinese MP4",
    type=["mp4"],
    help="Maximum 500 MB and maximum 15 minutes per video.",
)

if uploaded and api_key:
    st.video(uploaded)

    if st.button("🚀 Convert to USA Short", type="primary", use_container_width=True):
        try:
            with st.spinner("Working..."):
                result_bytes, script = process_video(uploaded)

            st.success("🎉 Your USA Short is ready!")

            st.download_button(
                "⬇️ Download Final MP4",
                data=result_bytes,
                file_name="usa_short.mp4",
                mime="video/mp4",
                use_container_width=True,
            )

        except Exception as exc:
            st.error(f"Processing failed: {exc}")
            st.caption(
                "If this is a Gemini quota/billing error, do not enable billing. "
                "Send the exact error to me and we will adjust the free setup."
            )
