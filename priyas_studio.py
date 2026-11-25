import os
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


# ---------- Web ping server (Render-க்கு) ----------
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
    # ஒவ்வொரு 10 நிமிஷத்துக்கும் தன்னையே ping பண்ணி, Telegram-க்கு message அனுப்பும்
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


# ---------- Reel + Caption ----------
def make_reel(photo_path, output_path, music_path=None, duration=15):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
    )
    cmd = [ffmpeg, "-y", "-loop", "1", "-i", photo_path]
    if music_path:
        cmd += ["-i", music_path]
    cmd += ["-vf", vf, "-c:v", "libx264", "-t", str(duration), "-r", "30"]
    if music_path:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [output_path]
    subprocess.run(cmd, check=True)
    return output_path


def make_caption(user_text=None):
    client = Groq(api_key=GROQ_API_KEY)
    prompt = (
        "Write a short, catchy Instagram reel caption in Tamil-English mix "
        "with 5 trending hashtags. Max 3 lines."
    )
    if user_text:
        prompt += f" Photo context: {user_text}"
    r = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
    )
    return r.choices[0].message.content.strip()


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
    media = cl.clip_upload(Path(path), caption)
    return f"https://www.instagram.com/reel/{media.code}/"


# ---------- Telegram ----------
def is_owner(update):
    return update.effective_chat and update.effective_chat.id == OWNER_CHAT_ID


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "Priyas Studio ரெடி.\nபோட்டோ அல்லது வீடியோ அனுப்புங்க (caption-ல title எழுதுங்க).\n"
        "/yt on|off  /ig on|off  /status"
    )


async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(
        f"YouTube: {'ON' if state['yt'] else 'OFF'}\n"
        f"Instagram: {'ON' if state['ig'] else 'OFF'}"
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


async def publish(msg, path, caption):
    title = caption.split("\n")[0]

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


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    msg = update.message
    await msg.reply_text("போட்டோ கிடைச்சது, reel செய்யறேன்...")

    tmp = tempfile.mkdtemp()
    photo_path = os.path.join(tmp, "photo.jpg")
    reel_path = os.path.join(tmp, "reel.mp4")

    tg_file = await msg.photo[-1].get_file()
    await tg_file.download_to_drive(photo_path)

    try:
        caption = await asyncio.to_thread(make_caption, msg.caption)
    except Exception as e:
        await msg.reply_text(f"Caption ❌ {e}")
        caption = msg.caption or "New reel"

    music = MUSIC_FILE if os.path.exists(MUSIC_FILE) else None
    try:
        await asyncio.to_thread(make_reel, photo_path, reel_path, music)
    except Exception as e:
        await msg.reply_text(f"Reel ❌ {e}")
        return

    await msg.reply_text(f"Caption:\n{caption}")
    await publish(msg, reel_path, caption)

    for p in (photo_path, reel_path):
        try:
            os.remove(p)
        except OSError:
            pass


async def handle_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    msg = update.message
    file_obj = msg.video or msg.document
    if not file_obj:
        return

    caption = msg.caption or "New video"
    await msg.reply_text("வீடியோ எடுக்கறேன்...")

    tg_file = await file_obj.get_file()
    path = str(DOWNLOAD_DIR / f"{file_obj.file_unique_id}.mp4")
    await tg_file.download_to_drive(path)

    await publish(msg, path, caption)

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
