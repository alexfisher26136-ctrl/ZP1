import os
import json
import time
import random
import string
import re
import requests
from pathlib import Path
from datetime import datetime, timedelta
from threading import Lock
from flask import Flask, request, render_template_string, redirect, url_for, jsonify, Response, session
from twilio.twiml.voice_response import VoiceResponse, Gather, Dial
from signalwire.rest import Client as signalwire_client
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("HOST_PASSWORD", "Painjab26136") + "zp1secret"

# ====================== ENV ======================
SIGNALWIRE_SPACE = os.getenv("SIGNALWIRE_SPACE", "")
SIGNALWIRE_PROJECT_ID = os.getenv("SIGNALWIRE_PROJECT_ID", "")
SIGNALWIRE_API_TOKEN = os.getenv("SIGNALWIRE_API_TOKEN", "")
SIGNALWIRE_PHONE_NUMBER = os.getenv("SIGNALWIRE_PHONE_NUMBER", "")


BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
HOST_TELEGRAM_ID = int(os.getenv("HOST_TELEGRAM_ID", "0"))
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY", "")
HOST_PASSWORD = os.getenv("HOST_PASSWORD", "Painjab26136")
REQUIRED_CHANNEL = "@cluckpain"
COST_PER_CALL = 0.35

client = None
if SIGNALWIRE_PROJECT_ID and SIGNALWIRE_API_TOKEN:
    client = signalwire_client(SIGNALWIRE_PROJECT_ID, SIGNALWIRE_API_TOKEN, signalwire_space_url=SIGNALWIRE_SPACE)

# ====================== STORAGE ======================
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

SETTINGS_FILE = DATA_DIR / "settings.json"
USERS_FILE = DATA_DIR / "users.json"
CODES_FILE = DATA_DIR / "codes.json"
BLACKLIST_FILE = DATA_DIR / "blacklist.json"
ACTIVITY_FILE = DATA_DIR / "activity.json"
CAMPAIGN_FILE = DATA_DIR / "campaign.json"
HISTORY_FILE = DATA_DIR / "history.json"
DNC_FILE = DATA_DIR / "dnc.json"
CALLBACK_FILE = DATA_DIR / "callback.json"
PAYMENTS_FILE = DATA_DIR / "payments.json"

lock = Lock()

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except:
            return default
    return default

def save_json(path, data):
    path.write_text(json.dumps(data, indent=2))

def load_settings():
    return load_json(SETTINGS_FILE, {
        "transfer": "",
        "message": "Hello. Press 1 to speak with an agent. Press 2 to receive a callback. Press 3 to be removed from our list.",
        "caller_id": SIGNALWIRE_PHONE_NUMBER,
        "digits": {
            "1": {"action": "transfer", "label": "Speak to agent"},
            "2": {"action": "callback", "label": "Call me back later"},
            "3": {"action": "dnc", "label": "Do Not Call"},
            "9": {"action": "repeat", "label": "Repeat menu"}
        },
        "invalid_message": "Sorry, I didn't understand that. Please try again.",
        "auto_continue": True,
        "stealth_mode": True
    })

def save_settings(data):
    save_json(SETTINGS_FILE, data)

settings = load_settings()

def load_users():
    return load_json(USERS_FILE, {})

def save_users(users):
    save_json(USERS_FILE, users)

def get_user(uid):
    users = load_users()
    uid = str(uid)
    if uid not in users:
        users[uid] = {
            "plan": None,
            "plan_expires": None,
            "minutes": 0.0,
            "transfer": "",
            "message": "Hello. Press 1 to speak with an agent.",
            "digits": {
                "1": {"action": "transfer", "label": "Speak to agent"},
                "2": {"action": "callback", "label": "Call me back later"},
                "3": {"action": "dnc", "label": "Do Not Call"},
                "9": {"action": "repeat", "label": "Repeat menu"}
            },
            "invalid_message": "Sorry, I didn't understand that.",
            "auto_continue": True,
            "history": [],
            "dnc": [],
            "callback": [],
            "campaign": {
                "numbers": [],
                "index": 0,
                "active": False,
                "paused": False,
                "busy": False,
                "called": [],
                "active_sids": []
            },
            "awaiting": None
        }
        save_users(users)
    return users[uid]

def update_user(uid, data):
    users = load_users()
    users[str(uid)] = data
    save_users(users)

def is_host(uid):
    return int(uid) == HOST_TELEGRAM_ID

def has_active_plan(uid):
    if is_host(uid):
        return True
    u = get_user(uid)
    if u.get("plan") == "lifetime":
        return True
    if not u.get("plan_expires"):
        return False
    try:
        return datetime.fromisoformat(u["plan_expires"]) > datetime.now()
    except:
        return False

def minutes_left(uid):
    if is_host(uid):
        return 99999.0
    return max(0.0, float(get_user(uid).get("minutes", 0)))

def load_codes():
    return load_json(CODES_FILE, {})

def save_codes(codes):
    save_json(CODES_FILE, codes)

def load_blacklist():
    return load_json(BLACKLIST_FILE, [])

def save_blacklist(bl):
    save_json(BLACKLIST_FILE, bl)

def is_blacklisted(uid):
    return str(uid) in load_blacklist()

def add_activity(text):
    with lock:
        act = load_json(ACTIVITY_FILE, [])
        act.insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "text": text})
        save_json(ACTIVITY_FILE, act[:50])

def clear_activity():
    save_json(ACTIVITY_FILE, [])

def add_history(number, outcome):
    with lock:
        hist = load_json(HISTORY_FILE, [])
        hist.insert(0, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "number": number,
            "outcome": outcome
        })
        save_json(HISTORY_FILE, hist[:100])

def add_dnc(number):
    with lock:
        dnc = load_json(DNC_FILE, [])
        if number and number not in dnc:
            dnc.append(number)
            save_json(DNC_FILE, dnc)

def add_callback(number):
    with lock:
        cb = load_json(CALLBACK_FILE, [])
        if number and number not in cb:
            cb.append(number)
            save_json(CALLBACK_FILE, cb)

def is_dnc(number):
    return number in load_json(DNC_FILE, [])

def sanitize_number(raw):
    if not raw:
        return None
    cleaned = re.sub(r"[^\d+]", "", str(raw).strip())
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if cleaned.startswith("+") and 10 <= len(digits) <= 15:
        return cleaned
    return None

