""" Priyas Studio v2 - 3 accounts (comedy / horror / romance), each with its own theme, voice, YouTube token, Instagram token - Talk to the bot normally (Tamil, Tanglish or English). No slash commands. Bot replies in English. - 6-agent pipeline, trending research, auto-picked posting times that learn from your results - Asks YOU for approval (with the reel preview) before anything is posted - Instagram through Meta's official API (no username/password login) - All times in IST """
import os
import json
import random
import asyncio
import logging
import subprocess
import tempfile
import shutil
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import imageio_ffmpeg
import edge_tts
from dotenv import load_dotenv
from groq import Groq
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TimedOut, NetworkError
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

load_dotenv()

try:
    import self_upgrade as SU
except Exception as _e:
    SU = None
    logging.warning('self_upgrade.py not loaded: %s', _e)
logging.basicConfig(level=logging.INFO)
# httpx logs full URLs at INFO level, which would print your bot token in logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ======================= CONFIG =======================
# Change this text whenever you upload a new file. The "Bot started" message shows it.
VERSION = "merged-final-v3-vote"
IST = ZoneInfo("Asia/Kolkata")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
YT_API_KEY = os.getenv("YT_API_KEY")  # optional: trending + view counts
IG_API_VERSION = os.getenv("IG_API_VERSION", "v23.0")

YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
IG_HOST = "https://graph.instagram.com"
IG_TOKEN_FILE = Path("ig_tokens.json")
MEMORY_FILE = "memory.json"
MUSIC_DIR = Path("music")  # music/<account>/*.mp3 first, else music/*.mp3

MAX_POSTS_PER_DAY = int(os.getenv("MAX_POSTS_PER_DAY", "2"))  # per account
PREP_MINUTES = int(os.getenv("PREP_MINUTES", "40"))  # preview is sent this long before the post time
APPROVAL_TIMEOUT_MIN = int(os.getenv("APPROVAL_TIMEOUT_MIN", "180"))
CANDIDATE_SLOTS = ["07:30", "09:00", "12:30", "16:00", "18:00", "20:00", "21:30"]  # IST

ACCOUNTS = {
    "comedy": {
        "label": "Comedy & Memes",
        "niche": "comedy and meme-style reels: relatable everyday humour, funny one-liners, light sarcasm",
        "voice": "en-US-GuyNeural",
    },
    "horror": {
        "label": "Horror & Thrilling",
        "niche": "horror and thriller micro-stories, creepy facts, suspenseful twists (no gore, no real tragedies)",
        "voice": "en-US-ChristopherNeural",
    },
    "romance": {
        "label": "Love & Romance",
        "niche": "love and romance: heartfelt quotes, sweet relationship moments, longing and feelings",
        "voice": "en-US-AriaNeural",
    },
}
FIRST_ACCOUNT = next(iter(ACCOUNTS))

# Fixed-date festivals only (Diwali etc. move every year, so they are left out on purpose)
FESTIVALS = {
    (1, 1): "New Year",
    (1, 14): "Pongal",
    (4, 14): "Tamil New Year",
    (8, 15): "Independence Day",
    (12, 25): "Christmas",
}

# ======================= TIME HELPERS =======================
def now_ist():
    return datetime.now(IST)


def today_str():
    return now_ist().strftime("%Y-%m-%d")


def hm_to_min(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def now_min():
    n = now_ist()
    return n.hour * 60 + n.minute


def nearest_slot():
    m = now_min()
    return min(CANDIDATE_SLOTS, key=lambda s: abs(hm_to_min(s) - m))


# ======================= MEMORY & STATE =======================
def load_memory():
    base = {"used": [], "posts": {}, "slots": {}, "history": [], "plan": {}, "state": {}, "niches": {}}
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE) as f:
                base.update(json.load(f))
        except Exception:
            pass
    return base


def save_memory():
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f)


memory = load_memory()

for _aid, _niche in memory.get("niches", {}).items():
    if _aid in ACCOUNTS:
        ACCOUNTS[_aid]["niche"] = _niche


def get_state(aid):
    st = memory["state"].setdefault(aid, {})
    st.setdefault("auto", True)
    st.setdefault("yt", True)
    st.setdefault("ig", True)
    return st


def global_auto():
    return memory["state"].get("_global_auto", True)


def set_global_auto(v):
    memory["state"]["_global_auto"] = v
    save_memory()


def set_state(aid, key, value):
    get_state(aid)[key] = value
    save_memory()


def posts_today(aid):
    return memory["posts"].get(today_str(), {}).get(aid, 0)


def count_post(aid):
    day = memory["posts"].setdefault(today_str(), {})
    day[aid] = day.get(aid, 0) + 1
    # keep only the last 14 days
    for k in sorted(memory["posts"])[:-14]:
        memory["posts"].pop(k, None)
    save_memory()


