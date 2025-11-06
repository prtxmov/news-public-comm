# Crypto MCP (CryptoPanic → ChatGPT → Gemini → Telegram)

A background worker for Render that:
1. Pulls latest crypto news from CryptoPanic.
2. Summarizes using ChatGPT (OpenAI gpt-4.1-mini).
3. Generates images using Google Gemini API (API key method, no service account needed).
4. Posts to Telegram channel.

## Setup

1. Fill in `.env` (or env vars in Render) with your API keys.
2. Push repo to GitHub.
3. Deploy to Render as a Background Worker.
4. Add all `.env` variables in the Render Dashboard.

## Notes

- Uses Redis if available to track seen news and avoid duplicates.
- Default polling interval: 90 seconds.