def cancel_active_calls(sids):
    if not client or not sids:
        return
    for sid in list(sids):
        try:
            client.calls(sid).update(status="completed")
        except Exception as e:
            print("Cancel error:", e)

# ====================== NOWPayments ======================
PLANS = {
    "1day":    {"price": 65,   "minutes": 130,  "days": 1,   "name": "1 Day Plan"},
    "2day":    {"price": 100,  "minutes": 200,  "days": 2,   "name": "2 Days Plan"},
    "3day":    {"price": 180,  "minutes": 360,  "days": 3,   "name": "3 Days Plan"},
    "1week":   {"price": 250,  "minutes": 500,  "days": 7,   "name": "1 Week Plan"},
    "lifetime":{"price": 3500, "minutes": 1000, "days": 0,   "name": "Lifetime Plan"}
}

def create_nowpayments_invoice(plan_key, telegram_id):
    if not NOWPAYMENTS_API_KEY:
        return None, "NOWPayments key missing"
    plan = PLANS.get(plan_key)
    if not plan:
        return None, "Invalid plan"
    order_id = f"zp1_{telegram_id}_{plan_key}_{int(time.time())}"
    payload = {
        "price_amount": plan["price"],
        "price_currency": "usd",
        "order_id": order_id,
        "order_description": plan["name"],
        "ipn_callback_url": f"{BASE_URL}/nowpayments_ipn"
    }
    headers = {"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post("https://api.nowpayments.io/v1/invoice", json=payload, headers=headers, timeout=15)
        data = r.json()
        if "invoice_url" in data:
            payments = load_json(PAYMENTS_FILE, {})
            payments[order_id] = {
                "telegram_id": str(telegram_id),
                "plan": plan_key,
                "minutes": plan["minutes"],
                "days": plan["days"],
                "status": "pending"
            }
            save_json(PAYMENTS_FILE, payments)
            return data["invoice_url"], None
        return None, str(data)
    except Exception as e:
        return None, str(e)

# ====================== TELEGRAM ======================
def tg_api(method, payload=None):
    if not TELEGRAM_BOT_TOKEN:
        return None
    try:
        return requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}", json=payload or {}, timeout=10).json()
    except Exception as e:
        print("TG error:", e)
        return None

def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_api("sendMessage", payload)

def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_api("editMessageText", payload)