# ======================= AI & 6-AGENT PIPELINE =======================
def ask_ai(prompt, system_prompt=None, max_tokens=1500):
    if HAS_ANTHROPIC and ANTHROPIC_API_KEY:
        aclient = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        kwargs = {
            "model": CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        resp = aclient.messages.create(**kwargs)
        return resp.content[0].text.strip()

    client = Groq(api_key=GROQ_API_KEY)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    r = client.chat.completions.create(model=GROQ_MODEL, messages=messages)
    return r.choices[0].message.content.strip()


def extract_json(text):
    s, e = text.index("{"), text.rindex("}") + 1
    return json.loads(text[s:e])


def run_agent(role, system_prompt, user_input):
    logging.info("[%s] running...", role)
    return ask_ai(user_input, system_prompt=f"You are a {role}. {system_prompt}")


_trend_cache = {"t": 0, "titles": []}


def fetch_trending():
    """Optional: needs YT_API_KEY. Cached for 6 hours."""
    if not YT_API_KEY:
        return []
    if time.time() - _trend_cache["t"] < 6 * 3600 and _trend_cache["titles"]:
        return _trend_cache["titles"]
    titles = []
    for region in ("IN", "US"):
        try:
            r = requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "snippet", "chart": "mostPopular", "regionCode": region,
                        "maxResults": 10, "key": YT_API_KEY},
                timeout=20,
            )
            if r.ok:
                titles += [i["snippet"]["title"] for i in r.json().get("items", [])]
        except Exception as e:
            logging.warning("trending fetch failed: %s", e)
    _trend_cache.update({"t": time.time(), "titles": titles[:20]})
    return _trend_cache["titles"]


# Agent 1: Researcher
def researcher_agent(aid, trending, festival=None):
    niche = ACCOUNTS[aid]["niche"]
    extra = f" Today is {festival}; work it in if it fits." if festival else ""
    if trending:
        hint = "Currently popular on YouTube: " + " | ".join(trending) + ". Use these only as a signal of what people like right now."
    else:
        hint = "Use your knowledge of what is trending in short-form video right now."
    return run_agent(
        "Researcher Agent",
        "Find trending topics, viral hooks and audience pain points for short-form Reels/Shorts in English. "
        "Pick ONE fresh angle that fits the niche.",
        f"Niche: {niche}.{extra}\n{hint}",
    )


# Agent 2: Competitor analysis
def competitor_analysis_agent(aid, research_notes):
    return run_agent(
        "Competitor Analysis Agent",
        "Analyze competitor short-form content patterns and define a high-retention structure, "
        "the best mood and the visual direction.",
        f"Niche: {ACCOUNTS[aid]['niche']}\n\n{research_notes}",
    )


# Agent 3: Video generator (script package)
def video_generator_agent(aid, strategy):
    prompt = (
        "Based on this strategy, create a viral Instagram Reel / YouTube Short package in English for the niche: "
        f"{ACCOUNTS[aid]['niche']}. "
        "Write: a short quote or line of max 20 words for voiceover; "
        "a hook/title of max 60 chars; a caption with emojis, a call to action and 8 relevant hashtags; "
        "and a 2-4 word search_query for stock footage that matches the mood. "
        'Return ONLY valid JSON with keys: "mood", "title", "quote", "caption", "search_query".'
    )
    text = run_agent("Video Generator Agent", prompt, strategy)
    try:
        d = extract_json(text)
        return {
            "mood": d.get("mood", "engaging"),
            "title": str(d.get("title", "New Reel"))[:90],
            "quote": d.get("quote", ""),
            "caption": d.get("caption", text),
            "search_query": d.get("search_query", ""),
        }
    except Exception:
        return {"mood": "engaging", "title": text.split("\n")[0][:90],
                "quote": "", "caption": text, "search_query": ""}


# Agent 5: Approval (safety check, before it even reaches you)
def approval_agent(pkg):
    if not pkg.get("title") or not pkg.get("caption"):
        return False
    verdict = run_agent(
        "Approval Agent",
        "Check if the reel script, quote and caption are safe, clear and engaging, with no hate, "
        "graphic violence, copyrighted lyrics/quotes or misleading claims. Reply ONLY with APPROVED or REJECTED.",
        json.dumps(pkg),
    )
    return "REJECTED" not in verdict.upper()


