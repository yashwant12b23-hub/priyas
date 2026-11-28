# self_upgrade.py  -  NEW FILE. The old code is never edited by the bot.
# Holds: (1) chat memory, (2) settings.json (bot name, owner info, rules),
#        (3) safe self-upgrade into the /upgrades folder, (4) direct reel keyword rule.
import os, json, re, py_compile, shutil, time
from pathlib import Path

CHAT_FILE = Path("chat_history.json")
SETTINGS_FILE = Path("settings.json")
UPGRADES_DIR = Path("upgrades")
MAX_TURNS = 50

DEFAULT_SETTINGS = {
    "bot_name": "Priya",
    "owner_name": "Yashwanth",
    "reply_language": "Tamil",
    "extra_rules": [],
}

# ---------- (1) MEMORY ----------
def load_history():
    try:
        return json.loads(CHAT_FILE.read_text())[-MAX_TURNS:]
    except Exception:
        return []

def add_turn(role, text):
    h = load_history()
    h.append({"role": role, "text": text[:600]})
    CHAT_FILE.write_text(json.dumps(h[-MAX_TURNS:], ensure_ascii=False))

def history_text():
    h = load_history()
    if not h:
        return ""
    lines = [("Owner: " if t["role"] == "user" else "Bot: ") + t["text"] for t in h]
    return "Recent conversation:\n" + "\n".join(lines) + "\n\n"

# ---------- (2) SETTINGS ----------
def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text()))
    except Exception:
        pass
    return s

def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2))

def apply_setting(key, value):
    """key: bot_name | owner_name | reply_language | add_rule | remove_rule"""
    s = load_settings()
    if key in ("bot_name", "owner_name", "reply_language"):
        s[key] = str(value)[:60]
    elif key == "add_rule":
        s["extra_rules"] = (s.get("extra_rules", []) + [str(value)[:200]])[-20:]
    elif key == "remove_rule":
        s["extra_rules"] = [r for r in s.get("extra_rules", []) if str(value).lower() not in r.lower()]
    else:
        return False
    save_settings(s)
    return True

def persona_prompt():
    s = load_settings()
    rules = "\n".join("- " + r for r in s.get("extra_rules", []))
    return (
        f"Your name is {s['bot_name']}. You assist {s['owner_name']}, a social media creator who runs "
        f"3 accounts (comedy, horror, romance). Reply briefly in {s['reply_language']}.\n"
        + (f"Extra rules from the owner:\n{rules}\n" if rules else "")
    )

# ---------- (4) DIRECT REEL RULE ----------
REEL_WORDS = ["reel", "video", "ரீல்", "வீடியோ", "rill"]
ACTION_WORDS = ["make", "podu", "pottu", "போடு", "செய்", "பண்ணு", "create", "ready", "வேணும்", "venum"]

def looks_like_reel_command(text):
    t = text.lower()
    return any(w in t for w in REEL_WORDS) and any(w in t for w in ACTION_WORDS)

def guess_account(text):
    t = text.lower()
    if "comedy" in t or "காமெடி" in t: return "comedy"
    if "horror" in t or "ஹாரர்" in t: return "horror"
    if "romance" in t or "love" in t or "ரொமான்ஸ்" in t: return "romance"
    return None

# ---------- (3) SAFE SELF-UPGRADE ----------
# The AI writes a NEW python file in /upgrades. We only accept it if it compiles.
# The old priyas_studio.py is never modified.
def write_upgrade(name, code):
    UPGRADES_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^a-z0-9_]", "_", name.lower())[:40] or "upgrade"
    path = UPGRADES_DIR / f"{safe}_{int(time.time())}.py"
    path.write_text(code)
    try:
        py_compile.compile(str(path), doraise=True)
    except Exception as e:
        bad = path.with_suffix(".bad")
        shutil.move(str(path), str(bad))
        return None, f"Code had an error, not activated: {str(e)[:200]}"
    return path, None

def load_upgrades():
    """Run each good upgrade file. A broken one is skipped, never crashes the bot."""
    loaded, failed = [], []
    if not UPGRADES_DIR.exists():
        return loaded, failed
    for p in sorted(UPGRADES_DIR.glob("*.py")):
        try:
            ns = {}
            exec(compile(p.read_text(), str(p), "exec"), ns)
            loaded.append(p.name)
        except Exception as e:
            failed.append(f"{p.name}: {str(e)[:100]}")
            shutil.move(str(p), str(p.with_suffix(".bad")))
    return loaded, failed
  