def answer_callback(callback_query_id, text=None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    return tg_api("answerCallbackQuery", payload)

def check_channel_member(uid):
    if is_host(uid):
        return True
    try:
        r = tg_api("getChatMember", {"chat_id": REQUIRED_CHANNEL, "user_id": int(uid)})
        if r and r.get("ok"):
            return r["result"]["status"] in ["member", "administrator", "creator", "restricted"]
    except:
        pass
    return False

def main_keyboard(uid):
    u = get_user(uid)
    auto = "✅" if u.get("auto_continue", True) else "❌"
    kb = [
        [{"text": "📞 Start Campaign", "callback_data": "tg_start"}],
        [{"text": "📤 Upload Numbers", "callback_data": "tg_upload"}],
        [{"text": "⏸ Pause", "callback_data": "tg_pause"},
         {"text": "▶️ Resume", "callback_data": "tg_resume"},
         {"text": "⏹ Stop", "callback_data": "tg_stop"}],
        [{"text": "⚙️ Settings", "callback_data": "tg_settings"}],
        [{"text": f"💰 Wallet: {minutes_left(uid):.2f}", "callback_data": "tg_wallet"}],
        [{"text": f"Auto-continue {auto}", "callback_data": "tg_auto"}],
        [{"text": "📜 History", "callback_data": "tg_history"}],
        [{"text": "🚫 DNC", "callback_data": "tg_dnc"}, {"text": "📞 Callbacks", "callback_data": "tg_callback"}],
        [{"text": "🛑 PANIC STOP", "callback_data": "tg_panic"}]
    ]
    if is_host(uid):
        kb.append([{"text": "👑 Admin Tools", "callback_data": "host_admin"}])
    return {"inline_keyboard": kb}

def settings_keyboard():
    return {"inline_keyboard": [
        [{"text": "📞 Set Transfer Number", "callback_data": "set_transfer"}],
        [{"text": "💬 Set Message", "callback_data": "set_message"}],
        [{"text": "« Back to Main", "callback_data": "tg_main"}]
    ]}

def host_admin_keyboard():
    return {"inline_keyboard": [
        [{"text": "🎟 Redeem Codes", "callback_data": "host_codes"}],
        [{"text": "🚫 Blacklist", "callback_data": "host_blacklist"}],
        [{"text": "💰 Gift Minutes", "callback_data": "host_gift"}],
        [{"text": "👥 Users", "callback_data": "host_users"}],
        [{"text": "📊 Stats", "callback_data": "host_stats"}],
        [{"text": "🌐 Web Panel", "url": BASE_URL}],
        [{"text": "« Back", "callback_data": "tg_main"}]
    ]}

def channel_keyboard():
    return {"inline_keyboard": [
        [{"text": "📢 Join @cluckpain", "url": "https://t.me/cluckpain"}],
        [{"text": "✅ I Joined – Check", "callback_data": "check_channel"}]
    ]}

def dial_next_for_user(uid):
    with lock:
        u = get_user(uid)
        camp = u.get("campaign", {})

        if not camp.get("active") or camp.get("paused") or camp.get("busy"):
            return

        if not has_active_plan(uid) and not is_host(uid):
            camp["active"] = False
            u["campaign"] = camp
            update_user(uid, u)
            send_message(uid, "⚠️ Plan expired.")
            return

        if minutes_left(uid) < COST_PER_CALL and not is_host(uid):
            camp["active"] = False
            u["campaign"] = camp
            update_user(uid, u)
            send_message(uid, "⚠️ Balance too low.")
            return

        number = None
        while camp["index"] < len(camp.get("numbers", [])):
            candidate = camp["numbers"][camp["index"]]
            camp["index"] += 1
            if candidate in camp.get("called", []):
                continue
            if is_dnc(candidate) or candidate in u.get("dnc", []):
                continue
            number = candidate
            break

        if not number:
            camp["active"] = False
            camp["busy"] = False
            u["campaign"] = camp
            update_user(uid, u)
            send_message(uid, "✅ Campaign finished.")
            return

        camp["busy"] = True
        camp.setdefault("called", []).append(number)
        u["campaign"] = camp
        update_user(uid, u)

    if settings.get("stealth_mode", True):
        time.sleep(random.uniform(1.8, 4.0))

    try:
        call = client.calls.create(
            to=number,
            from_=SIGNALWIRE_PHONE_NUMBER,
            url=f"{BASE_URL}/voice_user?uid={uid}",
            status_callback=f"{BASE_URL}/status_user?uid={uid}",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
            timeout=25,
            machine_detection="DetectMessageEnd",
            machine_detection_timeout=4
        )
        with lock:
            u = get_user(uid)
            u["campaign"].setdefault("active_sids", []).append(call.sid)
            update_user(uid, u)

        if not is_host(uid):
            u = get_user(uid)
            u["minutes"] = max(0.0, float(u.get("minutes", 0)) - COST_PER_CALL)
            update_user(uid, u)

        send_message(uid, f"📞 Dialing `{number}`...")

    except Exception as e:
        send_message(uid, f"❌ Failed: {e}")
        with lock:
            u = get_user(uid)
            u["campaign"]["busy"] = False
            update_user(uid, u)
        if u.get("auto_continue", True):
            time.sleep(2)
            dial_next_for_user(uid)

def handle_start(chat_id, uid):
    if is_blacklisted(uid):
        send_message(chat_id, "⛔ Blacklisted.")
        return
    if not check_channel_member(uid):
        send_message(chat_id, f"🔒 Join {REQUIRED_CHANNEL}", channel_keyboard())
        return
    if not has_active_plan(uid) and not is_host(uid):
        kb = {"inline_keyboard": [
            [{"text": "💳 Buy Plan", "callback_data": "buy_plan"}],
            [{"text": "🎟 Redeem Code", "callback_data": "redeem_code"}]
        ]}
        send_message(chat_id, "🔒 No active plan", kb)
        return

    u = get_user(uid)
    nums = len(u.get("campaign", {}).get("numbers", []))
    text = f"✅ *ZP1 Dialer*\n\nPlan: `{u.get('plan') or 'Host'}`\nMinutes: *{minutes_left(uid):.2f}*\nNumbers loaded: *{nums}*"
    send_message(chat_id, text, main_keyboard(uid))

def handle_callback(chat_id, message_id, uid, data, callback_id):
    answer_callback(callback_id)
    if is_blacklisted(uid):
        return
    if data == "check_channel":
        if check_channel_member(uid):
            handle_start(chat_id, uid)
        else:
            edit_message(chat_id, message_id, "❌ Not detected yet.", channel_keyboard())
        return
    if not is_host(uid) and not check_channel_member(uid):
        edit_message(chat_id, message_id, f"🔒 Join {REQUIRED_CHANNEL}", channel_keyboard())
        return

    u = get_user(uid)

    if data == "tg_main":
        handle_start(chat_id, uid)
        return

    if data == "tg_start":
        numbers = u.get("campaign", {}).get("numbers", [])
        if not numbers:
            u["awaiting"] = "upload_then_start"
            update_user(uid, u)
            edit_message(chat_id, message_id, "📞 No numbers yet. Send the list now (will start immediately).",
                         {"inline_keyboard": [[{"text": "« Cancel", "callback_data": "tg_main"}]]})
        else:
            u["campaign"]["active"] = True
            u["campaign"]["paused"] = False
            u["campaign"]["busy"] = False
            u["campaign"]["index"] = 0
            u["campaign"]["called"] = []
            u["campaign"]["active_sids"] = []
            update_user(uid, u)
            edit_message(chat_id, message_id, f"🚀 Starting {len(numbers)} numbers...", main_keyboard(uid))
            dial_next_for_user(uid)
        return

    if data == "tg_upload":
        u["awaiting"] = "upload_only"
        update_user(uid, u)
        edit_message(chat_id, message_id, "📤 Send numbers now (will NOT start).",
                     {"inline_keyboard": [[{"text": "« Cancel", "callback_data": "tg_main"}]]})
        return

    if data == "tg_pause":
        u["campaign"]["paused"] = True
        update_user(uid, u)
        edit_message(chat_id, message_id, "⏸ PAUSED", main_keyboard(uid))
        return

    if data == "tg_resume":
        u["campaign"]["paused"] = False
        update_user(uid, u)
        edit_message(chat_id, message_id, "▶️ RESUMED", main_keyboard(uid))
        if u["campaign"].get("active") and not u["campaign"].get("busy"):
            dial_next_for_user(uid)
        return

    if data in ["tg_stop", "tg_panic"]:
        sids = u.get("campaign", {}).get("active_sids", [])
        cancel_active_calls(sids)
        u["campaign"] = {"numbers": [], "index": 0, "active": False, "paused": False, "busy": False, "called": [], "active_sids": []}
        u["awaiting"] = None
        update_user(uid, u)
        edit_message(chat_id, message_id, "🛑 STOPPED – calls cancelled", main_keyboard(uid))
        return

    if data == "tg_auto":
        u["auto_continue"] = not u.get("auto_continue", True)
        update_user(uid, u)
        edit_message(chat_id, message_id, f"Auto-continue: *{'ON' if u['auto_continue'] else 'OFF'}*", main_keyboard(uid))
        return

    if data == "tg_wallet":
        mins = minutes_left(uid)
        text = f"💰 Wallet\n\n*{mins:.2f}* min left\nCost/call: {COST_PER_CALL}"
        kb = {"inline_keyboard": [[{"text": "➕ Top Up", "callback_data": "buy_plan"}], [{"text": "« Back", "callback_data": "tg_main"}]]}
        edit_message(chat_id, message_id, text, kb)
        return

    if data == "tg_settings":
        text = f"⚙️ Settings\n\nTransfer: `{u.get('transfer') or 'Not set'}`\nMessage: `{u.get('message')[:40]}...`"
        edit_message(chat_id, message_id, text, settings_keyboard())
        return

    if data == "set_transfer":
        u["awaiting"] = "transfer"
        update_user(uid, u)
        edit_message(chat_id, message_id, "📞 Send transfer number:",
                     {"inline_keyboard": [[{"text": "« Back", "callback_data": "tg_settings"}]]})
        return

    if data == "set_message":
        u["awaiting"] = "message"
        update_user(uid, u)
        edit_message(chat_id, message_id, "💬 Send new message:",
                     {"inline_keyboard": [[{"text": "« Back", "callback_data": "tg_settings"}]]})
        return

    if data == "tg_history":
        hist = u.get("history", [])[-8:]
        text = "📜 History\n\n" + ("\n".join([f"{h.get('time')} → {h.get('number')} ({h.get('outcome')})" for h in hist]) or "Empty")
        edit_message(chat_id, message_id, text, {"inline_keyboard": [[{"text": "« Back", "callback_data": "tg_main"}]]})
        return

    if data == "tg_dnc":
        text = "🚫 DNC\n\n" + ("\n".join(u.get("dnc", [])) or "Empty")
        edit_message(chat_id, message_id, text, {"inline_keyboard": [[{"text": "« Back", "callback_data": "tg_main"}]]})
        return

    if data == "tg_callback":
        text = "📞 Callbacks\n\n" + ("\n".join(u.get("callback", [])) or "Empty")
        edit_message(chat_id, message_id, text, {"inline_keyboard": [[{"text": "« Back", "callback_data": "tg_main"}]]})
        return

    if data == "buy_plan":
        kb = {"inline_keyboard": [
            [{"text": "1 Day – $65", "callback_data": "pay_1day"}],
            [{"text": "2 Days – $100", "callback_data": "pay_2day"}],
            [{"text": "3 Days – $180", "callback_data": "pay_3day"}],
            [{"text": "1 Week – $250", "callback_data": "pay_1week"}],
            [{"text": "Lifetime – $3500", "callback_data": "pay_lifetime"}],
            [{"text": "« Back", "callback_data": "tg_main"}]
        ]}
        edit_message(chat_id, message_id, "💳 Choose Plan", kb)
        return

    if data.startswith("pay_"):
        plan_key = data.replace("pay_", "")
        url, err = create_nowpayments_invoice(plan_key, uid)
        if url:
            kb = {"inline_keyboard": [[{"text": "💳 Pay Now", "url": url}], [{"text": "« Back", "callback_data": "buy_plan"}]]}
            edit_message(chat_id, message_id, "✅ Invoice created. Minutes added after payment.", kb)
        else:
            edit_message(chat_id, message_id, f"❌ {err}", {"inline_keyboard": [[{"text": "« Back", "callback_data": "buy_plan"}]]})
        return

    if data == "redeem_code":
        u["awaiting"] = "redeem"
        update_user(uid, u)
        edit_message(chat_id, message_id, "🎟 Send code:",
                     {"inline_keyboard": [[{"text": "« Cancel", "callback_data": "tg_main"}]]})
        return

    if is_host(uid):
        if data == "host_admin":
            edit_message(chat_id, message_id, "👑 Admin Tools", host_admin_keyboard())
            return
        if data == "host_codes":
            kb = {"inline_keyboard": [
                [{"text": "1 Day", "callback_data": "gen_1day"}, {"text": "2 Days", "callback_data": "gen_2day"}],
                [{"text": "3 Days", "callback_data": "gen_3day"}, {"text": "1 Week", "callback_data": "gen_1week"}],
                [{"text": "Lifetime", "callback_data": "gen_lifetime"}],
                [{"text": "📋 Recent", "callback_data": "list_codes"}],
                [{"text": "« Back", "callback_data": "host_admin"}]
            ]}
            edit_message(chat_id, message_id, "🎟 Generate", kb)
            return
        if data.startswith("gen_"):
            plan = data.replace("gen_", "")
            code = "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
            p = PLANS.get(plan, PLANS["1day"])
            codes = load_codes()
            codes[code] = {"plan": plan, "minutes": p["minutes"], "days": p["days"]}
            save_codes(codes)
            edit_message(chat_id, message_id, f"✅ `{code}`\n{plan} | {p['minutes']} min",
                         {"inline_keyboard": [[{"text": "« Back", "callback_data": "host_codes"}]]})
            return
        if data == "list_codes":
            codes = load_codes()
            text = "📋\n" + "\n".join([f"`{c}` → {i.get('plan')}" for c, i in list(codes.items())[-10:]]) or "None"
            edit_message(chat_id, message_id, text, {"inline_keyboard": [[{"text": "« Back", "callback_data": "host_codes"}]]})
            return
        if data == "host_stats":
            users = load_users()
            edit_message(chat_id, message_id, f"📊 Users: {len(users)}\nWith plan: {sum(1 for x in users.values() if x.get('plan'))}",
                         {"inline_keyboard": [[{"text": "« Back", "callback_data": "host_admin"}]]})
            return
        if data == "host_blacklist":
            bl = load_blacklist()
            text = "🚫\n" + ("\n".join(bl) or "Empty")
            edit_message(chat_id, message_id, text, {"inline_keyboard": [[{"text": "« Back", "callback_data": "host_admin"}]]})
            return
        if data == "host_gift":
            edit_message(chat_id, message_id, "💰 gift USER_ID 50",
                         {"inline_keyboard": [[{"text": "« Back", "callback_data": "host_admin"}]]})
            return
        if data == "host_users":
            users = load_users()
            text = "👥\n" + "\n".join([f"`{uid_}` → {x.get('plan')} | {x.get('minutes',0):.1f}m" for uid_, x in list(users.items())[:12]])
            edit_message(chat_id, message_id, text, {"inline_keyboard": [[{"text": "« Back", "callback_data": "host_admin"}]]})
            return

    edit_message(chat_id, message_id, "Done.", {"inline_keyboard": [[{"text": "« Back", "callback_data": "tg_main"}]]})

@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True)

    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        uid = msg["from"]["id"]
        text = msg.get("text", "").strip()

        if text in ["/start", "/panic"]:
            if text == "/panic":
                u = get_user(uid)
                cancel_active_calls(u.get("campaign", {}).get("active_sids", []))
                u["campaign"] = {"numbers": [], "index": 0, "active": False, "paused": False, "busy": False, "called": [], "active_sids": []}
                update_user(uid, u)
                send_message(chat_id, "🛑 PANIC – calls cancelled")
            handle_start(chat_id, uid)
            return "OK"

        u = get_user(uid)
        awaiting = u.get("awaiting")

        codes = load_codes()
        if text.upper() in codes:
            info = codes.pop(text.upper())
            save_codes(codes)
            u["plan"] = info["plan"]
            u["minutes"] = float(u.get("minutes", 0)) + float(info["minutes"])
            if info["days"] > 0:
                u["plan_expires"] = (datetime.now() + timedelta(days=info["days"])).isoformat()
            else:
                u["plan"] = "lifetime"
                u["plan_expires"] = None
            u["awaiting"] = None
            update_user(uid, u)
            send_message(chat_id, f"✅ +{info['minutes']} min")
            handle_start(chat_id, uid)
            return "OK"

        if is_host(uid):
            if text.lower().startswith("blacklist "):
                target = text.split(" ", 1)[1].strip()
                bl = load_blacklist()
                if target not in bl:
                    bl.append(target)
                    save_blacklist(bl)
                send_message(chat_id, f"✅ Blacklisted")
                return "OK"
            if text.lower().startswith("unblacklist "):
                target = text.split(" ", 1)[1].strip()
                bl = load_blacklist()
                if target in bl:
                    bl.remove(target)
                    save_blacklist(bl)
                send_message(chat_id, "✅ Removed")
                return "OK"
            if text.lower().startswith("gift "):
                parts = text.split()
                if len(parts) >= 3:
                    try:
                        target, amount = parts[1], float(parts[2])
                        tu = get_user(target)
                        tu["minutes"] = float(tu.get("minutes", 0)) + amount
                        if not tu.get("plan"):
                            tu["plan"] = "gifted"
                            tu["plan_expires"] = (datetime.now() + timedelta(days=7)).isoformat()
                        update_user(target, tu)
                        send_message(chat_id, f"✅ Gifted {amount}")
                    except:
                        pass
                return "OK"

        if awaiting == "transfer":
            cleaned = sanitize_number(text)
            if cleaned:
                u["transfer"] = cleaned
                u["awaiting"] = None
                update_user(uid, u)
                send_message(chat_id, f"✅ Transfer: `{cleaned}`",
                             {"inline_keyboard": [[{"text": "« Back", "callback_data": "tg_settings"}]]})
            return "OK"

        if awaiting == "message":
            if len(text) > 5:
                u["message"] = text
                u["awaiting"] = None
                update_user(uid, u)
                send_message(chat_id, "✅ Message saved",
                             {"inline_keyboard": [[{"text": "« Back", "callback_data": "tg_settings"}]]})
            return "OK"

        if awaiting in ["upload_only", "upload_then_start"]:
            raw_list = [n.strip() for n in text.replace(",", "\n").splitlines() if n.strip()]
            numbers = []
            for raw in raw_list:
                cleaned = sanitize_number(raw)
                if cleaned:
                    numbers.append(cleaned)
            numbers = list(dict.fromkeys(numbers))

            if numbers:
                start_now = (awaiting == "upload_then_start")
                u["campaign"] = {
                    "numbers": numbers,
                    "index": 0,
                    "active": start_now,
                    "paused": False,
                    "busy": False,
                    "called": [],
                    "active_sids": []
                }
                u["awaiting"] = None
                update_user(uid, u)
                if start_now:
                    send_message(chat_id, f"✅ {len(numbers)} numbers – dialing started", main_keyboard(uid))
                    dial_next_for_user(uid)
                else:
                    send_message(chat_id, f"✅ {len(numbers)} numbers loaded. Press Start Campaign.", main_keyboard(uid))
            else:
                send_message(chat_id, "❌ No valid numbers")
            return "OK"

    if "callback_query" in data:
        cq = data["callback_query"]
        handle_callback(cq["message"]["chat"]["id"], cq["message"]["message_id"], cq["from"]["id"], cq["data"], cq["id"])

    return "OK"