# ======================= PEXELS MEDIA =======================
def fetch_photos(query, count, folder):
    headers = {"Authorization": PEXELS_API_KEY}
    r = requests.get(
        "https://api.pexels.com/v1/search", headers=headers,
        params={"query": query, "per_page": 40, "page": random.randint(1, 10), "orientation": "portrait"},
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
            continue
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


def fetch_video(query, folder):
    headers = {"Authorization": PEXELS_API_KEY}
    r = requests.get(
        "https://api.pexels.com/videos/search", headers=headers,
        params={"query": query, "per_page": 30, "page": random.randint(1, 5), "orientation": "portrait"},
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


# ======================= MUSIC + VOICE =======================
def pick_music(aid):
    for folder in (MUSIC_DIR / aid, MUSIC_DIR):
        if folder.exists():
            files = list(folder.glob("*.mp3"))
            if files:
                return str(random.choice(files))
    return None


async def make_voice(text, path, voice):
    if not text:
        return None
    try:
        await edge_tts.Communicate(text, voice).save(path)
        return path
    except Exception as e:
        logging.warning("voice failed: %s", e)
        return None


# ======================= REEL MAKING (Agent 4: Video Editing) =======================
def run_ffmpeg(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def audio_filter(music_idx, voice_idx):
    if music_idx is not None and voice_idx is not None:
        return (
            f"[{music_idx}:a]volume=0.25[ma];[{voice_idx}:a]volume=1.4[va];"
            f"[ma][va]amix=inputs=2:duration=longest[aud]"
        ), "[aud]"
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


async def video_editing_agent(pkg, aid, tmp):
    music = pick_music(aid)
    voice = await make_voice(pkg["quote"], os.path.join(tmp, "voice.mp3"), ACCOUNTS[aid]["voice"])
    out = os.path.join(tmp, "reel.mp4")
    media_query = pkg.get("search_query") or ACCOUNTS[aid]["niche"].split(":")[0]
    made = False
    if random.random() < 0.5:
        clip = await asyncio.to_thread(fetch_video, media_query, tmp)
        if clip:
            await asyncio.to_thread(make_video_reel, clip, out, music, voice)
            made = True
    if not made:
        photos = await asyncio.to_thread(fetch_photos, media_query, 3, tmp)
        if len(photos) < 3:
            raise RuntimeError("Not enough good photos found")
        await asyncio.to_thread(make_photo_reel, photos, out, music, voice)
    return out


# ======================= YOUTUBE (one token per account) =======================
def yt_token_path(aid):
    p = Path(f"yt_token_{aid}.json")
    if not p.exists() and aid == FIRST_ACCOUNT and Path("yt_token.json").exists():
        return Path("yt_token.json")
    return p


def yt_configured(aid):
    return yt_token_path(aid).exists() or bool(os.getenv(f"YT_TOKEN_JSON_{aid.upper()}"))


def yt_creds(aid):
    path = yt_token_path(aid)
    if path.exists():
        creds = Credentials.from_authorized_user_file(str(path), YT_SCOPES)
    else:
        raw = os.getenv(f"YT_TOKEN_JSON_{aid.upper()}")
        if not raw:
            raise RuntimeError(f"No YouTube token for '{aid}'")
        creds = Credentials.from_authorized_user_info(json.loads(raw), YT_SCOPES)
        path = None
    if not creds.valid:
        if creds.refresh_token:
            creds.refresh(Request())
            if path:
                path.write_text(creds.to_json())
        else:
            raise RuntimeError(f"YouTube token for '{aid}' is invalid")
    return creds


def upload_youtube(aid, path, title, description):
    yt = build("youtube", "v3", credentials=yt_creds(aid))
    if "#shorts" not in description.lower():
        description = description + "\n\n#Shorts"
    body = {
        "snippet": {"title": title[:100], "description": description[:4900], "categoryId": "22"},
        "status": {"privacyStatus": "public"},
    }
    media = MediaFileUpload(path, chunksize=-1, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"], f"https://youtu.be/{resp['id']}"


# ======================= INSTAGRAM (official Meta API, one token per account) =======================
def _ig_store():
    if IG_TOKEN_FILE.exists():
        try:
            return json.loads(IG_TOKEN_FILE.read_text())
        except Exception:
            pass
    return {}


def _ig_save(store):
    IG_TOKEN_FILE.write_text(json.dumps(store))
    try:
        os.chmod(IG_TOKEN_FILE, 0o600)
    except OSError:
        pass


def ig_creds(aid):
    key = aid.upper()
    env_token = os.getenv(f"IG_TOKEN_{key}")
    user_id = os.getenv(f"IG_USER_ID_{key}")
    entry = _ig_store().get(aid, {})
    # a refreshed token is used only while the env token is still the one it came from
    if entry.get("token") and entry.get("env_token") == env_token:
        return user_id, entry["token"]
    return user_id, env_token


def ig_configured(aid):
    uid, tok = ig_creds(aid)
    return bool(uid and tok)


def _check(r, what):
    if not r.ok:
        raise RuntimeError(f"{what} failed ({r.status_code}): {r.text[:250]}")
    return r.json()


def upload_instagram(aid, path, caption):
    user_id, token = ig_creds(aid)
    if not user_id or not token:
        raise RuntimeError(f"Instagram token / user id not set for '{aid}'")
    auth = {"Authorization": f"Bearer {token}"}
    base = f"{IG_HOST}/{IG_API_VERSION}"
    size = os.path.getsize(path)

    # 1) create a resumable container (so no public video URL is needed)
    j = _check(requests.post(
        f"{base}/{user_id}/media", headers=auth, timeout=60,
        data={"media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200]},
    ), "Instagram container")
    cid = j["id"]
    uri = j.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{IG_API_VERSION}/{cid}"

    # 2) upload the bytes
    with open(path, "rb") as f:
        up = requests.post(
            uri, timeout=600, data=f,
            headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)},
        )
    _check(up, "Instagram upload")

    # 3) wait until processing is finished
    for _ in range(48):
        st = _check(requests.get(f"{base}/{cid}", headers=auth, timeout=30,
                                 params={"fields": "status_code,status"}), "Instagram status")
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram processing {code}: {st.get('status')}")
        time.sleep(5)
    else:
        raise RuntimeError("Instagram processing timed out")

    # 4) publish
    pub = _check(requests.post(f"{base}/{user_id}/media_publish", headers=auth, timeout=60,
                               data={"creation_id": cid}), "Instagram publish")
    media_id = pub["id"]
    link = ""
    try:
        link = requests.get(f"{base}/{media_id}", headers=auth, timeout=30,
                            params={"fields": "permalink"}).json().get("permalink", "")
    except Exception:
        pass
    return media_id, link or f"(media id {media_id})"


def refresh_ig_tokens():
    """Long-lived tokens last 60 days. Refresh any that are older than 30 days."""
    store = _ig_store()
    changed = []
    for aid in ACCOUNTS:
        uid, token = ig_creds(aid)
        if not token:
            continue
        entry = store.get(aid, {})
        if time.time() - entry.get("refreshed", 0) < 30 * 86400:
            continue
        try:
            r = requests.get(f"{IG_HOST}/refresh_access_token", timeout=30,
                             params={"grant_type": "ig_refresh_token", "access_token": token})
            j = _check(r, "Instagram token refresh")
            store[aid] = {"token": j["access_token"], "env_token": os.getenv(f"IG_TOKEN_{aid.upper()}"),
                          "refreshed": time.time()}
            changed.append(aid)
        except Exception as e:
            logging.warning("IG refresh failed for %s: %s", aid, e)
    if changed:
        _ig_save(store)
    return changed


# ======================= UPLOAD AGENT (Agent 6) =======================
async def auto_upload_agent(aid, out, title, caption):
    st = get_state(aid)
    results, ids = [], {}
    if st["yt"]:
        try:
            vid, url = await asyncio.to_thread(upload_youtube, aid, out, title, caption)
            ids["yt_id"], ids["yt_url"] = vid, url
            results.append(f"YouTube OK {url}")
        except HttpError as e:
            if "uploadLimitExceeded" in str(e):
                results.append("YouTube FAILED: daily upload limit reached")
            else:
                results.append(f"YouTube FAILED {str(e)[:150]}")
        except Exception as e:
            results.append(f"YouTube FAILED {str(e)[:150]}")
    if st["ig"]:
        try:
            mid, url = await asyncio.to_thread(upload_instagram, aid, out, caption)
            ids["ig_id"], ids["ig_url"] = mid, url
            results.append(f"Instagram OK {url}")
        except Exception as e:
            results.append(f"Instagram FAILED {str(e)[:200]}")
    if not st["yt"] and not st["ig"]:
        results.append("Both YouTube and Instagram are switched off for this account.")
    return results, ids


# ======================= LEARNING: best posting times =======================
def choose_slots(aid, k):
    stats = memory["slots"].get(aid, {})
    chosen = []
    for _ in range(k):
        pool = [s for s in CANDIDATE_SLOTS
                if s not in chosen and all(abs(hm_to_min(s) - hm_to_min(c)) >= 120 for c in chosen)]
        if not pool:
            break
        unexplored = [s for s in pool if stats.get(s, {}).get("n", 0) < 2]
        if unexplored:
            chosen.append(random.choice(unexplored))
        elif random.random() < 0.2:
            chosen.append(random.choice(pool))  # keep exploring a little
        else:
            chosen.append(max(pool, key=lambda s: stats[s]["total"] / stats[s]["n"]))
    return sorted(chosen)


def plan_for_today():
    if memory["plan"].get("date") == today_str():
        return
    items = []
    for aid in ACCOUNTS:
        for slot in choose_slots(aid, MAX_POSTS_PER_DAY):
            items.append({"aid": aid, "slot": slot, "prepared": False})
    memory["plan"] = {"date": today_str(), "items": items}
    save_memory()


def collect_stats():
    """Measure posts older than 24h. Score = YouTube views + 5 x (Instagram likes + comments)."""
    changed = False
    for h in memory["history"]:
        if h.get("measured") or time.time() - h["ts"] < 86400:
            continue
        score, got = 0, False
        if h.get("yt_id") and YT_API_KEY:
            try:
                r = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=20,
                                 params={"part": "statistics", "id": h["yt_id"], "key": YT_API_KEY})
                items = r.json().get("items", [])
                if items:
                    score += int(items[0]["statistics"].get("viewCount", 0))
                    got = True
            except Exception as e:
                logging.warning("yt stats failed: %s", e)
        if h.get("ig_id"):
            try:
                uid, token = ig_creds(h["aid"])
                r = requests.get(f"{IG_HOST}/{IG_API_VERSION}/{h['ig_id']}", timeout=20,
                                 headers={"Authorization": f"Bearer {token}"},
                                 params={"fields": "like_count,comments_count"})
                if r.ok:
                    j = r.json()
                    score += 5 * (int(j.get("like_count", 0)) + int(j.get("comments_count", 0)))
                    got = True
            except Exception as e:
                logging.warning("ig stats failed: %s", e)
        if got:
            s = memory["slots"].setdefault(h["aid"], {}).setdefault(h["slot"], {"n": 0, "total": 0})
            s["n"] += 1
            s["total"] += score
            h["score"] = score
            h["measured"] = True
            changed = True
    memory["history"] = memory["history"][-100:]
    if changed:
        save_memory()


# ======================= PENDING APPROVALS =======================
PENDING = {}  # pid -> dict. Kept in RAM: a restart clears waiting previews.
BACKGROUND = set()
_lock = None


def get_lock():
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def spawn(coro):
    t = asyncio.create_task(coro)
    BACKGROUND.add(t)
    t.add_done_callback(BACKGROUND.discard)
    return t


async def notify(app, text):
    for attempt in range(3):
        try:
            await app.bot.send_message(chat_id=OWNER_CHAT_ID, text=text[:4000])
            return
        except (TimedOut, NetworkError) as e:
            logging.warning("notify timeout (try %s): %s", attempt + 1, e)
            await asyncio.sleep(3)
        except Exception as e:
            logging.warning("notify failed: %s", e)
            return


def cleanup(p):
    try:
        shutil.rmtree(p.get("tmp", ""), ignore_errors=True)
    except Exception:
        pass


def waiting_pendings(aid=None):
    return [pid for pid, p in PENDING.items()
            if p["status"] == "waiting" and (aid is None or p["aid"] == aid)]


async def prepare_reel(app, aid, slot=None):
    """Make a reel and send it to the owner for approval."""
    label = ACCOUNTS[aid]["label"]
    async with get_lock():
        tmp = tempfile.mkdtemp()
        try:
            n = now_ist()
            festival = FESTIVALS.get((n.month, n.day))
            trending = await asyncio.to_thread(fetch_trending)
            research = await asyncio.to_thread(researcher_agent, aid, trending, festival)
            strategy = await asyncio.to_thread(competitor_analysis_agent, aid, research)
            pkg = await asyncio.to_thread(video_generator_agent, aid, strategy)
            if not await asyncio.to_thread(approval_agent, pkg):
                await notify(app, f"[{label}] The safety check rejected the generated content, so I skipped it.")
                shutil.rmtree(tmp, ignore_errors=True)
                return
            out = await video_editing_agent(pkg, aid, tmp)
        except Exception as e:
            await notify(app, f"[{label}] Reel failed: {str(e)[:300]}\n{traceback.format_exc()[-300:]}")
            shutil.rmtree(tmp, ignore_errors=True)
            return

    pid = uuid.uuid4().hex[:8]
    PENDING[pid] = {"aid": aid, "pkg": pkg, "path": out, "tmp": tmp, "slot": slot,
                    "status": "waiting", "created": time.time(), "post_at": None}
    st = get_state(aid)
    where = " + ".join([x for x, on in (("YouTube", st["yt"]), ("Instagram", st["ig"])) if on]) or "nowhere (both off)"
    when = f"at {slot} IST" if slot else "right after you approve"
    caption = (
        f"[{label}] {pkg['title']}\n"
        f"Mood: {pkg['mood']}\nQuote: {pkg['quote']}\n"
        f"Posting {when} to {where}\n\n{pkg['caption']}"
    )[:1000]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Post it", callback_data=f"ok:{pid}"),
        InlineKeyboardButton("Skip", callback_data=f"no:{pid}"),
    ]])
    try:
        with open(out, "rb") as vf:
            await app.bot.send_video(chat_id=OWNER_CHAT_ID, video=vf, caption=caption, reply_markup=kb,
                                     read_timeout=180, write_timeout=180, connect_timeout=30)
    except Exception as e:
        await notify(app, f"[{label}] Reel is ready but I couldn't send you the preview: {str(e)[:200]}")
        cleanup(PENDING.pop(pid))


