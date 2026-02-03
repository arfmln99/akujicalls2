import os, json, re, asyncio, time
import requests
import random
from telethon import TelegramClient, events

# =========================
# ENV
# =========================
API_ID = int(os.getenv("API_ID", "36844603"))
API_HASH = os.getenv("API_HASH", "5e4401388a3a85acb0efc50bf8f41c6f")

SOURCE_CHAT_ID = int(os.getenv("SOURCE_CHAT_ID", "-1003522701263"))   # Grup 1 (realtime)
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-1003674803959"))   # Grup 2 (tujuan)

# ✅ Channel 3 (realtime) — pakai ID angka -100...
CHANNEL_CHAT_ID = int(os.getenv("CHANNEL_CHAT_ID", "-1001758611100"))

BEARER_TOKEN = os.getenv(
    "BEARER_TOKEN",
    "AAAAAAAAAAAAAAAAAAAAAE1B3AEAAAAA%2FTzcoldK0UTvODM2SkLOQDM%2FHkw%3DAtYIg1H1MYpxWADOwELwOixYO33hH44JHuP4QOje4pnJ1Ll4JL"
)
X_USERNAME = os.getenv("X_USERNAME", "0xWiz7")

# ✅ X Free plan: polling aman >= 900 detik (15 menit). Pakai 930 biar buffer.
POLL_SECONDS = int(float(os.getenv("POLL_SECONDS", "930")))

STATE_FILE = os.getenv("STATE_FILE", "x_state.json")

# ✅ Start silent X: run pertama set patokan terbaru, tidak kirim backlog
START_SILENT = os.getenv("START_SILENT", "true").lower() in ("1", "true", "yes")

# Hapus link dari teks (channel + X)
REMOVE_LINKS = os.getenv("REMOVE_LINKS", "true").lower() in ("1", "true", "yes")

# Channel: hanya proses post yang ada media (gambar/video)?
CHANNEL_REQUIRE_MEDIA = os.getenv("CHANNEL_REQUIRE_MEDIA", "true").lower() in ("1", "true", "yes")

# Dedup max (biar state gak bengkak)
SEEN_MAX = int(os.getenv("SEEN_MAX", "5000"))

if API_ID == 0 or not API_HASH:
    raise SystemExit("Set API_ID & API_HASH")
if SOURCE_CHAT_ID == 0 or TARGET_CHAT_ID == 0:
    raise SystemExit("Set SOURCE_CHAT_ID & TARGET_CHAT_ID")
if not BEARER_TOKEN:
    print("⚠️ BEARER_TOKEN kosong. Monitoring X akan off.")

# =========================
# Address regex (EVM / SOL / TON)
# =========================
EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
SOL_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
TON_RE = re.compile(r"\b(?:EQ|UQ|kQ)[A-Za-z0-9_-]{46}\b")

# exclude yang diawali /pnl
PNL_EXCLUDE_RE = re.compile(
    r"(?i)(?:^|\s)/pnl\s+("
    r"[1-9A-HJ-NP-Za-km-z]{32,44}"
    r"|0x[a-fA-F0-9]{40}"
    r"|(?:EQ|UQ|kQ)[A-Za-z0-9_-]{46}"
    r")\b"
)

def extract_addresses(text: str):
    text = text or ""
    excluded = set(m.group(1) for m in PNL_EXCLUDE_RE.finditer(text))

    found = []
    found += EVM_RE.findall(text)
    found += SOL_RE.findall(text)
    found += TON_RE.findall(text)

    out, seen = [], set()
    for a in found:
        # ✅ normalize EVM biar gak double karena beda huruf besar/kecil
        if a.startswith("0x"):
            a = a.lower()

        if a in excluded:
            continue
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out

def remove_links(text: str) -> str:
    text = text or ""
    if not REMOVE_LINKS:
        return text.strip()
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# =========================
# Countdown helper (per detik)
# =========================
async def countdown_sleep(seconds: int, prefix: str = "Menunggu Jeda"):
    seconds = max(int(seconds), 0)
    if seconds <= 0:
        return
    print(f"{prefix} {seconds} detik...")
    for remaining in range(seconds, 0, -1):
        print(f"⏳ {remaining}  ", end="\r")
        await asyncio.sleep(1)
    print("✅ Lanjut proses...      ")

