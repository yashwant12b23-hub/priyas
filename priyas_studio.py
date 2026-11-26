import os
import json
import asyncio
import logging
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import imageio_ffmpeg
from dotenv import load_dotenv
from groq import Groq
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

from instagrapi import Client as IGClient

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

load_dotenv()
logging.basicConfig(level=logging.INFO)


# ---------- Web ping server (Render) ----------
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def start_web():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


def keep_alive():
    while True:
        time.sleep(600)
        try:
            url = os.environ.get("RENDER_EXTERNAL_URL")
            if url:
                urllib.request.urlopen(url, timeout=10)
            if BOT_TOKEN and OWNER_CHAT_ID:
                text = urllib.parse.quote("நான் ஓடுறேன் ✅")
                urllib.request.urlopen(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                    f"?chat_id={OWNER_CHAT_ID}&text={text}",
                    timeout=10,
                )
        except Exception as e:
            logging.warning("keep_alive error: %s", e)


threading.Thread(target=start_web, daemon=True).start()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0"))
IG_USERNAME = os.getenv("IG_USERNAME")
IG_PASSWORD = os.getenv("IG_PASSWORD")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Groq model. Render env-ல GROQ_MODEL வச்சா அதை எடுக்கும்.
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

threading.Thread(target=keep_alive, daemon=True).start()

IG_SESSION_FILE = "ig_session.json"
YT_CLIENT_SECRET = "client_secret.json"
YT_TOKEN_FILE = "yt_token.json"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# repo-ல music.mp3 இருந்தா reel-ல சேரும்
MUSIC_FILE = "music.mp3"

state = {"yt": True, "ig": True}

# பல போட்டோ album-ஆ வந்தா சேர்த்து வைக்க
albums = {}


# ---------- Reel (single photo or slideshow) ----------
def make_reel(photo_paths, output_path, music_path=None, sec_per_photo=3):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    if isinstance(photo_paths, str):
        photo_paths = [photo_paths]
    if len(photo_paths) == 1:
        sec_per_photo = 12
    n = len(photo_paths)
    total = n * sec_per_photo

    cmd = [ffmpeg, "-y"]
    for p in photo_paths:
        cmd += ["-loop", "1", "-t", str(sec_per_photo), "-i", p]
    if music_path:
        cmd += ["-stream_loop", "-1", "-i", music_path]

    filters_ = []
    for i in range(n):
        filters_.append(
            f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{i}]"
        )
    concat_in = "".join(f"[v{i}]" for i in range(n))
    filters_.append(f"{concat_in}concat=n={n}:v=1:a=0[outv]")

    cmd += ["-filter_complex", ";".join(filters_), "-map", "[outv]"]
    if music_path:
        cmd += ["-map", f"{n}:a", "-c:a", "aac", "-shortest"]
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-threads", "1", "-t", str(total), "-movflags", "+faststart", output_path]
    subprocess.run(cmd, check=True)
    return output_path


# ---------- AI caption (mood தானா தேர்ந்தெடுக்கும்) ----------
CAPTION_PROMPT = """You are a social media expert who writes viral Instagram Reels and YouTube Shorts captions for a Tamil audience.

Steps:
1. Decide the best mood for this content: funny, serious, emotional, motivational, or romantic. Use the user's context if given, otherwise pick the mood that gets the most engagement.
2. Write a strong hook as the first line (max 8 words) that makes people stop scrolling.
3. Write 1 or 2 short lines in Tamil-English mix (Tanglish) in that mood, with fitting emojis.
4. End with a short call to action (like, share, follow, comment).
5. Add 8 relevant hashtags: mix of trending and niche.

Return ONLY valid JSON with keys: "mood", "title" (max 60 chars), "caption" (everything including hashtags)."""


def make_caption(user_text=None):
    client = Groq(api_key=GROQ_API_KEY)
    prompt = CAPTION_PROMPT
    if user_text:
        prompt += f"\n\nUser context for the photo: {user_text}"
    r = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    text = r.choices[0].message.content.strip()
    try:
        s, e = text.index("{"), text.rindex("}") + 1
        data = json.loads(text[s:e])
        return (
            data.get("mood", ""),
            data.get("title", "New reel")[:100],
            data.get("caption", text),
        )
    except Exception:
        return ("", text.split("\n")[0][:100], text)