async def approve(app, pid):
    p = PENDING.get(pid)
    if not p or p["status"] != "waiting":
        return "That one is no longer waiting for approval."
    slot = p.get("slot")
    if slot and hm_to_min(slot) > now_min():
        p["status"], p["post_at"] = "approved", hm_to_min(slot)
        return f"Approved. I'll post it at {slot} IST."
    p["status"] = "posting"
    spawn(post_pending(app, pid))
    return "Approved. Posting now."


def reject(pid):
    p = PENDING.pop(pid, None)
    if not p:
        return "That one is no longer waiting."
    cleanup(p)
    return "Skipped. Nothing was posted."


async def post_pending(app, pid):
    p = PENDING.get(pid)
    if not p:
        return
    aid, pkg = p["aid"], p["pkg"]
    label = ACCOUNTS[aid]["label"]
    try:
        results, ids = await auto_upload_agent(aid, p["path"], pkg["title"], pkg["caption"])
        if any(" OK " in r for r in results):
            count_post(aid)
            memory["history"].append({"aid": aid, "slot": p.get("slot") or nearest_slot(), "ts": time.time(),
                                      "date": today_str(), "title": pkg["title"], **ids})
            save_memory()
        await notify(app, f"[{label}] {pkg['title']}\n" + "\n".join(results))
    except Exception as e:
        await notify(app, f"[{label}] Posting crashed: {str(e)[:300]}")
    finally:
        cleanup(PENDING.pop(pid, p))


