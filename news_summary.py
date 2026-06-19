"""
Dagelijkse Nieuwssamenvatting via Telegram
=========================================
Haalt nieuws op via RSS, vat samen met OpenAI, stuurt naar Telegram.

Setup:
  pip install feedparser openai requests python-dotenv

Benodigde .env variabelen:
  OPENAI_API_KEY=sk-...
  TELEGRAM_BOT_TOKEN=123456789:ABCDEF...
  TELEGRAM_CHAT_ID=123456789
"""

import os
from datetime import datetime
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv
from openai import OpenAI


load_dotenv(Path(__file__).parent / ".env")


# ── Configuratie ──────────────────────────────────────────────────────────────

RSS_FEEDS = {
    "NOS Algemeen": "https://feeds.nos.nl/nosnieuwsalgemeen",
    "NU.nl Algemeen": "https://www.nu.nl/rss/algemeen",
}

MAX_ITEMS_PER_FEED = 6
MAX_CHARS_PER_ITEM = 300


# ── Nieuws ophalen ─────────────────────────────────────────────────────────────

def fetch_news() -> str:
    """Haalt RSS-feeds op en geeft ruwe tekst terug voor OpenAI."""
    all_items = []

    for source_name, url in RSS_FEEDS.items():
        feed = feedparser.parse(url)

        for item in feed.entries[:MAX_ITEMS_PER_FEED]:
            title = item.get("title", "").strip()
            summary = item.get("summary", item.get("description", "")).strip()
            summary = " ".join(summary.split())[:MAX_CHARS_PER_ITEM]
            link = item.get("link", "").strip()

            all_items.append(
                f"[{source_name}] {title}\n"
                f"Samenvatting: {summary}\n"
                f"Link: {link}"
            )

    return "\n\n".join(all_items)


# ── Samenvatten met OpenAI ─────────────────────────────────────────────────────

def summarize_news(raw_news: str) -> str:
    """Stuurt nieuws naar OpenAI en krijgt een Telegram-vriendelijke samenvatting terug."""
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    today = datetime.now().strftime("%d-%m-%Y")

    prompt = f"""
Je bent mijn persoonlijke nieuwsassistent.

Maak een beknopte dagelijkse nieuwssamenvatting voor Telegram.

Datum: {today}

Regels:
- Schrijf in het Nederlands
- Maak 6 tot 8 nieuwsitems
- Per nieuwsitem:
  1. één duidelijke titel
  2. twee korte zinnen uitleg
  3. waarom dit belangrijk is
- Gebruik eenvoudige taal
- Voeg relevante links toe

Nieuwsberichten:
{raw_news}
"""

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
        max_output_tokens=1200,
    )

    return response.output_text.strip()


# ── Telegram sturen ────────────────────────────────────────────────────────────

def split_message(text: str, max_length: int = 3900) -> list[str]:
    """Splitst lange Telegram-berichten netjes op."""
    parts = []
    current_part = ""

    for paragraph in text.split("\n\n"):
        if len(current_part) + len(paragraph) + 2 <= max_length:
            current_part += paragraph + "\n\n"
        else:
            parts.append(current_part.strip())
            current_part = paragraph + "\n\n"

    if current_part.strip():
        parts.append(current_part.strip())

    return parts


def send_telegram_message(message: str) -> bool:
    """Verstuurt een Telegram-bericht, ook als het langer is dan Telegram toestaat."""
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    for part in split_message(message):
        response = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": part,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        if not response.ok:
            print(f"❌ Fout bij versturen: {response.status_code} – {response.text}")
            return False

    print("✅ Telegram-bericht(en) verstuurd!")
    return True


# ── Hoofdprogramma ─────────────────────────────────────────────────────────────

def main() -> None:
    print(f"🗞️ Nieuws ophalen... ({datetime.now().strftime('%H:%M')})")
    raw_news = fetch_news()

    if not raw_news.strip():
        print("⚠️ Geen nieuwsartikelen gevonden.")
        return

    print("🤖 Samenvatten met OpenAI...")
    summary = summarize_news(raw_news)

    print("\n── Samenvatting ──────────────────────────────────")
    print(summary)
    print("──────────────────────────────────────────────────\n")

    print("📱 Versturen naar Telegram...")
    send_telegram_message(summary)


if __name__ == "__main__":
    main()