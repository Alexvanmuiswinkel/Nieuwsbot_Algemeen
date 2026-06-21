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
    "Financial Times": "https://www.ft.com/rss/home",
    "McKinsey Insights": "https://www.mckinsey.com/insights/rss",
    "BBC Business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "The Economist": "https://www.economist.com/latest/rss.xml",
}

MAX_ITEMS_PER_FEED = 5
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
Je bent mijn persoonlijke ochtendbriefing-assistent.

Maak een cleane Nederlandse ochtendbriefing op basis van de nieuwsitems hieronder.

Belangrijk:
Sommige bronnen bevatten alleen een titel, korte teaser en link.
Doe niet alsof je het volledige artikel hebt gelezen.
Baseer je alleen op de beschikbare titel, teaser en link.
Formuleer beperkt beschikbare informatie als signaal, niet als harde conclusie.

Doel:
Ik wil in 2 minuten een breed overzicht krijgen van de belangrijkste economische, geopolitieke, business-, technologie- en marktontwikkelingen.

Gebruik exact deze structuur:

📅 Ochtendbriefing — {today}

IN 30 SECONDEN

• [Eerste rode draad in één zin]
• [Tweede rode draad in één zin]
• [Derde rode draad in één zin]

TOP 10 SIGNALEN

1. [Korte titel]
[Korte uitleg in maximaal 1 zin.]
Impact: [Waarom dit relevant kan zijn in maximaal 1 zin.]
[Zet hier alleen de URL]

2. [Korte titel]
[Korte uitleg in maximaal 1 zin.]
Impact: [Waarom dit relevant kan zijn in maximaal 1 zin.]
[Zet hier alleen de URL]

Herhaal dit tot maximaal 10 items.

OM TE ONTHOUDEN

• [Belangrijkste takeaway]
• [Tweede takeaway]

Selectieregels:
• Kies maximaal 10 items
• Geef prioriteit aan economie, geopolitiek, markten, technologie, bedrijven en beleid
• Vermijd sport, celebritynieuws, lifestyle en kleine incidenten
• Neem FT/Economist-items vooral mee als signaal van wat op de agenda staat
• Gebruik open bronnen met meer context voor iets meer duiding
• Als informatie beperkt is, schrijf neutraal: "Dit signaleert..." of "Dit wijst mogelijk op..."

Strikte opmaakregels:
• Gebruik geen markdown
• Gebruik nooit ** of __
• Gebruik geen streepjes als bullets
• Gebruik alleen • als bullet
• Gebruik geen [Link](url)
• Gebruik niet het woord "Link:"
• Zet URL's los op een aparte regel
• Houd witruimte tussen secties
• Schrijf kort, rustig, zakelijk en simpel
• Geen afsluiting zoals "Fijne dag"
• Geen meningen of speculatie buiten de beschikbare informatie

Nieuwsitems:
{raw_news}
"""

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
        max_output_tokens=1800,
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