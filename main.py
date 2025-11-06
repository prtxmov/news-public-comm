# main.py  -- final production-ready worker
import os
import time
import json
import logging
import requests
import base64
from threading import Thread

# optional libs
try:
    import redis
except Exception:
    redis = None

from dotenv import load_dotenv
import openai

# google genai (Gemini)
try:
    from google import genai
except Exception:
    genai = None

# optional small health server (Flask) if you need a bound port (enable with ENABLE_HEALTH=1)
ENABLE_HEALTH = os.getenv("ENABLE_HEALTH", "0") in ("1", "true", "True")

if ENABLE_HEALTH:
    try:
        from flask import Flask, jsonify
    except Exception:
        Flask = None
        logging.warning("Flask not installed; health endpoint won't run unless flask is installed in requirements.")

# load environment file if present
load_dotenv()

# -------- Configuration & env vars --------
CRYPTOPANIC_KEY = os.getenv("CRYPTOPANIC_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
REDIS_URL = os.getenv("REDIS_URL", "").strip() or None
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "180"))
MAX_FETCH_LIMIT = int(os.getenv("MAX_FETCH_LIMIT", "15"))

# CryptoPanic endpoint to prefer (v1 is simpler). If you have a different dev endpoint (v2), set CP_API_URL
CP_API_URL = os.getenv("CP_API_URL", "https://cryptopanic.com/api/v1/posts/")

# Logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("crypto-mcp")

# -------- Basic validation --------
missing = []
if not CRYPTOPANIC_KEY:
    missing.append("CRYPTOPANIC_KEY")
if not OPENAI_API_KEY:
    missing.append("OPENAI_API_KEY")
if not GEMINI_API_KEY:
    log.warning("GEMINI_API_KEY not set — image generation will be disabled.")
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    missing.append("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID")

if missing:
    log.error("Missing required env vars: %s. Please set them before running.", ", ".join(missing))
    # Do not exit immediately — allow the worker to run for debugging but it won't post.
    # You can uncomment below to abort early:
    # raise SystemExit("Missing env vars: " + ", ".join(missing))

# -------- Setup external clients --------
openai.api_key = OPENAI_API_KEY if OPENAI_API_KEY else None

if genai is not None and GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception:
        gemini_client = None
        log.exception("Failed to initialize google-genai client (Gemini).")
else:
    gemini_client = None
    if genai is None:
        log.warning("google-genai package not installed; gemini image generation is disabled.")
    elif not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY not provided; gemini image generation is disabled.")

# -------- Persistence (Redis or local file) --------
SEEN_KEY = "cryptopanic_seen_ids"
if REDIS_URL and redis:
    try:
        redis_client = redis.from_url(REDIS_URL)
        def is_seen(_id): return redis_client.sismember(SEEN_KEY, _id)
        def mark_seen(_id): return redis_client.sadd(SEEN_KEY, _id)
        log.info("Using Redis for persistence (REDIS_URL provided).")
    except Exception:
        redis_client = None
        log.exception("Failed to connect to Redis; falling back to local file.")
        REDIS_URL = None

if not REDIS_URL:
    DATA_FILE = "/tmp/seen_ids.json"
    try:
        with open(DATA_FILE, "r") as f:
            SEEN = set(json.load(f))
    except Exception:
        SEEN = set()
    def is_seen(_id): return _id in SEEN
    def mark_seen(_id):
        SEEN.add(_id)
        try:
            with open(DATA_FILE, "w") as f:
                json.dump(list(SEEN), f)
        except Exception:
            log.exception("Failed to write seen ids to local file.")

    log.info("Using local file for persistence at %s (ephemeral across restarts).", DATA_FILE)

