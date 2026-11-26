import os
import json
import random
import asyncio
import logging
import subprocess
import tempfile
import traceback
from datetime import datetime, time as dtime, date
from pathlib import Path

import requests
import imageio_ffmpeg
import edge_tts
from dotenv import load_dotenv
from groq import Groq
from PIL import Image
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)
from instagrapi import Client as IGClient
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

load_dotenv()
logging.basicConfig(level=logging.INFO)

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
IG_USERNAME = os.getenv("IG_USERNAME")
IG_PASSWORD = os.getenv("IG_PASSWORD")

IG_SESSION_FILE = "ig_session.json"
YT_TOKEN_FILE = "yt_token.json"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
MEMORY_FILE = "memory.json"
MUSIC_DIR = Path("music")
WATERMARK = os.getenv("WATERMARK", "Priyas Studio")
POST_TIMES = [dtime(9, 0), dtime(18, 0)]  # two reels a day (server time)
MAX_POSTS_PER_DAY = 2

state = {
    "yt": True,
    "ig": True,
    "auto": True,
    "theme": os.getenv("THEME", "nature"),
}

THEMES = {
    "nature": "nature landscape",
    "travel": "travel destination",
    "motivation": "sunrise mountain success",
    "temple": "temple india",
    "city": "city night",
}

FESTIVALS = {
    (1, 14): "Pongal",
    (1, 15): "Thiruvalluvar Day",
    (4, 14): "Tamil New Year",
    (8, 15): "Independence Day",
    (10, 31): "Diwali",
    (12, 25): "Christmas",
    (1, 1): "New Year",
}


# ---------------- MEMORY (survives restarts) ----------------
def load_memory():
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"used_photos": [], "posts": {}}


def save_memory():
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f)


memory = load_memory()


def posts_today():
    return memory["posts"].get(str(date.today()), 0)


def count_post():
    k = str(date.today())
    memory["posts"][k] = memory["posts"].get(k, 0) + 1
    save_memory()


# ---------------- AI ----------------
def ask_ai(prompt):
    client = Groq(api_key=GROQ_API_KEY)
    r = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.choices[0].message.content.strip()


def parse_json(text):
    s, e = text.index("{"), text.rindex("}") + 1
    return json.loads(text[s:e])


def make_content(theme_text, festival=None):
    extra = f" Today is {festival}, make it about that." if festival else ""
    prompt = (
        "You write viral Instagram Reels and YouTube Shorts for a Tamil audience. "
        f"Theme: {theme_text}.{extra} "
        "Pick the best mood (funny, serious, emotional, motivational, romantic). "
        "Write: a quote of max 15 words in Tamil script for on-screen text and voiceover; "
        "a hook line of max 8 words; a caption in Tanglish with emojis and a call to action; "
        "8 relevant hashtags. "
        'Return ONLY valid JSON with keys: "mood", "title" (max 60 chars), '
        '"quote", "caption" (including hashtags).'
    )
    text = ask_ai(prompt)
    try:
        d = parse_json(text)
        return (
            d.get("mood", ""),
            d.get("title", "New reel")[:90],
            d.get("quote", ""),
            d.get("caption", text),
        )
    except Exception:
        return ("", text.split("\n")[0][:90], "", text)


# ---------------- PEXELS PHOTOS ----------------
def fetch_photos(query, count, folder):
    headers = {"Authorization": PEXELS_API_KEY}
    page = random.randint(1, 10)
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers=headers,
        params={"query": query, "per_page": 40, "page": page, "orientation": "portrait"},
        timeout=20,
    )
    r.raise_for_status()
    photos = r.json().get("photos", [])
    random.shuffle(photos)
    paths = []
    for p in photos:
        if len(paths) >= count:
            break
        pid = str(p["id"])
        if pid in memory["used_photos"]:
            continue
        if p.get("width", 0) < 1000 or p.get("height", 0) < 1500:
            continue  # skip low quality
        url = p["src"]["large2x"]
        path = os.path.join(folder, f"p{len(paths)}.jpg")
        img = requests.get(url, timeout=30).content
        with open(path, "wb") as f:
            f.write(img)
        try:
            Image.open(path).verify()
        except Exception:
            continue
        memory["used_photos"].append(pid)
        paths.append(path)
    memory["used_photos"] = memory["used_photos"][-500:]
    save_memory()
    return paths