# ======================= SCHEDULER =======================
async def tick(ctx: ContextTypes.DEFAULT_TYPE):
    app = ctx.application
    plan_for_today()
    nm = now_min()

    if global_auto():
        for item in memory["plan"]["items"]:
            if item["prepared"]:
                continue
            aid, slot = item["aid"], item["slot"]
            diff = hm_to_min(slot) - nm
            if diff > PREP_MINUTES:
                continue
            item["prepared"] = True
            save_memory()
            if diff < -90:  # bot was down, slot is long gone
                continue
            if not get_state(aid)["auto"]:
                continue
            pending_here = len([1 for p in PENDING.values() if p["aid"] == aid])
            if posts_today(aid) + pending_here >= MAX_POSTS_PER_DAY:
                continue
            spawn(prepare_reel(app, aid, slot))

    for pid, p in list(PENDING.items()):
        if p["status"] == "approved" and p["post_at"] is not None and p["post_at"] <= nm:
            p["status"] = "posting"
            spawn(post_pending(app, pid))
        elif p["status"] == "waiting" and time.time() - p["created"] > APPROVAL_TIMEOUT_MIN * 60:
            label = ACCOUNTS[p["aid"]]["label"]
            cleanup(PENDING.pop(pid))
            await notify(app, f"[{label}] You didn't answer in time, so I did not post that reel.")