# =========================
# State helpers + GLOBAL DEDUP CA
# =========================
_state_lock = asyncio.Lock()

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f) or {}
    except:
        st = {}

    seen = st.get("seen_addrs") or []
    if not isinstance(seen, list):
        seen = []
    st["seen_addrs"] = seen[-SEEN_MAX:]
    return st

def save_state(st):
    seen = st.get("seen_addrs") or []
    st["seen_addrs"] = seen[-SEEN_MAX:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f)

async def filter_new_addrs(addrs):
    """Return only addresses that haven't been shared before (global dedup)."""
    if not addrs:
        return []
    async with _state_lock:
        st = load_state()
        seen_list = st.get("seen_addrs") or []
        seen_set = set(seen_list)

        new_addrs = [a for a in addrs if a not in seen_set]
        if new_addrs:
            seen_list.extend(new_addrs)
            st["seen_addrs"] = seen_list[-SEEN_MAX:]
            save_state(st)

        return new_addrs

# =========================
# X API helpers
# =========================
def x_headers():
    return {"Authorization": f"Bearer {BEARER_TOKEN}"}

def x_get_user_id(username: str) -> str:
    url = f"https://api.x.com/2/users/by/username/{username}"
    r = requests.get(url, headers=x_headers(), timeout=20)
    r.raise_for_status()
    return r.json()["data"]["id"]

def x_get_latest_tweets(user_id: str, since_id: str | None):
    url = f"https://api.x.com/2/users/{user_id}/tweets"
    params = {
        "max_results": 5,  # boleh 5, tapi nanti kita KIRIM 1 (yang terbaru)
        "tweet.fields": "text,attachments",
        "expansions": "attachments.media_keys",
        "media.fields": "url,preview_image_url,type",
        "exclude": "retweets,replies",
    }
    if since_id:
        params["since_id"] = since_id

    r = requests.get(url, headers=x_headers(), params=params, timeout=20)
    r.raise_for_status()
    j = r.json()

    tweets = j.get("data", []) or []
    media_list = (j.get("includes", {}) or {}).get("media", []) or []

    media_map = {}
    for m in media_list:
        key = m.get("media_key")
        murl = m.get("url") or m.get("preview_image_url")
        if key and murl:
            media_map[key] = murl

    return tweets, media_map