# ---------------- PEXELS VIDEOS ----------------
def fetch_video(query, folder):
    headers = {"Authorization": PEXELS_API_KEY}
    r = requests.get(
        "https://api.pexels.com/videos/search",
        headers=headers,
        params={"query": query, "per_page": 30, "page": random.randint(1, 5),
                "orientation": "portrait"},
        timeout=20,
    )
    r.raise_for_status()
    vids = r.json().get("videos", [])
    random.shuffle(vids)
    for v in vids:
        vid = "v" + str(v["id"])
        if vid in memory["used_photos"]:
            continue
        files = [f for f in v["video_files"] if f.get("file_type") == "video/mp4"
                 and f.get("height", 0) >= 960]
        if not files:
            continue
        files.sort(key=lambda f: f.get("height", 0))
        url = files[0]["link"]
        path = os.path.join(folder, "clip.mp4")
        data = requests.get(url, timeout=60).content
        with open(path, "wb") as f:
            f.write(data)
        memory["used_photos"].append(vid)
        save_memory()
        return path
    return None


# ---------------- MUSIC + VOICE ----------------
def pick_music():
    if MUSIC_DIR.exists():
        files = list(MUSIC_DIR.glob("*.mp3"))
        if files:
            return str(random.choice(files))
    return None


async def make_voice(text, path):
    if not text:
        return None
    try:
        comm = edge_tts.Communicate(text, "ta-IN-PallaviNeural")
        await comm.save(path)
        return path
    except Exception as e:
        logging.warning("voice failed: %s", e)
        return None
        # ---------------- REEL MAKING ----------------
