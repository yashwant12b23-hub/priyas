import os
import json
import random
import asyncio
import logging
import subprocess
import tempfile
import traceback
from datetime import time as dtime, date
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
from googleapiclient.errors import HttpError

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
POST_TIMES = [dtime(9, 0), dtime(18, 0)]  # two reels a day, server time
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
    (1, 1): "New Year",
    (1, 14): "Pongal",
    (4, 14): "Tamil New Year",
    (8, 15): "Independence Day",
    (10, 31): "Diwali",
    (12, 25): "Christmas",
}


# ---------------- MEMORY (survives restarts) ----------------
def load_memory():
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"used": [], "posts": {}}


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


# ---------------- AI (English only) ----------------
def ask_ai(prompt):
    client = Groq(api_key=GROQ_API_KEY)
    r = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.choices[0].message.content.strip()


def make_content(theme_text, festival=None):
    extra = f" Today is {festival}, make it about that." if festival else ""
    prompt = (
        "You write viral Instagram Reels and YouTube Shorts in English. "
        f"Theme: {theme_text}.{extra} "
        "Pick the best mood (funny, serious, emotional, motivational, romantic). "
        "Write: a short inspiring quote of max 15 words, in English only, used for the voiceover; "
        "a hook line of max 8 words; a caption in English with emojis and a call to action; "
        "8 relevant hashtags. Everything must be in English. "
        'Return ONLY valid JSON with keys: "mood", "title" (max 60 chars), '
        '"quote", "caption" (including hashtags).'
    )
    text = ask_ai(prompt)
    try:
        s, e = text.index("{"), text.rindex("}") + 1
        d = json.loads(text[s:e])
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
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers=headers,
        params={"query": query, "per_page": 40, "page": random.randint(1, 10),
                "orientation": "portrait"},
        timeout=20,
    )
    r.raise_for_status()
    photos = r.json().get("photos", [])
    random.shuffle(photos)
    paths = []
    for p in photos:
        if len(paths) >= count:
            break
        pid = "p" + str(p["id"])
        if pid in memory["used"]:
            continue
        if p.get("width", 0) < 1000 or p.get("height", 0) < 1500:
            continue  # skip low quality
        path = os.path.join(folder, f"p{len(paths)}.jpg")
        with open(path, "wb") as f:
            f.write(requests.get(p["src"]["large2x"], timeout=30).content)
        try:
            Image.open(path).verify()
        except Exception:
            continue
        memory["used"].append(pid)
        paths.append(path)
    memory["used"] = memory["used"][-500:]
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
        if vid in memory["used"]:
            continue
        files = [f for f in v["video_files"]
                 if f.get("file_type") == "video/mp4" and f.get("height", 0) >= 960]
        if not files:
            continue
        files.sort(key=lambda f: f.get("height", 0))
        path = os.path.join(folder, "clip.mp4")
        with open(path, "wb") as f:
            f.write(requests.get(files[0]["link"], timeout=60).content)
        memory["used"].append(vid)
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
        await edge_tts.Communicate(text, "en-US-AriaNeural").save(path)
        return path
    except Exception as e:
        logging.warning("voice failed: %s", e)
        return None


