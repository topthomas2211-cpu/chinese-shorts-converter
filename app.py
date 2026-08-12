import json,re,sqlite3,subprocess,tempfile,time,wave
from datetime import datetime,timezone
from pathlib import Path
import streamlit as st
from google import genai
from google.genai import types

LIMIT=900; MAX_MB=500; MAX_SEC=900
ANALYSIS_MODEL="gemini-3.1-flash-lite"
TTS_MODEL="gemini-3.1-flash-tts-preview"
DB=Path(__file__).parent/"usage.db"

st.set_page_config(page_title="Chinese → USA Shorts",page_icon="🎬")
def init():
    c=sqlite3.connect(DB); c.execute("CREATE TABLE IF NOT EXISTS usage(day TEXT PRIMARY KEY,seconds INTEGER NOT NULL)"); c.commit(); c.close()
def day(): return datetime.now(timezone.utc).strftime("%Y-%m-%d")
def used():
    init(); c=sqlite3.connect(DB); r=c.execute("SELECT seconds FROM usage WHERE day=?",(day(),)).fetchone(); c.close(); return r[0] if r else 0
def reserve(s):
    init(); c=sqlite3.connect(DB); c.execute("""INSERT INTO usage(day,seconds) VALUES(?,?) ON CONFLICT(day) DO UPDATE SET seconds=seconds+excluded.seconds""",(day(),int(s))); c.commit(); c.close()
def run(a):
    p=subprocess.run(a,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if p.returncode: raise RuntimeError(p.stderr[-3000:])
    return p.stdout.strip()
def dur(p): return float(run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(p)]))
def js(t):
    t=re.sub(r"^```(?:json)?\s*","",t.strip()); t=re.sub(r"\s*```$","",t)
    try:return json.loads(t)
    except:
        m=re.search(r"\{.*\}",t,re.S)
        if not m: raise
        return json.loads(m.group())
def analyze(client,p,d):
    st.write("🧠 Understanding video + Chinese audio...")
    f=client.files.upload(file=str(p))
    for _ in range(90):
        x=client.files.get(name=f.name); s=getattr(x.state,"name",str(x.state)).upper()
        if s=="ACTIVE": break
        if s=="FAILED": raise RuntimeError("Gemini could not process the video.")
        time.sleep(2)
    else: raise RuntimeError("Gemini video processing timed out.")
    prompt=f"""Understand the attached Chinese short video using BOTH visuals and Chinese audio. Do NOT literally translate it. Create native American English YouTube Shorts narration for a US audience.
Video duration: {d:.1f}s.
Rules: strong hook; curiosity; explain the actual story; satisfying payoff; no invented facts; natural American English; energetic and believable; fit the footage; captions in 3-7 word chunks with 1-3 highlighted words.
Return ONLY JSON: {{"hook":"string","script":"string","captions":[{{"text":"chunk","highlight":["word"]}}]}}"""
    r=client.models.generate_content(model=ANALYSIS_MODEL,contents=[f,prompt],config=types.GenerateContentConfig(temperature=.8,response_mime_type="application/json"))
    x=js(r.text)
    if not x.get("script"): raise RuntimeError("Gemini returned no script.")
    return x
def tts(client,script,out):
    st.write("🎙️ Generating American English voice...")
    p=f"""Read this YouTube Shorts narration in natural American English. Energetic, conversational, confident, fast but clear, strong emphasis. Do not add words.
NARRATION:
{script}"""
    r=client.models.generate_content(model=TTS_MODEL,contents=p,config=types.GenerateContentConfig(response_modalities=["AUDIO"],speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")))))
    try:b=r.candidates[0].content.parts[0].inline_data.data
    except Exception as e: raise RuntimeError("Gemini TTS returned no audio.") from e
    with wave.open(str(out),"wb") as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b)
def tm(s):
    cs=int(round(max(0,s)*100)); h,cs=divmod(cs,360000); m,cs=divmod(cs,6000); sec,cs=divmod(cs,100)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"
def ass(caps,audio,out):
    caps=[c for c in caps if str(c.get("text","")).strip()]; total=sum(max(1,len(str(c["text"]))) for c in caps) or 1; cur=0
    head="""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Shorts,Arial,72,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,1,0,0,0,100,100,0,0,1,5,2,2,80,80,250,1
[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    lines=[head]
    for c in caps:
        text=str(c["text"]); span=max(.35,audio*max(1,len(text))/total); start=cur; end=min(audio,cur+span); cur=end
        hi={str(x).lower() for x in c.get("highlight",[])}; words=[]
        for w in text.split():
            clean=re.sub(r"[^A-Za-z0-9'-]","",w).lower()
            words.append((r"{\c&H00FFFF&\fscx115\fscy115}"+w+r"{\c&HFFFFFF&\fscx100\fscy100}") if clean in hi else w)
        lines.append(f"Dialogue: 0,{tm(start)},{tm(end)},Shorts,,0,0,0,,{' '.join(words)}")
    out.write_text("\n".join(lines),encoding="utf-8")
def render(v,a,s,o):
    st.write("🎬 Rendering 1080×1920...")
    vf=f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,subtitles={s.as_posix()}"
    run(["ffmpeg","-y","-i",str(v),"-i",str(a),"-map","0:v:0","-map","1:a:0","-vf",vf,"-c:v","libx264","-preset","veryfast","-crf","23","-c:a","aac","-b:a","128k","-movflags","+faststart","-shortest",str(o)])
def process(upload,key):
    if not upload.name.lower().endswith(".mp4"): raise ValueError("Please upload MP4.")
    if upload.size>MAX_MB*1024*1024: raise ValueError("Maximum file size is 500 MB.")
    rem=max(0,LIMIT-used())
    with tempfile.TemporaryDirectory(prefix="shorts_") as td:
        td=Path(td); v=td/"input.mp4"; a=td/"voice.wav"; s=td/"captions.ass"; o=td/"usa_short.mp4"; v.write_bytes(upload.getbuffer())
        d=dur(v)
        if d<=0: raise ValueError("Could not read video duration.")
        if d>MAX_SEC: raise ValueError("Maximum video duration is 15 minutes.")
        if d>rem: raise ValueError(f"Daily limit exceeded. Remaining: {int(rem)//60}m {int(rem)%60:02d}s.")
        reserve(d); client=genai.Client(api_key=key); data=analyze(client,v,d)
        st.success("✓ Video understood and US script created.")
        with st.expander("Generated narration"): st.write(data["script"])
        tts(client,data["script"],a); ad=dur(a); ass(data.get("captions",[]),ad,s); render(v,a,s,o)
        return o.read_bytes()

init()
st.title("🎬 Chinese → USA Shorts Converter")
st.caption("V1 • 15 minutes/day • free-tier first")
with st.sidebar:
    st.header("Gemini")
    key=""
    try:key=st.secrets.get("GEMINI_API_KEY","")
    except:pass
    if not key:key=st.text_input("Gemini API key",type="password")
    rem=max(0,LIMIT-used()); st.metric("Remaining today",f"{rem//60}m {rem%60:02d}s")
up=st.file_uploader("Upload Chinese MP4",type=["mp4"])
if up and key:
    st.video(up)
    if st.button("🚀 Convert to USA Short",type="primary",use_container_width=True):
        try:
            with st.spinner("Processing..."): result=process(up,key)
            st.success("🎉 Done!")
            st.download_button("⬇️ Download Final MP4",result,"usa_short.mp4","video/mp4",use_container_width=True)
        except Exception as e: st.error(f"Processing failed: {e}")
elif up and not key: st.warning("Add your Gemini API key in the sidebar.")