# -------- Helper functions --------
def fetch_news_with_backoff(limit=10, max_attempts=6):
    """
    Fetch CryptoPanic posts with exponential backoff and Retry-After handling.
    Returns list of results or [] on repeated failures.
    """
    if not CRYPTOPANIC_KEY:
        log.error("CRYPTOPANIC_KEY missing; cannot fetch CryptoPanic.")
        return []

    params = {
        "auth_token": CRYPTOPANIC_KEY,
        "public": "true",
        "filter": "news",
        "page": 1
    }
    attempt = 0
    backoff = 2  # seconds
    while attempt < max_attempts:
        try:
            r = requests.get(CP_API_URL, params=params, timeout=20)
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = int(retry_after)
                    except Exception:
                        wait = backoff
                else:
                    wait = backoff
                log.warning("CryptoPanic 429 Too Many Requests — waiting %s seconds (attempt %d/%d).", wait, attempt+1, max_attempts)
                time.sleep(wait)
                attempt += 1
                backoff = min(300, backoff * 2)
                continue
            r.raise_for_status()
            data = r.json()
            results = data.get("results", []) if isinstance(data, dict) else data
            if not results:
                log.info("CryptoPanic returned empty results.")
                return []
            return results[:limit]
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            log.exception("HTTPError fetching CryptoPanic (status=%s).", status)
            if status == 429:
                # handled above but fallback
                time.sleep(backoff)
                attempt += 1
                backoff = min(300, backoff*2)
                continue
            if status and 500 <= status < 600:
                # server error - retry
                time.sleep(backoff)
                attempt += 1
                backoff = min(300, backoff*2)
                continue
            # other HTTP errors -> no retry
            return []
        except Exception:
            log.exception("Unexpected error fetching CryptoPanic.")
            time.sleep(backoff)
            attempt += 1
            backoff = min(300, backoff*2)
    log.error("Exceeded fetch attempts for CryptoPanic; returning empty list.")
    return []

def ask_chatgpt_for_json(title, url, excerpt):
    """
    Ask OpenAI ChatCompletion to return JSON with keys: summary, caption, image_prompt.
    Returns dict fallback if parsing fails.
    """
    if not OPENAI_API_KEY:
        log.error("OPENAI_API_KEY missing — cannot summarize articles.")
        return {"summary": f"{title} — read more: {url}", "caption": title[:120], "image_prompt": {"scene": title}}

    system = (
        "You are a concise crypto news editor. Output ONLY valid JSON with three keys: "
        "\"summary\" (2-3 sentence factual summary), \"caption\" (<=120 chars), and \"image_prompt\" "
        "(an object with keys style, scene, elements, restrictions)."
    )
    user = f"Title: {title}\nURL: {url}\n\nExcerpt: {excerpt}\n\nReturn JSON exactly like: {{\"summary\":\"...\",\"caption\":\"...\",\"image_prompt\":{{\"style\":\"\",\"scene\":\"\",\"elements\":\"\",\"restrictions\":\"\"}}}}"

    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4.1-mini",
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            max_tokens=400,
            temperature=0.2,
        )
        txt = resp["choices"][0]["message"]["content"].strip()
        try:
            j = json.loads(txt)
            return j
        except Exception:
            # Try to extract JSON substring if model returned text + JSON
            start = txt.find("{")
            end = txt.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    j = json.loads(txt[start:end+1])
                    return j
                except Exception:
                    log.exception("Failed to parse JSON from ChatGPT response.")
            log.warning("ChatGPT didn't return parseable JSON. Using fallback summary text.")
            return {"summary": txt[:800], "caption": title[:120], "image_prompt": {"scene": title}}
    except Exception:
        log.exception("ChatGPT call failed; returning fallback summary.")
        return {"summary": f"{title} — read more: {url}", "caption": title[:120], "image_prompt": {"scene": title}}

def generate_image_via_gemini(prompt_obj):
    """
    Use google-genai (Gemini) to produce an image. Returns raw bytes or None.
    """
    if gemini_client is None:
        log.warning("Gemini client not configured - skipping image generation.")
        return None

    if isinstance(prompt_obj, dict):
        parts = []
        for k, v in prompt_obj.items():
            parts.append(f"{k}: {v}")
        prompt_text = " | ".join(parts)
    else:
        prompt_text = str(prompt_obj)

    log.info("Requesting Gemini image. Prompt (truncated): %s", prompt_text[:240])
    try:
        # Model name may vary based on your access. If this errors, try "gemini-2.1" or "gemini-2.5-flash-image" etc.
        model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        response = gemini_client.models.generate_content(model=model_name, contents=[prompt_text])
        # The SDK returns candidates with parts; find inline_data
        try:
            candidate = response.candidates[0]
            for part in candidate.content.parts:
                inline = getattr(part, "inline_data", None)
                if inline:
                    # inline.data is a bytes-like; return raw bytes
                    return bytes(inline.data)
                # fallback: if part.text contains a data:uri
                text = getattr(part, "text", None)
                if text and text.strip().startswith("data:image"):
                    b64 = text.split(",", 1)[1]
                    return base64.b64decode(b64)
        except Exception:
            log.exception("Unexpected Gemini response shape.")
    except Exception:
        log.exception("Gemini image generation failed.")
    return None