# ---------------- REEL MAKING ----------------
def run_ffmpeg(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def audio_filter(music_idx, voice_idx):
    """returns (extra filter text, map value) for the audio part"""
    if music_idx is not None and voice_idx is not None:
        return (f"[{music_idx}:a]volume=0.25[ma];[{voice_idx}:a]volume=1.4[va];"
                f"[ma][va]amix=inputs=2:duration=longest[aud]"), "[aud]"
    if music_idx is not None:
        return None, f"{music_idx}:a"
    if voice_idx is not None:
        return None, f"{voice_idx}:a"
    return None, None


def make_photo_reel(photo_paths, out, music=None, voice=None):
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    n = len(photo_paths)
    sec = 5
    frames = sec * 24

    cmd = [ff, "-y"]
    for p in photo_paths:
        cmd += ["-loop", "1", "-t", str(sec), "-i", p]
    idx = n
    music_idx = voice_idx = None
    if music:
        cmd += ["-stream_loop", "-1", "-i", music]
        music_idx = idx
        idx += 1
    if voice:
        cmd += ["-i", voice]
        voice_idx = idx

    fl = []
    for i in range(n):
        style = random.choice(["in", "out", "pan"])
        if style == "in":
            z = f"zoompan=z='min(zoom+0.0015,1.3)':d={frames}:s=540x960:fps=24"
        elif style == "out":
            z = f"zoompan=z='if(eq(on,1),1.3,max(zoom-0.0015,1.0))':d={frames}:s=540x960:fps=24"
        else:
            z = f"zoompan=z=1.2:x='(iw-iw/zoom)*on/{frames}':d={frames}:s=540x960:fps=24"
        fl.append(
            f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,{z},setsar=1,format=yuv420p[v{i}]"
        )
    fl.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[outv]")
    extra, amap = audio_filter(music_idx, voice_idx)
    if extra:
        fl.append(extra)

    cmd += ["-filter_complex", ";".join(fl), "-map", "[outv]"]
    if amap:
        cmd += ["-map", amap, "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-threads", "1",
            "-t", str(n * sec), "-movflags", "+faststart", out]
    run_ffmpeg(cmd)
    return out


def make_video_reel(clip, out, music=None, voice=None):
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

    fl = ["[0:v]scale=540:960:force_original_aspect_ratio=increase,crop=540:960,"
          "setsar=1,fps=24,format=yuv420p[outv]"]
    extra, amap = audio_filter(music_idx, voice_idx)
    if extra:
        fl.append(extra)

    cmd += ["-filter_complex", ";".join(fl), "-map", "[outv]"]
    if amap:
        cmd += ["-map", amap, "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-threads", "1",
            "-t", "20", "-movflags", "+faststart", out]
    run_ffmpeg(cmd)
    return out


# ---------------- YOUTUBE ----------------
def yt_service():
    if not os.path.exists(YT_TOKEN_FILE):
        raise RuntimeError("yt_token.json missing. Log in once on your computer and upload it.")
    creds = Credentials.from_authorized_user_file(YT_TOKEN_FILE, YT_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(YT_TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        else:
            raise RuntimeError("YouTube token invalid. Log in again on your computer.")
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
            cl.get_timeline_feed()  # session still valid, skip fresh login
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
        await app.bot.send_message(chat_id=OWNER_CHAT_ID, text=text[:4000])
    except Exception as e:
        logging.warning("notify failed: %s", e)


async def create_and_post(app):
    if posts_today() >= MAX_POSTS_PER_DAY:
        await notify(app, "Already posted 2 reels today. Skipping.")
        return

    tmp = tempfile.mkdtemp()
    query = THEMES.get(state["theme"], state["theme"])
    festival = FESTIVALS.get((date.today().month, date.today().day))
    if festival:
        query = festival + " celebration"

    try:
        mood, title, quote, caption = await asyncio.to_thread(make_content, query, festival)
    except Exception as e:
        await notify(app, f"AI content failed: {e}")
        return

    music = pick_music()
    voice = await make_voice(quote, os.path.join(tmp, "voice.mp3"))
    out = os.path.join(tmp, "reel.mp4")

    made = False
    try:
        if random.random() < 0.5:  # half the time use a stock video clip
            clip = await asyncio.to_thread(fetch_video, query, tmp)
            if clip:
                await asyncio.to_thread(make_video_reel, clip, out, music, voice)
                made = True
        if not made:
            photos = await asyncio.to_thread(fetch_photos, query, 3, tmp)
            if len(photos) < 3:
                raise RuntimeError("Not enough good photos found")
            await asyncio.to_thread(make_photo_reel, photos, out, music, voice)
    except Exception as e:
        await notify(app, f"Reel failed: {e}\n{traceback.format_exc()[-300:]}")
        return

    results = []
    if state["yt"]:
        try:
            url = await asyncio.to_thread(upload_youtube, out, title, caption)
            results.append(f"YouTube OK {url}")
        except HttpError as e:
            if "uploadLimitExceeded" in str(e):
                results.append("YouTube FAILED: daily upload limit reached, try tomorrow")
            else:
                results.append(f"YouTube FAILED {str(e)[:150]}")
        except Exception as e:
            results.append(f"YouTube FAILED {str(e)[:150]}")
    if state["ig"]:
        try:
            url = await asyncio.to_thread(upload_instagram, out, caption)
            results.append(f"Instagram OK {url}")
        except Exception as e:
            results.append(f"Instagram FAILED {e}")

    if any(" OK " in r for r in results):
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
        "Priyas Studio is ready.\n"
        "/now - make and post a reel right now\n"
        "/theme nature|travel|motivation|temple|city\n"
        "/auto on|off - automatic posting\n"
        "/yt on|off  /ig on|off\n"
        "/status\n"
        "Send any text and the AI will reply."
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(
        f"YouTube: {'ON' if state['yt'] else 'OFF'}\n"
        f"Instagram: {'ON' if state['ig'] else 'OFF'}\n"
        f"Auto: {'ON' if state['auto'] else 'OFF'}\n"
        f"Theme: {state['theme']}\n"
        f"Posts today: {posts_today()}/{MAX_POSTS_PER_DAY}\n"
        f"Model: {GROQ_MODEL}"
    )


def make_toggle(key):
    async def toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update):
            return
        arg = ctx.args[0].lower() if ctx.args else ""
        if arg not in ("on", "off"):
            await update.message.reply_text(f"Use /{key} on or /{key} off")
            return
        state[key] = arg == "on"
        await update.message.reply_text(f"{key.upper()} is now {'ON' if state[key] else 'OFF'}")
    return toggle


async def cmd_theme(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    arg = ctx.args[0].lower() if ctx.args else ""
    if arg not in THEMES:
        await update.message.reply_text("Themes: " + ", ".join(THEMES))
        return
    state["theme"] = arg
    await update.message.reply_text(f"Theme set to {arg}")


async def cmd_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text("Creating a reel, this takes a few minutes...")
    asyncio.create_task(create_and_post(ctx.application))


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    try:
        reply = await asyncio.to_thread(
            ask_ai,
            "You are the assistant of a social media creator. Reply briefly in English.\n\nUser: "
            + update.message.text,
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