# ====================== USER CALL HANDLERS ======================
@app.route("/voice_user", methods=["POST"])
def voice_user():
    uid = request.args.get("uid")
    u = get_user(uid) if uid else {}
    message = u.get("message") or settings.get("message")
    answered_by = request.values.get("AnsweredBy", "")
    if answered_by and "machine" in answered_by:
        resp = VoiceResponse()
        resp.hangup()
        return str(resp)
    resp = VoiceResponse()
    gather = Gather(num_digits=1, action=f"/gather_user?uid={uid}", method="POST", timeout=10)
    gather.say(message)
    resp.append(gather)
    resp.say(u.get("invalid_message") or settings.get("invalid_message"))
    resp.redirect(f"/voice_user?uid={uid}")
    return str(resp)

@app.route("/gather_user", methods=["POST"])
def gather_user():
    uid = request.args.get("uid")
    u = get_user(uid) if uid else {}
    digit = request.values.get("Digits", "")
    customer_number = request.values.get("To") or request.values.get("From") or ""

    resp = VoiceResponse()
    action_data = u.get("digits", {}).get(digit) or settings.get("digits", {}).get(digit)

    if action_data:
        action = action_data.get("action")
        if action == "transfer":
            transfer = u.get("transfer") or settings.get("transfer")
            if transfer:
                dial = Dial(callerId=customer_number)
                dial.number(transfer)
                resp.append(dial)
            else:
                resp.say("No transfer number set.")
                resp.hangup()
        elif action == "callback":
            if customer_number and customer_number not in u.get("callback", []):
                u.setdefault("callback", []).append(customer_number)
                update_user(uid, u)
            resp.say("We will call you back. Goodbye.")
            resp.hangup()
        elif action == "dnc":
            if customer_number and customer_number not in u.get("dnc", []):
                u.setdefault("dnc", []).append(customer_number)
                update_user(uid, u)
            add_dnc(customer_number)
            resp.say("Removed from list. Goodbye.")
            resp.hangup()
        elif action == "repeat":
            resp.redirect(f"/voice_user?uid={uid}")
        else:
            resp.say("Goodbye.")
            resp.hangup()
    else:
        resp.say(u.get("invalid_message") or "Sorry.")
        resp.redirect(f"/voice_user?uid={uid}")
    return str(resp)