def post_to_telegram(image_bytes, caption):
    """
    Post either a photo (if image_bytes) or text message to Telegram.
    Returns response JSON or None.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram bot token or chat id missing; cannot post.")
        return None

    try:
        if image_bytes:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            files = {"photo": ("news.png", image_bytes)}
            data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
            r = requests.post(url, data=data, files=files, timeout=30)
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            json_data = {"chat_id": TELEGRAM_CHAT_ID, "text": caption, "parse_mode": "HTML"}
            r = requests.post(url, json=json_data, timeout=30)

        r.raise_for_status()
        log.info("Posted to Telegram (status %s).", r.status_code)
        return r.json()
    except requests.HTTPError as e:
        log.exception("Telegram API returned HTTP error: %s", getattr(e.response, "text", str(e)))
        return None
    except Exception:
        log.exception("Failed to post to Telegram.")
        return None

# -------- Optional small health server --------
def start_health_server():
    if not ENABLE_HEALTH:
        return
    if 'Flask' not in globals() or Flask is None:
        log.error("ENABLE_HEALTH set but Flask is not available in environment. Install Flask to enable health endpoint.")
        return
    app = Flask("health")
    @app.route("/healthz")
    def healthz():
        return {"status": "ok"}
    port = int(os.getenv("PORT", "10000"))
    log.info("Starting health server on port %s", port)
    # Run Flask in background thread (development server is OK for health endpoint)
    Thread(target=lambda: app.run(host="0.0.0.0", port=port), daemon=True).start()

# -------- Main processing loop --------
def process_once():
    # fetch news (with backoff)
    news_items = fetch_news_with_backoff(limit=MAX_FETCH_LIMIT)
    if not news_items:
        log.info("No news items fetched this cycle.")
        return 0

    posted = 0
    for item in news_items:
        # get unique id - CryptoPanic 'id' field usually exists; fallback to url
        nid = str(item.get("id") or item.get("uuid") or item.get("url") or "")
        if not nid:
            continue
        if is_seen(nid):
            log.debug("Skipping already seen id %s", nid)
            continue

        title = item.get("title", "Untitled")
        url = item.get("url", "")
        excerpt = (item.get("body") or item.get("excerpt") or "")[:1000]

        log.info("Processing article: %s", title)

        # Summarize & get image prompt
        j = ask_chatgpt_for_json(title, url, excerpt)
        summary = j.get("summary", title)
        caption = j.get("caption", title)[:120]
        image_prompt = j.get("image_prompt", {"scene": title})

        # Telegram message: summary + link
        tg_caption = f"{summary}\n\n🔗 <a href=\"{url}\">Read more</a>"

        # Generate image (best-effort)
        img_bytes = None
        try:
            img_bytes = generate_image_via_gemini(image_prompt)
        except Exception:
            log.exception("Image generation crashed for article: %s", title)
            img_bytes = None

        # Post to Telegram
        res = post_to_telegram(img_bytes, tg_caption)
        if res:
            mark_seen(nid)
            posted += 1
            # small delay to reduce burst
            time.sleep(1.2)
        else:
            log.warning("Failed to post article %s to Telegram; will not mark as seen.", title)
    log.info("Cycle complete — posted %d new items.", posted)
    return posted

def main_loop():
    if ENABLE_HEALTH:
        start_health_server()
    log.info("Starting main loop: poll every %s seconds", POLL_SECONDS)
    while True:
        try:
            process_once()
        except Exception:
            log.exception("Unhandled exception in main loop.")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main_loop()