def run_ffmpeg(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def font_file():
    for p in [
        "/usr/share/fonts/truetype/noto/NotoSansTamil-Bold.ttf",
        "/usr/share/fonts/truetype/lohit-tamil/Lohit-Tamil.ttf",
        "NotoSansTamil-Bold.ttf",
    ]:
        if os.path.exists(p):
            return p
    return None


def make_photo_reel(photo_paths, out, music=None, voice=None, quote=""):
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    n = len(photo_paths)
    sec = 5
    total = n * sec
    styles = ["zoomin", "zoomout", "pan"]

    cmd = [ff, "-y"]
    for p in photo_paths:
        cmd += ["-loop", "1", "-t", str(sec), "-i", p]
    next_in = n
    music_idx = voice_idx = None
    if music:
        cmd += ["-stream_loop", "-1", "-i", music]
        music_idx = next_in
        next_in += 1
    if voice:
        cmd += ["-i", voice]
        voice_idx = next_in
        next_in += 1

    frames = sec * 24
    fl = []
    for i in range(n):
        st = random.choice(styles)
        if st == "zoomin":
            z = f"zoompan=z='min(zoom+0.0015,1.3)':d={frames}:s=540x960:fps=24"
        elif st == "zoomout":
            z = f"zoompan=z='if(eq(on,1),1.3,max(zoom-0.0015,1.0))':d={frames}:s=540x960:fps=24"
        else:
            z = f"zoompan=z=1.2:x='(iw-iw/zoom)*on/{frames}':d={frames}:s=540x960:fps=24"
        fl.append(
            f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,{z},setsar=1,format=yuv420p[v{i}]"
        )
    concat_in = "".join(f"[v{i}]" for i in range(n))
    fl.append(f"{concat_in}concat=n={n}:v=1:a=0[cat]")

    last = "cat"
    font = font_file()
    if font:
        qfile = out + ".quote.txt"
        with open(qfile, "w", encoding="utf-8") as f:
            f.write(quote or "")
        fl.append(
            f"[{last}]drawtext=fontfile='{font}':textfile='{qfile}':fontcolor=white:"
            f"fontsize=34:x=(w-text_w)/2:y=h*0.72:box=1:boxcolor=black@0.45:boxborderw=14[q]"
        )
        last = "q"
        fl.append(
            f"[{last}]drawtext=fontfile='{font}':text='{WATERMARK}':fontcolor=white@0.7:"
            f"fontsize=22:x=w-text_w-20:y=h-text_h-30[wm]"
        )
        last = "wm"

    # audio
    audio_map = None
    if music_idx is not None and voice_idx is not None:
        fl.append(f"[{music_idx}:a]volume=0.25[ma];[{voice_idx}:a]volume=1.4[va];"
                  f"[ma][va]amix=inputs=2:duration=longest[aud]")
        audio_map = "[aud]"
    elif music_idx is not None:
        audio_map = f"{music_idx}:a"
    elif voice_idx is not None:
        audio_map = f"{voice_idx}:a"

    cmd += ["-filter_complex", ";".join(fl), "-map", f"[{last}]"]
    if audio_map:
        cmd += ["-map", audio_map, "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-threads", "1",
            "-t", str(total), "-movflags", "+faststart", out]
    run_ffmpeg(cmd)
    return out


def make_video_reel(clip, out, music=None, voice=None, quote=""):
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [ff, "-y", "-i", clip]
    idx = 1
    music_idx = voice_idx = None
    if music:
        cmd += ["-stream_loop", "-1", "-i", music]
        music_idx = idx
        idx += 1
    if voice:
        cmd += ["-i", voice]
        voice_idx = idx
        idx += 1
    fl = ["[0:v]scale=540:960:force_original_aspect_ratio=increase,crop=540:960,"
          "setsar=1,fps=24,format=yuv420p[v]"]
    last = "v"
    font = font_file()
    if font:
        qfile = out + ".quote.txt"
        with open(qfile, "w", encoding="utf-8") as f:
            f.write(quote or "")
        fl.append(
            f"[{last}]drawtext=fontfile='{font}':textfile='{qfile}':fontcolor=white:"
            f"fontsize=34:x=(w-text_w)/2:y=h*0.72:box=1:boxcolor=black@0.45:boxborderw=14[q]"
        )
        fl.append(
            f"[q]drawtext=fontfile='{font}':text='{WATERMARK}':fontcolor=white@0.7:"
            f"fontsize=22:x=w-text_w-20:y=h-text_h-30[wm]"
        )
        last = "wm"
    audio_map = None
    if music_idx is not None and voice_idx is not None:
        fl.append(f"[{music_idx}:a]volume=0.25[ma];[{voice_idx}:a]volume=1.4[va];"
                  f"[ma][va]amix=inputs=2:duration=longest[aud]")
        audio_map = "[aud]"
    elif music_idx is not None:
        audio_map = f"{music_idx}:a"
    elif voice_idx is not None:
        audio_map = f"{voice_idx}:a"
    cmd += ["-filter_complex", ";".join(fl), "-map", f"[{last}]"]
    if audio_map:
        cmd += ["-map", audio_map, "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-threads", "1",
            "-t", "20", "-movflags", "+faststart", out]
    run_ffmpeg(cmd)
    return out


# ---------------- YOUTUBE ----------------
def yt_service():
    if not os.path.exists(YT_TOKEN_FILE):
        raise RuntimeError("yt_token.json missing. Login once on your computer and upload it.")
    creds = Credentials.from_authorized_user_file(YT_TOKEN_FILE, YT_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(YT_TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        else:
            raise RuntimeError("YouTube token invalid. Login again on your computer.")
    return build("youtube", "v3", credentials=creds)


def upload_youtube(path, title, description):
    yt = yt_service()
    body = {
        "snippet": {"title": title[:100], "description": description, "categoryId": "22"},
        "status": {"privacyStatus": "public"},
    }
    media = MediaFileUpload(path, chunksize=-1, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return f"https://youtu.be/{resp['id']}"


# ---------------- INSTAGRAM ----------------
def ig_client():
    cl = IGClient()
    if os.path.exists(IG_SESSION_FILE):
        cl.load_settings(IG_SESSION_FILE)
        try:
            cl.get_timeline_feed()  # session still valid, no fresh login
            return cl
        except Exception:
            pass
    cl.login(IG_USERNAME, IG_PASSWORD)
    cl.dump_settings(IG_SESSION_FILE)
    return cl


def upload_instagram(path, caption):
    cl = ig_client()
    thumb = str(Path(path).with_suffix(".jpg"))
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    run_ffmpeg([ff, "-y", "-ss", "1", "-i", path, "-vframes", "1", thumb])
    media = cl.clip_upload(Path(path), caption, thumbnail=Path(thumb))
    return f"https://www.instagram.com/reel/{media.code}/"


# ---------------- ONE FULL RUN ----------------
async def notify(app, text):
    try:
        await app.bot.send_message(chat_id=OWNER_CHAT_ID, text=text)
    except Exception as e:
        logging.warning("notify failed: %s", e)


async def create_and_post(app):
    if posts_today() >= MAX_POSTS_PER_DAY:
        await notify(app, "Innaikku 2 post mudinchu, innum podala.")
        return

    tmp = tempfile.mkdtemp()
    theme_key = state["theme"]
    query = THEMES.get(theme_key, theme_key)
    festival = FESTIVALS.get((date.today().month, date.today().day))
    if festival:
        query = festival + " celebration"

    try:
        mood, title, quote, caption = await asyncio.to_thread(make_content, query, festival)
    except Exception as e:
        await notify(app, f"AI content fail: {e}")
        return

    music = pick_music()
    voice = await make_voice(quote, os.path.join(tmp, "voice.mp3"))
    out = os.path.join(tmp, "reel.mp4")

    # alternate: photo slideshow or stock video clip
    use_video = random.random() < 0.5
    made = False
    try:
        if use_video:
            clip = await asyncio.to_thread(fetch_video, query, tmp)
            if clip:
                await asyncio.to_thread(make_video_reel, clip, out, music, voice, quote)
                made = True
        if not made:
            photos = await asyncio.to_thread(fetch_photos, query, 3, tmp)
            if len(photos) < 3:
                raise RuntimeError("Pothumana nalla photos kidaikkala")
            await asyncio.to_thread(make_photo_reel, photos, out, music, voice, quote)
            made = True
    except Exception as e:
        await notify(app, f"Reel fail: {e}\n{traceback.format_exc()[-300:]}")
        return

    results = []
    if state["yt"]:
        try:
            url = await asyncio.to_thread(upload_youtube, out, title, caption)
            results.append(f"YouTube OK {url}")
        except Exception as e:
            results.append(f"YouTube FAIL {e}")
    if state["ig"]:
        try:
            url = await asyncio.to_thread(upload_instagram, out, caption)
            results.append(f"Instagram OK {url}")
        except Exception as e:
            results.append(f"Instagram FAIL {e}")

    if any("OK" in r for r in results):
        count_post()
    await notify(app, f"Mood: {mood}\nTitle: {title}\nQuote: {quote}\n\n" + "\n".join(results))

    for f in Path(tmp).glob("*"):
        try:
            f.unlink()
        except OSError:
            pass


# ---------------- TELEGRAM ----------------
def is_owner(update):
    return update.effective_chat and update.effective_chat.id == OWNER_CHAT_ID


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "Priyas Studio ready.\n"
        "/now - ippo oru reel podu\n"
        "/theme nature|travel|motivation|temple|city\n"
        "/auto on|off - thaana podurathu\n"
        "/yt on|off  /ig on|off\n"
        "/status\n"
        "Ethavathu text anuppina AI pathil solum."
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(
        f"YouTube: {'ON' if state['yt'] else 'OFF'}\n"
        f"Instagram: {'ON' if state['ig'] else 'OFF'}\n"
        f"Auto: {'ON' if state['auto'] else 'OFF'}\n"
        f"Theme: {state['theme']}\n"
        f"Innaikku posts: {posts_today()}/{MAX_POSTS_PER_DAY}\n"
        f"Model: {GROQ_MODEL}"
    )


def make_toggle(key):
    async def toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update):
            return
        arg = ctx.args[0].lower() if ctx.args else ""
        if arg not in ("on", "off"):
            await update.message.reply_text(f"/{key} on allathu /{key} off")
            return
        state[key] = arg == "on"
        await update.message.reply_text(f"{key.upper()} -> {'ON' if state[key] else 'OFF'}")
    return toggle


async def cmd_theme(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    arg = ctx.args[0].lower() if ctx.args else ""
    if arg not in THEMES:
        await update.message.reply_text("Themes: " + ", ".join(THEMES))
        return
    state["theme"] = arg
    await update.message.reply_text(f"Theme -> {arg}")


async def cmd_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text("Reel seiyaren, konjam neram aagum...")
    asyncio.create_task(create_and_post(ctx.application))


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    text = update.message.text
    try:
        reply = await asyncio.to_thread(
            ask_ai,
            "You are the assistant of a Tamil social media creator. "
            "Reply briefly in the same language style as the user.\n\nUser: " + text,
        )
    except Exception as e:
        reply = f"AI error: {e}"
    await update.message.reply_text(reply[:4000])


async def scheduled(ctx: ContextTypes.DEFAULT_TYPE):
    if state["auto"]:
        await create_and_post(ctx.application)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    await notify(ctx.application, f"Bot error: {ctx.error}")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("now", cmd_now))
    app.add_handler(CommandHandler("theme", cmd_theme))
    app.add_handler(CommandHandler("yt", make_toggle("yt")))
    app.add_handler(CommandHandler("ig", make_toggle("ig")))
    app.add_handler(CommandHandler("auto", make_toggle("auto")))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)
    for t in POST_TIMES:
        app.job_queue.run_daily(scheduled, time=t)
    app.run_polling()


if __name__ == "__main__":
    main()
