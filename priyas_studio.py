import os
import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv
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

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0"))
IG_USERNAME = os.getenv("IG_USERNAME")
IG_PASSWORD = os.getenv("IG_PASSWORD")

IG_SESSION_FILE = "ig_session.json"
YT_CLIENT_SECRET = "client_secret.json"
YT_TOKEN_FILE = "yt_token.json"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

state = {"yt": True, "ig": True}


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
        "Priyas Studio ரெடி.\nவீடியோ அனுப்புங்க (caption-ல title எழுதுங்க).\n"
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


async def handle_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    msg = update.message
    file_obj = msg.video or msg.document
    if not file_obj:
        return

    caption = msg.caption or "New video"
    title = caption.split("\n")[0]
    await msg.reply_text("வீடியோ எடுக்கறேன்...")

    tg_file = await file_obj.get_file()
    path = str(DOWNLOAD_DIR / f"{file_obj.file_unique_id}.mp4")
    await tg_file.download_to_drive(path)

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
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.run_polling()


if __name__ == "__main__":
    main()