"""
Dagelijkse Nieuwssamenvatting via Telegram
=========================================
Haalt nieuws op via RSS, filtert/dedupliceert, vat samen met OpenAI, stuurt naar Telegram.

Setup:
  pip install feedparser openai requests python-dotenv

Benodigde .env variabelen:
  OPENAI_API_KEY=sk-...
  TELEGRAM_BOT_TOKEN=123456789:ABCDEF...
  TELEGRAM_CHAT_ID=123456789
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
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

# Lager quotum voor evergreen/onderzoek-bronnen die geen "nieuws van vandaag" zijn
FEED_ITEM_CAP = {
    "Financial Times": 6,
    "BBC Business": 6,
    "The Economist": 6,
    "McKinsey Insights": 2,
}
DEFAULT_ITEM_CAP = 5

MAX_CHARS_PER_ITEM = 300
LOOKBACK_HOURS = 36  # alleen items die korter dan dit geleden gepubliceerd zijn
DEDUP_TITLE_SIMILARITY = 0.72  # boven deze drempel beschouwen we titels als hetzelfde verhaal

CACHE_FILE = Path(__file__).parent / "seen_articles.json"
CACHE_RETENTION_DAYS = 5  # hoe lang een link/titel "al gezien" blijft

MODEL = "gpt-4.1-mini"  # zet evenueel terug naar gpt-4.1-mini voor lagere kosten


# ── Seen-cache ──────────────────────────────────────────────────────────────

def load_seen_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=CACHE_RETENTION_DAYS)
    pruned = {}
    for key, added_at in data.items():
        try:
            ts = datetime.fromisoformat(added_at)
        except ValueError:
            continue
        if ts >= cutoff:
            pruned[key] = added_at
    return pruned


def save_seen_cache(cache: dict) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"⚠️ Kon seen-cache niet opslaan: {e}")


def normalize_title(title: str) -> str:
    """Lowercase, leestekens weg, witruimte normaliseren — voor vergelijking."""
    cleaned = re.sub(r"[^\w\s]", "", title.lower())
    return " ".join(cleaned.split())


def is_duplicate(title: str, seen_titles: list[str]) -> bool:
    norm = normalize_title(title)
    for other in seen_titles:
        if SequenceMatcher(None, norm, other).ratio() >= DEDUP_TITLE_SIMILARITY:
            return True
    return False


# ── Tekst helpers ─────────────────────────────────────────────────────────────

def truncate_at_sentence(text: str, max_chars: int) -> str:
    """Knipt tekst af op het laatste zinseinde binnen max_chars, i.p.v. midden in een zin."""
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text

    window = text[:max_chars]
    last_boundary = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))

    if last_boundary > max_chars * 0.4:  # alleen gebruiken als het niet te vroeg afkapt
        return window[: last_boundary + 1]

    return window.rstrip() + "…"


def entry_published_dt(item) -> datetime | None:
    """Geeft een timezone-aware publicatiedatum terug, of None als onbekend."""
    parsed = item.get("published_parsed") or item.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# ── Nieuws ophalen ─────────────────────────────────────────────────────────────

def fetch_news() -> tuple[str, list[dict]]:
    """
    Haalt RSS-feeds op, filtert op recentheid en dedupliceert.
    Geeft (ruwe_tekst_voor_openai, lijst_van_gebruikte_items) terug.
    """
    seen_cache = load_seen_cache()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    used_items: list[dict] = []
    seen_titles_this_run: list[str] = []

    for source_name, url in RSS_FEEDS.items():
        feed = feedparser.parse(url)

        if feed.bozo:
            print(f"⚠️ Feed '{source_name}' gaf een parsefout: {feed.bozo_exception}")

        if not feed.entries:
            print(f"⚠️ Geen entries gevonden voor '{source_name}', bron overgeslagen.")
            continue

        cap = FEED_ITEM_CAP.get(source_name, DEFAULT_ITEM_CAP)
        added_for_source = 0

        for item in feed.entries:
            if added_for_source >= cap:
                break

            title = item.get("title", "").strip()
            link = item.get("link", "").strip()
            if not title or not link:
                continue

            # Recentheidsfilter — items zonder datum laten we erdoor (sommige feeds
            # zoals McKinsey geven die niet altijd consistent mee), maar wel binnen het cap.
            published_dt = entry_published_dt(item)
            if published_dt is not None and published_dt < cutoff:
                continue

            # Al eerder verstuurd?
            if link in seen_cache or normalize_title(title) in seen_cache:
                continue

            # Dubbel verhaal binnen deze run (bv. zelfde nieuws bij FT en BBC)?
            if is_duplicate(title, seen_titles_this_run):
                continue

            summary = item.get("summary", item.get("description", "")).strip()
            summary = truncate_at_sentence(summary, MAX_CHARS_PER_ITEM)

            used_items.append(
                {
                    "source": source_name,
                    "title": title,
                    "summary": summary,
                    "link": link,
                }
            )
            seen_titles_this_run.append(normalize_title(title))
            added_for_source += 1

    raw_text = "\n\n".join(
        f"[{it['source']}] {it['title']}\nSamenvatting: {it['summary']}\nLink: {it['link']}"
        for it in used_items
    )

    return raw_text, used_items


def update_seen_cache(used_items: list[dict]) -> None:
    cache = load_seen_cache()
    now_iso = datetime.now(timezone.utc).isoformat()
    for it in used_items:
        cache[it["link"]] = now_iso
        cache[normalize_title(it["title"])] = now_iso
    save_seen_cache(cache)


# ── Samenvatten met OpenAI ─────────────────────────────────────────────────────

INSTRUCTIONS = """
Je bent mijn persoonlijke ochtendbriefing-assistent.