async def daily_maintenance(ctx: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(refresh_ig_tokens)
    await asyncio.to_thread(collect_stats)


# ======================= TELEGRAM: talking to the bot =======================
def is_owner(update):
    return update.effective_chat and update.effective_chat.id == OWNER_CHAT_ID


YES = {"yes", "y", "ok", "okay", "post", "post it", "sure", "podu", "pottudu", "approve", "approved",
       "சரி", "போடு", "ஓகே", "ok da"}
NO = {"no", "n", "nope", "reject", "cancel", "skip", "venam", "vendam", "வேண்டாம்", "வேணாம்"}


def status_text():
    lines = [f"Global auto-posting: {'ON' if global_auto() else 'OFF'}",
             f"AI model: {CLAUDE_MODEL if (HAS_ANTHROPIC and ANTHROPIC_API_KEY) else GROQ_MODEL}",
             f"Time now: {now_ist().strftime('%d %b, %H:%M')} IST", ""]
    for aid, a in ACCOUNTS.items():
        st = get_state(aid)
        lines.append(
            f"{a['label']} ({aid})\n"
            f" auto {'ON' if st['auto'] else 'OFF'} | YouTube {'ON' if st['yt'] else 'OFF'}"
            f"{'' if yt_configured(aid) else ' (not connected)'} | Instagram {'ON' if st['ig'] else 'OFF'}"
            f"{'' if ig_configured(aid) else ' (not connected)'}\n"
            f" posts today {posts_today(aid)}/{MAX_POSTS_PER_DAY}\n"
            f" theme: {a['niche']}"
        )
    items = memory["plan"].get("items", [])
    if items:
        lines.append("\nToday's plan:")
        for it in items:
            lines.append(f" {it['slot']} {it['aid']}{' (preview sent)' if it['prepared'] else ''}")
    w = waiting_pendings()
    if w:
        lines.append(f"\n{len(w)} reel(s) waiting for your approval.")
    return "\n".join(lines)


def stats_text():
    lines = []
    for aid, a in ACCOUNTS.items():
        stats = memory["slots"].get(aid, {})
        ranked = sorted(((s, v["total"] / v["n"], v["n"]) for s, v in stats.items() if v["n"]),
                        key=lambda x: -x[1])
        if ranked:
            best = ", ".join(f"{s} IST (avg {int(avg)}, {n} posts)" for s, avg, n in ranked[:3])
            lines.append(f"{a['label']}: best times so far: {best}")
        else:
            lines.append(f"{a['label']}: still learning, not enough measured posts yet.")
    recent = memory["history"][-6:]
    if recent:
        lines.append("\nRecent posts:")
        for h in reversed(recent):
            link = h.get("yt_url") or h.get("ig_url") or ""
            lines.append(f" {h['date']} {h['slot']} [{h['aid']}] {h['title'][:40]} {link}"
                         f"{' score ' + str(h['score']) if 'score' in h else ''}")
    if not YT_API_KEY:
        lines.append("\n(Add a YT_API_KEY to see YouTube views and use real trending data.)")
    return "\n".join(lines)


ROUTER_SYSTEM = (
    "You are the intent router for a social media bot. The owner writes in Tamil, Tanglish or English.\n"
    f"Accounts: {', '.join(ACCOUNTS)}.\n"
    "Return ONLY JSON: {\"intent\": ..., \"account\": ..., \"value\": ...}\n"
    "intents: make_reel (account = id or \"all\" or null), approve, reject, status, stats, auto_on, auto_off, "
    "platform_on, platform_off (value = \"yt\" or \"ig\"; account = id or \"all\"), "
    "set_niche (account = id, value = new theme description), "
    "set_setting (value = \"key=text\" where key is bot_name, owner_name, reply_language, add_rule or remove_rule), "
    "upgrade (value = a clear description of the new feature or code change the owner wants), chat.\n"
    "approve/reject: the owner is answering a waiting preview (account optional). "
    "auto_on/auto_off: account id or \"all\"/null for everything. "
    "Use chat for anything else (questions, ideas, greetings)."
)


def _ask_provider(provider, prompt, system_prompt, max_tokens):
    if provider == "claude":
        aclient = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = aclient.messages.create(model=CLAUDE_MODEL, max_tokens=max_tokens, system=system_prompt,
                                       messages=[{"role": "user", "content": prompt}])
        return resp.content[0].text.strip()
    client = Groq(api_key=GROQ_API_KEY)
    r = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}])
    return r.choices[0].message.content.strip()