@app.route("/status_user", methods=["POST"])
def status_user():
    uid = request.args.get("uid")
    call_sid = request.values.get("CallSid")
    call_status = request.values.get("CallStatus")
    answered_by = request.values.get("AnsweredBy", "")
    to_number = request.values.get("To", "")

    if call_status in ["completed", "busy", "no-answer", "failed", "canceled"]:
        outcome = "voicemail/AMD" if "machine" in answered_by else call_status
        if uid:
            u = get_user(uid)
            u.setdefault("history", []).insert(0, {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "number": to_number,
                "outcome": outcome
            })
            u["history"] = u["history"][:50]
            u["campaign"]["busy"] = False
            if call_sid in u["campaign"].get("active_sids", []):
                u["campaign"]["active_sids"].remove(call_sid)
            update_user(uid, u)

            if u.get("auto_continue", True) and u["campaign"].get("active") and not u["campaign"].get("paused"):
                time.sleep(1.5)
                dial_next_for_user(uid)
    return "OK", 200

@app.route("/nowpayments_ipn", methods=["POST"])
def nowpayments_ipn():
    data = request.get_json(force=True) or {}
    order_id = data.get("order_id")
    status = data.get("payment_status")
    if not order_id:
        return "OK", 200
    payments = load_json(PAYMENTS_FILE, {})
    if order_id not in payments or payments[order_id].get("status") == "finished":
        return "OK", 200
    if status in ["finished", "confirmed"]:
        p = payments[order_id]
        uid = p["telegram_id"]
        u = get_user(uid)
        u["plan"] = p["plan"]
        u["minutes"] = float(u.get("minutes", 0)) + float(p["minutes"])
        if p["days"] > 0:
            u["plan_expires"] = (datetime.now() + timedelta(days=p["days"])).isoformat()
        else:
            u["plan"] = "lifetime"
            u["plan_expires"] = None
        update_user(uid, u)
        payments[order_id]["status"] = "finished"
        save_json(PAYMENTS_FILE, payments)
        send_message(uid, f"✅ Payment confirmed! +{p['minutes']} minutes")
    return "OK", 200