def clean_tweet_text(text: str) -> str:
    text = (text or "").strip()
    text = remove_links(text)
    text = re.sub(r"(?im)^chart:.*$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def download(url: str, out_path: str):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(r.content)

# =========================
# Telegram client
# =========================
client = TelegramClient("session", API_ID, API_HASH)

# =========================
# (1) Grup 1 realtime -> forward CA ke Grup 2 (DEDUP GLOBAL)
# =========================
@client.on(events.NewMessage(chats=SOURCE_CHAT_ID))
async def group_handler(event):
    text = event.raw_text or ""
    addrs = extract_addresses(text)
    if not addrs:
        return

    new_addrs = await filter_new_addrs(addrs)
    if not new_addrs:
        return

    msg = "🔥 New Calls Gems\n\n✅ CA:\n" + "\n".join(new_addrs)
    await client.send_message(TARGET_CHAT_ID, msg)
    print("✅ Sent from group (dedup ok)")

# =========================
# (2) Channel 3 realtime -> forward (judul tetap: New Calls Gems) + teks + gambar + CA (no links) (DEDUP GLOBAL)
# =========================
@client.on(events.NewMessage(chats=CHANNEL_CHAT_ID))
async def channel_handler(event):
    if CHANNEL_REQUIRE_MEDIA and not (event.message and event.message.media):
        return

    raw_text = event.raw_text or ""
    addrs = extract_addresses(raw_text)
    if not addrs:
        return

    new_addrs = await filter_new_addrs(addrs)
    if not new_addrs:
        return

    clean_text = remove_links(raw_text)

    caption = f"🔥 New Calls Gems\n\n{clean_text}\n\n✅ CA:\n" + "\n".join(new_addrs)

    if event.message and event.message.media:
        await client.send_file(TARGET_CHAT_ID, event.message.media, caption=caption)
    else:
        await client.send_message(TARGET_CHAT_ID, caption)

    print("✅ Sent from channel (dedup ok)")

# =========================
# (3) X polling 15 menit -> forward 1 tweet TERBARU saja (START SILENT KERAS + DEDUP GLOBAL)
# =========================
async def twitter_loop():
    if not BEARER_TOKEN:
        return

    state = load_state()
    since_id = state.get("since_id")

    # cache user_id biar gak boros request kalau restart
    user_id = state.get("user_id")
    if not user_id:
        user_id = x_get_user_id(X_USERNAME)
        state["user_id"] = user_id
        save_state(state)

    # ✅ START SILENT KERAS:
    # - kalau since_id kosong: set patokan ke tweet terbaru
    # - TIDAK KIRIM apa pun
    # - langsung tidur 15 menit
    if START_SILENT and not since_id:
        print("✅ Start Silent X aktif (keras): set patokan terbaru, tidak kirim backlog...")
        try:
            tweets, _ = x_get_latest_tweets(user_id, None)
            if tweets:
                newest_id = max(t["id"] for t in tweets)
                state["since_id"] = newest_id
                save_state(state)
                since_id = newest_id
                print(f"✅ Patokan X diset: {newest_id}. Menunggu tweet baru...\n")
                await countdown_sleep(POLL_SECONDS, prefix="Menunggu Jeda")
            else:
                print("✅ Tidak ada tweet untuk patokan. Menunggu...\n")
        except Exception as e:
            print("❌ Gagal set patokan Start Silent X:", e)

    print("✅ Monitoring X... (Polling 15 menit, kirim 1 terbaru)")

    while True:
        try:
            tweets, media_map = x_get_latest_tweets(user_id, since_id)

            if tweets:
                # ✅ selalu majuin since_id ke tweet TERBARU yang kebaca
                newest_id = max(t["id"] for t in tweets)
                since_id = newest_id
                state["since_id"] = since_id
                save_state(state)

                # ✅ pilih 1 tweet paling baru
                t = max(tweets, key=lambda x: x["id"])
                tid = t["id"]
                raw = t.get("text", "") or ""
                text = clean_tweet_text(raw)

                addrs = extract_addresses(raw)
                if addrs:
                    new_addrs = await filter_new_addrs(addrs)
                    if new_addrs:
                        caption = f"🔥 New Calls Gems\n\n{text}\n\n✅ CA:\n" + "\n".join(new_addrs)

                        files = []
                        keys = (t.get("attachments") or {}).get("media_keys") or []
                        for i, k in enumerate(keys):
                            murl = media_map.get(k)
                            if not murl:
                                continue
                            out = f"tmp_{tid}_{i}.jpg"
                            try:
                                download(murl, out)
                                files.append(out)
                            except Exception as e:
                                print("Media download failed:", e)

                        if files:
                            await client.send_file(TARGET_CHAT_ID, files, caption=caption)
                        else:
                            await client.send_message(TARGET_CHAT_ID, caption)

                        for f in files:
                            try:
                                os.remove(f)
                            except:
                                pass

                        print("✅ Sent X post (latest only)", tid)
                    else:
                        print("↩️ X skipped (CA sudah pernah di-share)")
                else:
                    print("↩️ X skipped (tidak ada CA)")
            else:
                print("ℹ️ Tidak ada tweet baru.")

        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 429:
                reset = None
                try:
                    reset = e.response.headers.get("x-rate-limit-reset")
                except:
                    reset = None

                if reset:
                    wait_time = max(int(reset) - int(time.time()), 0) + random.randint(5, 20)
                else:
                    wait_time = 900 + random.randint(5, 20)

                await countdown_sleep(wait_time, prefix="Menunggu Jeda")
                continue

            print("❌ X HTTP error:", e)

        except Exception as e:
            print("❌ X loop error:", e)

        await countdown_sleep(POLL_SECONDS, prefix="Menunggu Jeda")

# =========================
# Run
# =========================
async def main():
    await client.start()
    print("🚀 Bot running (Group realtime + Channel realtime + X 15-min polling + Dedup + StartSilent)...")
    task = asyncio.create_task(twitter_loop())
    try:
        await client.run_until_disconnected()
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bot dihentikan manual (Ctrl+C).")
