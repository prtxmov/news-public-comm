import os, time, json, base64, logging, requests, openai
from dotenv import load_dotenv
from PIL import Image

try:
    import redis
except ImportError:
    redis = None

from google import genai

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CRYPTOPANIC_KEY = os.getenv("CRYPTOPANIC_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "90"))
REDIS_URL = os.getenv("REDIS_URL")

openai.api_key = OPENAI_API_KEY
client = genai.Client(api_key=GEMINI_API_KEY)

SEEN_KEY = "cryptopanic_seen_ids"
if REDIS_URL and redis:
    r = redis.from_url(REDIS_URL)
    is_seen = lambda _id: r.sismember(SEEN_KEY, _id)
    mark_seen = lambda _id: r.sadd(SEEN_KEY, _id)
    logging.info("Using Redis for persistence")
else:
    DATA_FILE = "/tmp/seen_ids.json"
    try:
        with open(DATA_FILE, "r") as f:
            SEEN = set(json.load(f))
    except Exception:
        SEEN = set()
    def is_seen(_id): return _id in SEEN
    def mark_seen(_id):
        SEEN.add(_id)
        with open(DATA_FILE, "w") as f: json.dump(list(SEEN), f)
    logging.info("Using local file for persistence")

import time, logging, requests

CRYPTOPANIC_KEY = os.getenv("CRYPTOPANIC_KEY")
DEFAULT_POLL_SECONDS = int(os.getenv("POLL_SECONDS", "180"))

def fetch_news_with_backoff(limit=10, max_attempts=5):
    url = "https://cryptopanic.com/api/v1/posts/"  # prefer /api/v1/posts/ if available
    params = {"auth_token": CRYPTOPANIC_KEY, "public": "true", "filter": "news"}
    attempt = 0
    backoff = 1.0
    while attempt < max_attempts:
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429:
                # honor Retry-After if provided
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    wait = int(retry_after)
                    logging.warning("CryptoPanic 429 — Retry-After: %s seconds", wait)
                else:
                    wait = min(60, int(backoff))  # cap wait
                    logging.warning("CryptoPanic 429 — backing off for %s seconds (attempt %d/%d)", wait, attempt+1, max_attempts)
                time.sleep(wait)
                attempt += 1
                backoff *= 2
                continue
            r.raise_for_status()
            data = r.json()
            results = data.get("results", []) if isinstance(data, dict) else data
            return results[:limit]
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", None)
            logging.exception("HTTPError fetching CryptoPanic (status %s)", code)
            if code == 429:
                # caught above usually, but fallback
                time.sleep(min(60, backoff))
                attempt += 1
                backoff *= 2
                continue
            # for other 5xx errors, backoff too
            if code and 500 <= code < 600:
                time.sleep(min(30, backoff))
                attempt += 1
                backoff *= 2
                continue
            # non-retryable
            return []
        except Exception:
            logging.exception("Error fetching CryptoPanic feed")
            time.sleep(min(10, backoff))
            attempt += 1
            backoff *= 2
    logging.error("Exceeded attempts fetching CryptoPanic; returning empty list")
    return []


def summarize_news(title, url, excerpt):
    sys = "You are a concise crypto news editor. Output JSON with summary, caption, and image_prompt."
    user = f"Title: {title}\nURL: {url}\nExcerpt: {excerpt}\nReturn JSON with summary, caption, image_prompt."
    resp = openai.ChatCompletion.create(
        model="gpt-4.1-mini",
        messages=[{"role":"system","content":sys},{"role":"user","content":user}],
        temperature=0.25,
        max_tokens=400
    )
    txt = resp["choices"][0]["message"]["content"].strip()
    try: return json.loads(txt)
    except: return {"summary": title, "caption": title[:100], "image_prompt": {"scene": title}}

def generate_image(prompt_obj):
    prompt = " | ".join(f"{k}:{v}" for k,v in prompt_obj.items()) if isinstance(prompt_obj, dict) else str(prompt_obj)
    res = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt])
    for part in res.candidates[0].content.parts:
        if part.inline_data: return bytes(part.inline_data.data)
    return None

def post_telegram(img, caption):
    if img:
        files = {"photo": ("news.png", img)}
        data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto", data=data, files=files)
    else:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": caption, "parse_mode": "HTML"})

def process_once():
    for item in fetch_news():
        nid = str(item.get("id") or item.get("url"))
        if is_seen(nid): continue
        title, url, excerpt = item.get("title",""), item.get("url",""), item.get("body","")
        j = summarize_news(title, url, excerpt)
        caption = j.get("caption", title)
        summ = j.get("summary", title)
        prompt = j.get("image_prompt", {"scene": title})
        msg = f"{summ}\n\n🔗 <a href='{url}'>Read more</a>"
        try:
            img = generate_image(prompt)
        except Exception as e:
            logging.warning("Image failed: %s", e); img=None
        post_telegram(img, msg)
        mark_seen(nid)
        time.sleep(2)
    logging.info("Cycle done.")

if __name__ == "__main__":
    while True:
        try: process_once()
        except Exception as e: logging.error("Loop error: %s", e)
        time.sleep(POLL_SECONDS)