def route_intent(text, n_waiting):
    """Ask Claude and Groq separately. Same intent -> use it. Different -> 'unsure' (ask the owner again)."""
    prompt = f"Waiting previews: {n_waiting}\nOwner message: {text}"
    providers = []
    if HAS_ANTHROPIC and ANTHROPIC_API_KEY:
        providers.append("claude")
    if GROQ_API_KEY:
        providers.append("groq")

    def one(p):
        try:
            d = extract_json(_ask_provider(p, prompt, ROUTER_SYSTEM, 300))
            return d.get("intent", "chat"), d.get("account"), d.get("value")
        except Exception as e:
            logging.warning("router %s failed: %s", p, e)
            return None

    if not providers:
        return "chat", None, None
    with ThreadPoolExecutor(max_workers=len(providers)) as ex:
        results = [r for r in ex.map(one, providers) if r]
    if not results:
        return "chat", None, None
    if len(results) == 1 or results[0][0] == results[1][0]:
        return results[0]
    logging.info("router disagreed: %s", results)
    return "unsure", None, None


def target_accounts(account):
    if account in ACCOUNTS:
        return [account]
    return list(ACCOUNTS)


async def handle_owner_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text):
    app = ctx.application
    reply = update.message.reply_text
    clean = text.strip().lower().strip(".!?, ")
    waiting = waiting_pendings()

    # quick yes/no for a waiting preview
    if waiting and (clean in YES or clean in NO):
        if len(waiting) > 1:
            await reply("More than one reel is waiting. Please tap the Post it / Skip button under the one you mean.")
            return
        await reply(await approve(app, waiting[0]) if clean in YES else reject(waiting[0]))
        return

    if SU:
        SU.add_turn("user", text)

    # direct rule: reel/video words -> start a reel without asking the AI router
    if SU and SU.looks_like_reel_command(text):
        intent, account, value = "make_reel", (SU.guess_account(text) or "all"), None
    else:
        intent, account, value = route_intent(text, len(waiting))

    if intent in ("approve", "reject"):
        cands = waiting_pendings(account if account in ACCOUNTS else None)
        if not cands:
            await reply("Nothing is waiting for approval right now.")
        elif len(cands) > 1:
            await reply("More than one reel is waiting. Tell me which account, or tap the buttons.")
        else:
            await reply(await approve(app, cands[0]) if intent == "approve" else reject(cands[0]))

    elif intent == "make_reel":
        if account not in ACCOUNTS and account != "all":
            await reply("Which account should I make it for: " + ", ".join(ACCOUNTS) + ", or all?")
            return
        targets = target_accounts(account)
        await reply(f"On it. I'll send you the preview for {', '.join(targets)} when it's ready.")
        for aid in targets:
            spawn(prepare_reel(app, aid, None))

    elif intent == "status":
        await reply(status_text())

    elif intent == "stats":
        await reply(stats_text())

    elif intent in ("auto_on", "auto_off"):
        on = intent == "auto_on"
        if account in ACCOUNTS:
            set_state(account, "auto", on)
            await reply(f"Auto-posting for {account} is now {'ON' if on else 'OFF'}.")
        else:
            set_global_auto(on)
            await reply(f"Auto-posting is now {'ON' if on else 'OFF'} for all accounts.")

    elif intent in ("platform_on", "platform_off") and value in ("yt", "ig"):
        on = intent == "platform_on"
        for aid in target_accounts(account):
            set_state(aid, value, on)
        name = "YouTube" if value == "yt" else "Instagram"
        scope = account if account in ACCOUNTS else "all accounts"
        await reply(f"{name} is now {'ON' if on else 'OFF'} for {scope}.")

    elif intent == "set_niche" and account in ACCOUNTS and value:
        ACCOUNTS[account]["niche"] = value[:300]
        memory["niches"][account] = value[:300]
        save_memory()
        await reply(f"Theme for {account} updated. New reels will follow it.")

    elif intent == "unsure":
        await reply("I am not fully sure what you meant. Please say it once more, for example: make a comedy reel.")

    elif intent == "set_setting" and SU and value and "=" in str(value):
        k, _, v = str(value).partition("=")
        ok = SU.apply_setting(k.strip(), v.strip())
        await reply("Done, setting updated: " + str(value)[:100] if ok else "I could not change that setting.")

    elif intent == "upgrade" and SU and value:
        await reply("Okay, writing the upgrade into a NEW file. The old code will not be touched...")
        try:
            new_code = await asyncio.to_thread(
                ask_ai,
                "Write a small standalone Python module for this change request: " + str(value) +
                "\nReturn ONLY Python code, no markdown. It must not delete files or import anything unusual.",
                "You are a careful Python developer.", 2500)
            new_code = new_code.replace("```python", "").replace("```", "").strip()
            path, err = SU.write_upgrade(str(value)[:30], new_code)
            if err:
                await reply(err + "\nThe old code is unchanged and still running.")
            else:
                await reply(f"Upgrade saved as {path.name}. It will be active after the next restart. Old code untouched.")
        except Exception as e:
            await reply(f"Upgrade failed, old code is fine: {str(e)[:150]}")

    else:
        try:
            _persona = SU.persona_prompt() if SU else (
                "You are the assistant of a social media creator who runs 3 accounts (comedy, horror, romance). "
                "Reply briefly in English, even if the owner writes in Tamil.\n")
            _hist = SU.history_text() if SU else ""
            answer = await asyncio.to_thread(
                ask_ai, _hist + "Owner message: " + text,
                _persona + "Current bot state:\n" + status_text(),
            )
            if SU:
                SU.add_turn("bot", answer)
        except Exception as e:
            answer = f"AI error: {e}"
        await reply(answer[:4000])


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message or not update.message.text:
        return
    await handle_owner_text(update, ctx, update.message.text)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Voice message -> text (Groq Whisper) -> same as typing."""
    if not is_owner(update) or not update.message:
        return
    media = update.message.voice or update.message.audio
    if not media:
        return
    try:
        f = await ctx.bot.get_file(media.file_id)
        data = bytes(await f.download_as_bytearray())

        def transcribe():
            client = Groq(api_key=GROQ_API_KEY)
            r = client.audio.transcriptions.create(file=("voice.ogg", data), model="whisper-large-v3-turbo")
            return r.text.strip()

        text = await asyncio.to_thread(transcribe)
    except Exception as e:
        await update.message.reply_text(f"I couldn't understand that voice message: {str(e)[:150]}")
        return
    if not text:
        await update.message.reply_text("I couldn't hear anything in that voice message.")
        return
    await update.message.reply_text(f"I heard: {text}")
    await handle_owner_text(update, ctx, text)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.message or q.message.chat.id != OWNER_CHAT_ID:
        return
    await q.answer()
    action, _, pid = (q.data or "").partition(":")
    msg = await approve(ctx.application, pid) if action == "ok" else reject(pid)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await ctx.application.bot.send_message(chat_id=OWNER_CHAT_ID, text=msg)


async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "Hi! Just talk to me normally, in Tamil or English, by text or voice.\n"
        "For example: make a comedy reel, how are we doing, stop Instagram for horror."
    )


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    # Network hiccups are normal and retried by polling; do not spam the owner.
    if isinstance(ctx.error, (TimedOut, NetworkError)):
        logging.warning("Telegram network error: %s", ctx.error)
        return
    logging.error("Bot error: %s", ctx.error)
    await notify(ctx.application, f"Bot error: {str(ctx.error)[:300]}")


async def on_startup(app):
    # Sent once every time the bot starts, so you can see which code is running.
    extra = ""
    if SU:
        ok, bad = SU.load_upgrades()
        extra = f" | upgrades loaded: {len(ok)}" + (f", skipped broken: {len(bad)}" if bad else "")
    await notify(app, f"Bot started \u2705 (version: {VERSION}){extra}")


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(tick, interval=300, first=15)
    app.job_queue.run_daily(daily_maintenance, time=dtime(3, 0, tzinfo=IST))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