Maak een cleane Nederlandse ochtendbriefing op basis van de nieuwsitems die de gebruiker
hieronder aanlevert.

Belangrijk:
Sommige bronnen bevatten alleen een titel, korte teaser en link.
Doe niet alsof je het volledige artikel hebt gelezen.
Baseer je alleen op de beschikbare titel, teaser en link.
Formuleer beperkt beschikbare informatie als signaal, niet als harde conclusie.
Vertaal Engelstalige titels naar het Nederlands — gebruik nooit een Engelse titel in de output.

Doel:
Ik wil in 2 minuten een breed overzicht krijgen van de belangrijkste economische,
geopolitieke, business-, technologie- en marktontwikkelingen. Geef impliciet iets meer
gewicht aan items die raken aan AI/data, strategie-consulting en financiele markten/
beleggen, zonder dat je dit expliciet benoemt in de output — het is een sorteercriterium,
geen onderwerp om te noemen.

Gebruik exact deze structuur:

📅 Ochtendbriefing — {today}

IN 30 SECONDEN

• [Thema 1: clustering van gerelateerde signalen in één zin, bv. "Midden-Oosten diplomatie beweegt op meerdere fronten"]
• [Thema 2: ander cluster in één zin]
• [Thema 3: ander cluster in één zin]

TOP 10 SIGNALEN

1. [Korte titel in het Nederlands]
[Korte uitleg in maximaal 1 zin.]
Impact: [Concrete, specifieke reden waarom dit relevant is — geen generieke zin die op bijna elk artikel in deze categorie zou passen.]
[Zet hier alleen de URL]

2. [Korte titel in het Nederlands]
[Korte uitleg in maximaal 1 zin.]
Impact: [Concrete, specifieke reden.]
[Zet hier alleen de URL]

Herhaal dit voor 6 tot 10 items — kies niet meer items dan er daadwerkelijk relevant nieuws is.
Sorteer de signalen op relevantie/impact, niet op de volgorde waarin ze zijn aangeleverd.

OM TE ONTHOUDEN

• [Vooruitkijkende conclusie: wat dit voor de komende dagen kan betekenen — geen herhaling van IN 30 SECONDEN]
• [Tweede vooruitkijkende conclusie]

Voorbeeld van een zwakke Impact-zin (vermijd dit soort generieke formulering):
"Impact: dit kan invloed hebben op stabiliteit en de economie."
Voorbeeld van een sterke Impact-zin (specifiek en concreet):
"Impact: een uitbreiding van de gesprekken naar Iran kan olieprijzen op korte termijn beïnvloeden."

Selectieregels:
• Kies tussen 6 en 10 items, alleen items die er echt toe doen
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
"""


def summarize_news(raw_news: str) -> str:
    """Stuurt nieuws naar OpenAI en krijgt een Telegram-vriendelijke samenvatting terug."""
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    today = datetime.now().strftime("%d-%m-%Y")
    instructions = INSTRUCTIONS.replace("{today}", today)

    response = client.responses.create(
        model=MODEL,
        instructions=instructions,
        input=f"Nieuwsitems:\n\n{raw_news}",
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
    raw_news, used_items = fetch_news()

    if not raw_news.strip():
        print("⚠️ Geen nieuwsartikelen gevonden (na filtering/dedup).")
        return

    print(f"📰 {len(used_items)} items overgehouden na filtering en dedup.")

    print("🤖 Samenvatten met OpenAI...")
    summary = summarize_news(raw_news)

    print("\n── Samenvatting ──────────────────────────────────")
    print(summary)
    print("──────────────────────────────────────────────────\n")

    print("📱 Versturen naar Telegram...")
    sent_ok = send_telegram_message(summary)

    if sent_ok:
        update_seen_cache(used_items)


if __name__ == "__main__":
    main()