# ---------- YouTube ----------
def yt_service():
    creds = None
    if os.path.exists(YT_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(YT_TOKEN_FILE, YT_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(YT_CLIENT_SECRET, YT_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(YT_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
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


# ---------- Instagram ----------
def ig_client():
    cl = IGClient()
    if os.path.exists(IG_SESSION_FILE):
        cl.load_settings(IG_SESSION_FILE)
    cl.login(IG_USERNAME, IG_PASSWORD)
    cl.dump_settings(IG_SESSION_FILE)
    return cl


def upload_instagram(path, caption):
    cl = ig_client()
    thumb = str(Path(path).with_suffix(".jpg"))
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [ffmpeg, "-y", "-ss", "1", "-i", path, "-vframes", "1", thumb],
        check=True,
    )
    media = cl.clip_upload(Path(path), caption, thumbnail=Path(thumb))
    return f"https://www.instagram.com/reel/{media.code}/"

# ---------- Telegram ----------
def is_owner(update):
    return update.effective_chat and update.effective_chat.id == OWNER_CHAT_ID


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "Priyas Studio ரெடி.\nஒரு போட்டோ அல்லது பல போட்டோ (slideshow) அல்லது வீடியோ அனுப்புங்க.\n"
        "Caption-ல context எழுதினா AI அதுக்கேத்த மாதிரி எழுதும்.\n"
        "/yt on|off  /ig on|off  /status"
    )


async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(
        f"YouTube: {'ON' if state['yt'] else 'OFF'}\n"
        f"Instagram: {'ON' if state['ig'] else 'OFF'}\n"
        f"Model: {GROQ_MODEL}"
    )


def make_toggle(key):
    async def toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update):
            return
        arg = ctx.args[0].lower() if ctx.args else ""
        if arg not in ("on", "off"):
            await update.message.reply_text(f"/{key} on அல்லது /{key} off")
            return
        state[key] = arg == "on"
        await update.message.reply_text(f"{key.upper()} -> {'ON' if state[key] else 'OFF'}")
    return toggle


async def publish(msg, path, title, caption):
    if state["yt"]:
        await msg.reply_text("YouTube-ல ஏத்தறேன்...")
        try:
            url = await asyncio.to_thread(upload_youtube, path, title, caption)
            await msg.reply_text(f"YouTube ✅ {url}")
        except Exception as e:
            await msg.reply_text(f"YouTube ❌ {e}")

    if state["ig"]:
        await msg.reply_text("Instagram-ல ஏத்தறேன்...")
        try:
            url = await asyncio.to_thread(upload_instagram, path, caption)
            await msg.reply_text(f"Instagram ✅ {url}")
        except Exception as e:
            await msg.reply_text(f"Instagram ❌ {e}")


async def build_and_post(msg, photo_paths, user_text, tmp):
    reel_path = os.path.join(tmp, "reel.mp4")

    try:
        mood, title, caption = await asyncio.to_thread(make_caption, user_text)
    except Exception as e:
        await msg.reply_text(f"Caption ❌ {e}")
        mood, title = "", (user_text or "New reel")[:60]
        caption = user_text or "New reel"

    music = MUSIC_FILE if os.path.exists(MUSIC_FILE) else None
    try:
        await asyncio.to_thread(make_reel, photo_paths, reel_path, music)
    except Exception as e:
        await msg.reply_text(f"Reel ❌ {e}")
        return

    await msg.reply_text(f"Mood: {mood}\nTitle: {title}\n\n{caption}")
    await publish(msg, reel_path, title, caption)

    for p in list(photo_paths) + [reel_path]:
        try:
            os.remove(p)
        except OSError:
            pass


async def album_done(msg, group_id):
    # சில வினாடி காத்திருந்து, album-ல எல்லா போட்டோவும் வந்ததும் reel செய்யும்
    await asyncio.sleep(4)
    data = albums.pop(group_id, None)
    if not data:
        return
    await msg.reply_text(f"{len(data['paths'])} போட்டோ கிடைச்சது, slideshow reel செய்யறேன்...")
    await build_and_post(msg, data["paths"], data["text"], data["tmp"])


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    msg = update.message
    group_id = msg.media_group_id

    if group_id:
        data = albums.get(group_id)
        if data is None:
            data = {"paths": [], "text": msg.caption, "tmp": tempfile.mkdtemp()}
            albums[group_id] = data
            asyncio.create_task(album_done(msg, group_id))
        if msg.caption and not data["text"]:
            data["text"] = msg.caption
        path = os.path.join(data["tmp"], f"p{len(data['paths'])}.jpg")
        tg_file = await msg.photo[-1].get_file()
        await tg_file.download_to_drive(path)
        data["paths"].append(path)
        return

    await msg.reply_text("போட்டோ கிடைச்சது, reel செய்யறேன்...")
    tmp = tempfile.mkdtemp()
    photo_path = os.path.join(tmp, "photo.jpg")
    tg_file = await msg.photo[-1].get_file()
    await tg_file.download_to_drive(photo_path)
    await build_and_post(msg, [photo_path], msg.caption, tmp)


async def handle_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    msg = update.message
    file_obj = msg.video or msg.document
    if not file_obj:
        return

    await msg.reply_text("வீடியோ எடுக்கறேன்...")
    tg_file = await file_obj.get_file()
    path = str(DOWNLOAD_DIR / f"{file_obj.file_unique_id}.mp4")
    await tg_file.download_to_drive(path)

    try:
        mood, title, caption = await asyncio.to_thread(make_caption, msg.caption)
    except Exception as e:
        await msg.reply_text(f"Caption ❌ {e}")
        title = (msg.caption or "New video").split("\n")[0][:100]
        caption = msg.caption or "New video"

    await publish(msg, path, title, caption)

    try:
        os.remove(path)
    except OSError:
        pass


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("yt", make_toggle("yt")))
    app.add_handler(CommandHandler("ig", make_toggle("ig")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.run_polling()


if __name__ == "__main__":
    main()