# ====================== HOST WEB PANEL ======================
LOGIN_HTML = """
<!DOCTYPE html><html><head><title>Host Login</title>
<style>body{font-family:system-ui;background:#0f172a;color:white;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
.card{background:#1e293b;padding:40px;border-radius:16px;width:320px;text-align:center}
input{width:100%;padding:12px;margin:12px 0;border-radius:8px;border:none;background:#334155;color:white}
button{width:100%;padding:12px;background:#3b82f6;color:white;border:none;border-radius:8px;font-weight:bold;cursor:pointer}
</style></head><body>
<div class="card"><h2>ZP1 Host Panel</h2>
<form method="POST"><input type="password" name="password" placeholder="Password" required>
<button type="submit">Login</button></form>
{% if error %}<p style="color:#f87171">{{ error }}</p>{% endif %}
</div></body></html>
"""

PANEL_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>P1 • Outbound IVR</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f4f4f5;color:#18181b;min-height:100vh;padding:30px 16px}
.window{max-width:980px;margin:0 auto;background:white;border-radius:16px;box-shadow:0 10px 40px rgba(0,0,0,0.08);border:1px solid #e4e4e7;overflow:hidden}
.titlebar{background:#fafafa;padding:14px 20px;border-bottom:1px solid #e4e4e7;display:flex;align-items:center;gap:8px}
.dot{width:12px;height:12px;border-radius:50%}
.dot.red{background:#ff5f57}.dot.yellow{background:#febc2e}.dot.green{background:#28c840}
.title{margin-left:12px;font-weight:600;font-size:14px;color:#52525b}
.content{padding:24px}
h1{font-size:18px;margin-bottom:20px}
label{display:block;font-size:12px;font-weight:600;color:#71717a;margin-bottom:6px;text-transform:uppercase}
input,textarea,select{width:100%;padding:11px 14px;border:1px solid #e4e4e7;border-radius:10px;font-size:15px;background:#fafafa;margin-bottom:12px}
.btn{display:inline-flex;align-items:center;justify-content:center;padding:11px 18px;border-radius:10px;font-size:14px;font-weight:600;border:none;cursor:pointer;text-decoration:none}
.btn-green{background:#22c55e;color:white}.btn-blue{background:#3b82f6;color:white}
.btn-red{background:#ef4444;color:white}.btn-yellow{background:#eab308;color:#18181b}
.btn-gray{background:#e4e4e7;color:#18181b}.btn-full{width:100%}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.msg{background:#dcfce7;color:#166534;padding:12px 16px;border-radius:10px;margin-bottom:18px;text-align:center}
.digit-row{display:grid;grid-template-columns:50px 1fr 1fr;gap:8px;align-items:center;margin-bottom:8px}
.section-title{font-size:14px;font-weight:600;margin:28px 0 12px;color:#3f3f46}
.live-box,.history-box{background:#fafafa;border:1px solid #e4e4e7;border-radius:12px;padding:14px;height:200px;overflow-y:auto;font-size:13px}
.live-item,.hist-item{padding:6px 0;border-bottom:1px solid #f4f4f5}
.live-time,.hist-time{color:#a1a1aa;margin-right:8px}
.controls{display:flex;gap:10px;margin:16px 0;flex-wrap:wrap}
.controls form{flex:1;min-width:120px}.controls .btn{width:100%}
.export-row{display:flex;gap:10px;margin-top:12px}.export-row a{flex:1;text-align:center}
</style>
</head>
<body>
<div class="window">
<div class="titlebar">
<div class="dot red"></div><div class="dot yellow"></div><div class="dot green"></div>
<div class="title">P1 • Outbound IVR (Host Panel)</div>
</div>
<div class="content">
{% if message %}<div class="msg">{{ message }}</div>{% endif %}
<h1>P1 - OUTBOUND IVR</h1>
<div class="section-title">Campaign Controls</div>
<div class="controls">
<form method="POST" action="/pause_campaign"><button class="btn btn-yellow" type="submit">Pause</button></form>
<form method="POST" action="/resume_campaign"><button class="btn btn-green" type="submit">Resume</button></form>
<form method="POST" action="/stop_campaign"><button class="btn btn-red" type="submit">Stop</button></form>
</div>
<form method="POST" action="/save_settings">
<div class="row">
<div><label>Transfer Number</label>
<input type="text" name="transfer" value="{{ settings.transfer }}" placeholder="+1XXXXXXXXXX"></div>
<div><label>Caller ID (Host only)</label>
<input type="text" value="{{ settings.caller_id }}" readonly></div>
</div>
<label>Main Menu Prompt</label>
<textarea name="message">{{ settings.message }}</textarea>
<label>Invalid Option Message</label>
<input type="text" name="invalid_message" value="{{ settings.invalid_message }}">
<div class="section-title">Keypad Actions</div>
{% for d in ["1","2","3","4","5","6","7","8","9"] %}
<div class="digit-row">
<strong>{{ d }}</strong>
<select name="digit_{{ d }}_action">
<option value="none" {% if settings.digits.get(d, {}).get('action') == 'none' %}selected{% endif %}>Not used</option>
<option value="transfer" {% if settings.digits.get(d, {}).get('action') == 'transfer' %}selected{% endif %}>Transfer</option>
<option value="callback" {% if settings.digits.get(d, {}).get('action') == 'callback' %}selected{% endif %}>Callback later</option>
<option value="dnc" {% if settings.digits.get(d, {}).get('action') == 'dnc' %}selected{% endif %}>Do Not Call</option>
<option value="repeat" {% if settings.digits.get(d, {}).get('action') == 'repeat' %}selected{% endif %}>Repeat menu</option>
<option value="hangup" {% if settings.digits.get(d, {}).get('action') == 'hangup' %}selected{% endif %}>Hang up</option>
</select>
<input type="text" name="digit_{{ d }}_label" value="{{ settings.digits.get(d, {}).get('label', '') }}" placeholder="Label">
</div>
{% endfor %}
<label style="margin-top:16px;">
<input type="checkbox" name="auto_continue" value="1" {% if settings.auto_continue %}checked{% endif %}>
Auto-continue to next number after no-answer / voicemail / hangup
</label>
<button type="submit" class="btn btn-blue btn-full" style="margin-top:18px;">Save Settings</button>
</form>
<div style="margin-top:36px;border-top:1px solid #e4e4e7;padding-top:24px;">
<form method="POST" action="/start_campaign">
<label>Numbers to dial (one per line)</label>
<textarea name="numbers" placeholder="+18067816048&#10;+18059960823" style="min-height:100px;"></textarea>
<button type="submit" class="btn btn-green btn-full" style="margin-top:14px;">Start Progressive Dialing</button>
</form>
</div>
<div class="section-title">Live Activity</div>
<form method="POST" action="/clear_activity" style="margin-bottom:8px;">
<button type="submit" class="btn btn-gray">Clear Live Activity</button>
</form>
<div class="live-box" id="live">
{% for item in activity %}
<div class="live-item"><span class="live-time">{{ item.time }}</span>{{ item.text }}</div>
{% else %}
<div style="color:#a1a1aa;">Waiting for activity...</div>
{% endfor %}
</div>
<div class="section-title">Call History (last 100)</div>
<div class="history-box">
{% for h in history %}
<div class="hist-item"><span class="hist-time">{{ h.time }}</span><strong>{{ h.number }}</strong> → {{ h.outcome }}</div>
{% else %}
<div style="color:#a1a1aa;">No calls yet</div>
{% endfor %}
</div>
<div class="section-title">Export Lists</div>
<div class="export-row">
<a href="/export_dnc" class="btn btn-gray">Export DNC List</a>
<a href="/export_callback" class="btn btn-gray">Export Callback List</a>
</div>
</div>
</div>
<script>
setInterval(() => {
fetch('/activity').then(r => r.json()).then(data => {
const box = document.getElementById('live');
if (!data.length) { box.innerHTML = '<div style="color:#a1a1aa;">Waiting for activity...</div>'; return; }
box.innerHTML = data.map(i => `<div class="live-item"><span class="live-time">${i.time}</span>${i.text}</div>`).join('');
});
}, 4000);
</script>
</body>
</html>
"""

@app.route("/", methods=["GET", "POST"])
def panel():
    if request.method == "POST":
        if request.form.get("password") == HOST_PASSWORD:
            session["host_logged_in"] = True
            return redirect(url_for("panel"))
        return render_template_string(LOGIN_HTML, error="Wrong password")
    if not session.get("host_logged_in"):
        return render_template_string(LOGIN_HTML, error=None)
    msg = request.args.get("msg")
    activity = load_json(ACTIVITY_FILE, [])
    history = load_json(HISTORY_FILE, [])
    return render_template_string(PANEL_HTML, settings=settings, message=msg, activity=activity, history=history)

@app.route("/activity")
def activity_api():
    return jsonify(load_json(ACTIVITY_FILE, []))

@app.route("/clear_activity", methods=["POST"])
def clear_activity_route():
    if not session.get("host_logged_in"):
        return redirect(url_for("panel"))
    clear_activity()
    return redirect(url_for("panel", msg="Activity cleared"))

@app.route("/save_settings", methods=["POST"])
def save_settings_route():
    if not session.get("host_logged_in"):
        return redirect(url_for("panel"))
    global settings
    settings["transfer"] = request.form.get("transfer", "").strip()
    settings["message"] = request.form.get("message", "").strip()
    settings["invalid_message"] = request.form.get("invalid_message", "").strip()
    settings["caller_id"] = SIGNALWIRE_PHONE_NUMBER
    settings["auto_continue"] = True if request.form.get("auto_continue") else False
    digits = {}
    for d in ["1","2","3","4","5","6","7","8","9"]:
        action = request.form.get(f"digit_{d}_action", "none")
        label = request.form.get(f"digit_{d}_label", "").strip()
        if action != "none":
            digits[d] = {"action": action, "label": label}
    settings["digits"] = digits
    save_settings(settings)
    add_activity("Settings updated")
    return redirect(url_for("panel", msg="Settings saved"))

def load_campaign():
    return load_json(CAMPAIGN_FILE, {
        "numbers": [], "index": 0, "active": False, "paused": False,
        "busy": False, "current_sid": None, "called": [], "active_sids": []
    })

def save_campaign(data):
    save_json(CAMPAIGN_FILE, data)

def dial_next():
    with lock:
        camp = load_campaign()
        if not camp["active"] or camp["paused"] or camp["busy"]:
            return
        number = None
        while camp["index"] < len(camp["numbers"]):
            candidate = camp["numbers"][camp["index"]]
            camp["index"] += 1
            if candidate in camp.get("called", []):
                continue
            if is_dnc(candidate):
                continue
            number = candidate
            break
        if not number:
            camp["active"] = False
            camp["busy"] = False
            save_campaign(camp)
            add_activity("Campaign finished")
            return
        camp["busy"] = True
        camp.setdefault("called", []).append(number)
        save_campaign(camp)

    if settings.get("stealth_mode", True):
        time.sleep(random.uniform(1.8, 4.1))

    try:
        call = client.calls.create(
            to=number,
            from_=SIGNALWIRE_PHONE_NUMBER,
            url=f"{BASE_URL}/voice",
            status_callback=f"{BASE_URL}/status",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
            timeout=25,
            machine_detection="DetectMessageEnd",
            machine_detection_timeout=4
        )
        with lock:
            camp = load_campaign()
            camp["current_sid"] = call.sid
            camp.setdefault("active_sids", []).append(call.sid)
            save_campaign(camp)
        add_activity(f"Dialing {number}")
    except Exception as e:
        add_activity(f"Failed {number}: {e}")
        with lock:
            camp = load_campaign()
            camp["busy"] = False
            save_campaign(camp)
        if settings.get("auto_continue"):
            time.sleep(2)
            dial_next()

@app.route("/start_campaign", methods=["POST"])
def start_campaign():
    if not session.get("host_logged_in"):
        return redirect(url_for("panel"))
    raw = request.form.get("numbers", "")
    numbers = []
    for n in raw.replace(",", "\n").splitlines():
        cleaned = sanitize_number(n.strip())
        if cleaned:
            numbers.append(cleaned)
    numbers = list(dict.fromkeys(numbers))
    if not numbers:
        return redirect(url_for("panel", msg="No valid numbers"))
    with lock:
        save_campaign({
            "numbers": numbers,
            "index": 0,
            "active": True,
            "paused": False,
            "busy": False,
            "current_sid": None,
            "called": [],
            "active_sids": []
        })
    add_activity(f"Campaign started ({len(numbers)} numbers)")
    dial_next()
    return redirect(url_for("panel", msg=f"Started {len(numbers)} numbers"))

@app.route("/pause_campaign", methods=["POST"])
def pause_campaign():
    if not session.get("host_logged_in"):
        return redirect(url_for("panel"))
    with lock:
        camp = load_campaign()
        camp["paused"] = True
        save_campaign(camp)
    add_activity("PAUSED")
    return redirect(url_for("panel", msg="Paused"))

@app.route("/resume_campaign", methods=["POST"])
def resume_campaign():
    if not session.get("host_logged_in"):
        return redirect(url_for("panel"))
    with lock:
        camp = load_campaign()
        camp["paused"] = False
        save_campaign(camp)
    add_activity("RESUMED")
    if settings.get("auto_continue"):
        time.sleep(0.5)
        dial_next()
    return redirect(url_for("panel", msg="Resumed"))

@app.route("/stop_campaign", methods=["POST"])
def stop_campaign():
    if not session.get("host_logged_in"):
        return redirect(url_for("panel"))
    with lock:
        camp = load_campaign()
        sids = camp.get("active_sids", [])
        camp["active"] = False
        camp["paused"] = False
        camp["busy"] = False
        camp["active_sids"] = []
        save_campaign(camp)
    cancel_active_calls(sids)
    add_activity("STOPPED – calls cancelled")
    return redirect(url_for("panel", msg="Stopped"))

@app.route("/status", methods=["POST"])
def status_callback():
    call_sid = request.values.get("CallSid")
    call_status = request.values.get("CallStatus")
    answered_by = request.values.get("AnsweredBy", "")
    to_number = request.values.get("To", "")
    if call_status in ["completed", "busy", "no-answer", "failed", "canceled"]:
        outcome = "voicemail/AMD" if "machine" in answered_by else call_status
        add_activity(f"{outcome} on {to_number}")
        add_history(to_number, outcome)
        with lock:
            camp = load_campaign()
            camp["busy"] = False
            camp["current_sid"] = None
            if call_sid in camp.get("active_sids", []):
                camp["active_sids"].remove(call_sid)
            save_campaign(camp)
        if settings.get("auto_continue"):
            time.sleep(1.5)
            dial_next()
    return "OK", 200

@app.route("/voice", methods=["POST"])
def voice():
    answered_by = request.values.get("AnsweredBy", "")
    if answered_by and "machine" in answered_by:
        resp = VoiceResponse()
        resp.hangup()
        return str(resp)
    resp = VoiceResponse()
    gather = Gather(num_digits=1, action="/gather", method="POST", timeout=10)
    gather.say(settings.get("message"))
    resp.append(gather)
    resp.say(settings.get("invalid_message"))
    resp.redirect("/voice")
    return str(resp)

@app.route("/gather", methods=["POST"])
def gather():
    digit = request.values.get("Digits", "")
    customer_number = request.values.get("To") or request.values.get("From") or ""
    resp = VoiceResponse()
    action_data = settings.get("digits", {}).get(digit)
    if action_data:
        action = action_data.get("action")
        if action == "transfer" and settings.get("transfer"):
            add_activity(f"TRANSFER by {customer_number}")
            dial = Dial(callerId=customer_number)
            dial.number(settings["transfer"])
            resp.append(dial)
        elif action == "callback":
            add_activity(f"CALLBACK by {customer_number}")
            add_callback(customer_number)
            add_history(customer_number, "callback")
            resp.say("Thank you. We will call you back later. Goodbye.")
            resp.hangup()
        elif action == "dnc":
            add_activity(f"DNC by {customer_number}")
            add_dnc(customer_number)
            add_history(customer_number, "dnc")
            resp.say("You have been removed from our list. Goodbye.")
            resp.hangup()
        elif action == "repeat":
            resp.redirect("/voice")
        elif action == "hangup":
            add_history(customer_number, "hangup")
            resp.say("Goodbye.")
            resp.hangup()
        else:
            resp.say(settings.get("invalid_message"))
            resp.redirect("/voice")
    else:
        resp.say(settings.get("invalid_message"))
        resp.redirect("/voice")
    return str(resp)

@app.route("/export_dnc")
def export_dnc():
    dnc = load_json(DNC_FILE, [])
    return Response("\n".join(dnc) or "Empty", mimetype="text/plain",
                    headers={"Content-Disposition": "attachment;filename=dnc_list.txt"})

@app.route("/export_callback")
def export_callback():
    cb = load_json(CALLBACK_FILE, [])
    return Response("\n".join(cb) or "Empty", mimetype="text/plain",
                    headers={"Content-Disposition": "attachment;filename=callback_list.txt"})

@app.route("/sms", methods=["POST"])
def sms():
    return "OK", 200

@app.route("/health")
def health():
    return "OK", 200

if TELEGRAM_BOT_TOKEN and BASE_URL:
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
                     params={"url": f"{BASE_URL}/telegram_webhook"}, timeout=10)
    except:
        pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
