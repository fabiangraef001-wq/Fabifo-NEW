#!/usr/bin/env python3
"""
Fabifo - taegliches Update von Kursen und Nachrichten.
Wird automatisch von GitHub Actions ausgefuehrt (siehe .github/workflows/update.yml).

Wichtige Hinweise, bitte vor dem Einsatz lesen:

1) Kursdaten kommen ueber die inoffizielle, aber frei nutzbare
   Yahoo-Finance-Schnittstelle (Paket 'yfinance'). Yahoo kann diese jederzeit
   aendern oder blockieren. Jeder Abschnitt ist deshalb einzeln mit try/except
   abgesichert: schlaegt ein Kurs fehl, bleibt der Rest trotzdem aktuell,
   nur diese eine Kachel wird uebersprungen (alter Wert bleibt in news.json
   stehen, da das Script nur vorhandene Keys ueberschreibt).

2) Nachrichten (Deutschland/International/Maerkte) kommen aus dem oeffentlichen
   RSS-Feed der Tagesschau (https://www.tagesschau.de/xml/rss2) - RSS ist
   explizit fuer genau diese Art der Weiterverwendung gedacht (anders als die
   interne App-API der Tagesschau, die private, nicht-oeffentliche Nutzung
   verlangt - siehe https://github.com/bundesAPI/tagesschau-api). Trotzdem
   gilt: das ist ein automatischer Headline-Import, keine redaktionelle
   Auswahl. Die Einteilung laeuft ueber eine simple Stichwortsuche im Titel -
   eine grobe Heuristik, keine echte Kategorisierung.

3) EU-Meldungen kommen aus dem oeffentlichen Presseraum der Europaeischen
   Kommission (ec.europa.eu/commission/presscorner). Der Sprachparameter
   '?language=de' ist ein Best-Guess und nicht per Testlauf verifiziert -
   liefert der Feed englische statt deutsche Texte, muss die URL unten
   angepasst werden.

4) Jede Meldung bekommt jetzt zusaetzlich ihren Original-Link mit (aus dem
   RSS-Feld 'link') - darueber fuehrt der "Mehr ->"-Button auf der Seite
   zur Quelle.

5) Innovation-Spalte: heise-Newsticker (IT/Technik/Wissenschaft), gefiltert auf
   grosse Tech-/Chipkonzerne und Durchbruch-Begriffe (siehe
   INNOVATION_KEYWORDS) - Alltagsmeldungen werden dadurch aussortiert. Land
   spielt keine Rolle, nur ob ein bekannter Konzern/Begriff im Titel steht.

6) Leitzinsen (Fed/EZB) aendern sich nur nach Sitzungen (ca. 8x im Jahr).
   Dafuer lohnt sich kein automatischer Abruf - trag sie unten einfach von
   Hand nach, wenn es eine neue Entscheidung gab.

7) Zusaetzlich zu news.json legt das Skript taeglich einen Schnappschuss unter
   archive/YYYY-MM-DD.json ab (siehe update_archive()), damit die Seite auch
   fruehere Tage anzeigen kann. Die letzten 30 Tage werden aufgehoben, aeltere
   automatisch geloescht.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import yfinance as yf

NEWS_JSON_PATH = "news.json"
ARCHIVE_DIR = Path("archive")
ARCHIVE_INDEX_PATH = ARCHIVE_DIR / "index.json"
ARCHIVE_RETENTION_DAYS = 30  # aeltere Tage werden automatisch geloescht

TICKER_SYMBOLS = {
    "dax":       {"symbol": "^GDAXI",   "label": "DAX",        "unit": "Pkt."},
    "nasdaq100": {"symbol": "^NDX",     "label": "Nasdaq 100", "unit": "Pkt."},
    "sp500":     {"symbol": "^GSPC",    "label": "S&P 500",    "unit": "Pkt."},
    "brent":     {"symbol": "BZ=F",     "label": "Brent-Öl",   "unit": "$"},
    "gold":      {"symbol": "GC=F",     "label": "Gold",       "unit": "$"},
    "eurusd":    {"symbol": "EURUSD=X", "label": "EUR/USD",    "unit": ""},
}

# Von Hand pflegen, wenn Fed bzw. EZB tagen und den Zins aendern:
MANUAL_RATES = {
    "fed_rate": {"label": "Fed-Leitzins", "value": "3,50–3,75", "unit": "%", "change_pct": 0, "history": None},
    "ecb_rate": {"label": "EZB-Leitzins", "value": "2,40",       "unit": "%", "change_pct": 0, "history": None},
}

MARKT_KEYWORDS = ["aktie", "börse", "dax", "nasdaq", "zins", "leitzins", "konjunktur",
                   "inflation", "ölpreis", "wirtschaft", "fed", "ezb", "notenbank", "kurs"]
INTL_KEYWORDS = ["usa", "china", "iran", "israel", "ukraine", "russland", "gaza", "nahost",
                  "trump", "eu-", "brüssel", "nato", "krieg", "korea"]

RSS_FEED_URL = "https://www.tagesschau.de/xml/rss2"
EU_RSS_URL = "https://ec.europa.eu/commission/presscorner/api/rss?language=de"
HEISE_RSS_URL = "https://www.heise.de/newsticker/heise.rdf"
MAX_ITEMS_PER_COLUMN = 6
MAX_INNOVATION_ITEMS = 4
INNOVATION_CANDIDATE_POOL = 40  # so viele Feed-Eintraege werden nach Stichwort durchsucht

# Grosse Tech-/Chipkonzerne + generelle Durchbruch-Begriffe. Nur Meldungen, die
# mind. eins davon im Titel haben, gelten als "Innovation" - damit landen keine
# Alltags-Meldungen (Tarife, Fritzbox-Verfuegbarkeit usw.) in der Spalte.
INNOVATION_KEYWORDS = [
    "nvidia", "amd", "intel", "tsmc", "qualcomm", "apple", "google", "alphabet",
    "microsoft", "meta", "openai", "anthropic", "tesla", "spacex", "samsung",
    "amazon", "sap", "siemens", "asml", "ibm", "bytedance",
    "chip", "prozessor", "halbleiter", "ki-modell", "ki modell", "quantencomputer",
    "durchbruch", "raumfahrt", "rakete", "batterie", "akku-technik", "robotik",
    "biotech", "genom", "fusion", "forscher entwickeln",
]


def fetch_ticker():
    """Holt Kurs + 7-Tage-Verlauf je Symbol. Gibt bei Fehlern pro Symbol nichts
    zurueck, statt das ganze Update abzubrechen."""
    result = {}
    for key, meta in TICKER_SYMBOLS.items():
        try:
            hist = yf.Ticker(meta["symbol"]).history(period="8d")["Close"].dropna()
            if len(hist) < 2:
                raise ValueError("zu wenig Kursdaten zurueckbekommen")
            values = [round(float(v), 4 if v < 10 else 1) for v in hist.tolist()][-7:]
            change_pct = round((values[-1] - values[-2]) / values[-2] * 100, 2)
            result[key] = {
                "label": meta["label"],
                "value": values[-1],
                "unit": meta["unit"],
                "change_pct": change_pct,
                "history": values,
            }
        except Exception as exc:
            print(f"[warnung] Kurs '{key}' ({meta['symbol']}) nicht abrufbar: {exc}")
    result.update(MANUAL_RATES)
    return result


def categorize(title):
    t = title.lower()
    if any(k in t for k in MARKT_KEYWORDS):
        return "markt"
    if any(k in t for k in INTL_KEYWORDS):
        return "intl"
    return "de"


def fetch_news():
    """Liest den Tagesschau-RSS-Feed und verteilt die Meldungen grob auf
    Deutschland/International/Maerkte. Gibt (hero, columns) zurueck; hero ist
    die erste gefundene Meldung insgesamt."""
    columns = {"de": [], "intl": [], "markt": []}
    hero = None

    try:
        feed = feedparser.parse(RSS_FEED_URL)
        for entry in feed.entries:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            summary = re.sub("<[^<]+?>", "", entry.get("summary", "")).strip()
            link = entry.get("link") or None

            if hero is None:
                hero = {"eyebrow": "TOP-MELDUNG", "title": title, "text": summary[:400], "link": link}

            cat = categorize(title)
            if len(columns[cat]) < MAX_ITEMS_PER_COLUMN:
                columns[cat].append({"source": "Tagesschau", "title": title, "text": summary[:280], "link": link})

            if all(len(v) >= MAX_ITEMS_PER_COLUMN for v in columns.values()):
                break
    except Exception as exc:
        print(f"[warnung] Tagesschau-Feed nicht abrufbar: {exc}")

    return hero, columns


def fetch_eu_news():
    """Liest die EU-Kommission-Pressemitteilungen. Eigene, dedizierte Quelle -
    keine Stichwort-Heuristik noetig, alles hier ist per Definition EU-Politik."""
    items = []
    try:
        feed = feedparser.parse(EU_RSS_URL)
        for entry in feed.entries[:MAX_ITEMS_PER_COLUMN]:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            summary = re.sub("<[^<]+?>", "", entry.get("summary", "")).strip()
            items.append({
                "source": "EU-Kommission",
                "title": title,
                "text": summary[:280],
                "link": entry.get("link") or None,
            })
    except Exception as exc:
        print(f"[warnung] EU-Feed nicht abrufbar: {exc}")
    return items


def update_archive(data):
    """Legt einen taeglichen Schnappschuss unter archive/YYYY-MM-DD.json ab und
    pflegt archive/index.json (Liste der verfuegbaren Tage). Aeltere Tage als
    ARCHIVE_RETENTION_DAYS werden geloescht, damit das Repo nicht endlos waechst."""
    ARCHIVE_DIR.mkdir(exist_ok=True)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with open(ARCHIVE_DIR / f"{today_str}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    try:
        with open(ARCHIVE_INDEX_PATH, "r", encoding="utf-8") as f:
            dates = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        dates = []

    dates = sorted(set(dates) | {today_str})

    if len(dates) > ARCHIVE_RETENTION_DAYS:
        for old_date in dates[:-ARCHIVE_RETENTION_DAYS]:
            old_file = ARCHIVE_DIR / f"{old_date}.json"
            if old_file.exists():
                old_file.unlink()
        dates = dates[-ARCHIVE_RETENTION_DAYS:]

    with open(ARCHIVE_INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(dates, f, ensure_ascii=False, indent=2)


def fetch_innovation_news():
    """Liest den allgemeinen heise-Newsticker (IT/Technik/Wissenschaft) und
    behaelt nur Meldungen, die zu grossen Tech-/Chipkonzernen oder generellen
    Durchbruch-Begriffen passen (siehe INNOVATION_KEYWORDS) - der Feed selbst
    ist nicht vorgefiltert, sonst landen auch Alltagsmeldungen (Tarife,
    Router-Verfuegbarkeit usw.) in der Spalte. heise erlaubt die Uebernahme
    von RSS-Inhalten mit Link zum Original ausdruecklich
    (siehe https://www.heise.de/news-extern/news.html)."""
    items = []
    try:
        feed = feedparser.parse(HEISE_RSS_URL)
        for entry in feed.entries[:INNOVATION_CANDIDATE_POOL]:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            if not any(k in title.lower() for k in INNOVATION_KEYWORDS):
                continue
            summary = re.sub("<[^<]+?>", "", entry.get("summary", "")).strip()
            items.append({
                "source": "heise online",
                "title": title,
                "text": summary[:280],
                "link": entry.get("link") or None,
            })
            if len(items) >= MAX_INNOVATION_ITEMS:
                break
    except Exception as exc:
        print(f"[warnung] heise-Feed nicht abrufbar: {exc}")
    return items


def main():
    # Bestehende Datei laden, falls vorhanden - so bleibt z.B. ein alter
    # Kurswert erhalten, wenn der Abruf fuer genau dieses Symbol heute fehlschlaegt.
    try:
        with open(NEWS_JSON_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        existing = {"ticker": {}, "hero": None, "columns": {}}

    fresh_ticker = fetch_ticker()
    merged_ticker = {**existing.get("ticker", {}), **fresh_ticker}

    hero, columns = fetch_news()
    columns["eu"] = fetch_eu_news()
    columns["innovation"] = fetch_innovation_news()

    existing_columns = existing.get("columns", {})
    merged_columns = {
        cat: (columns.get(cat) or existing_columns.get(cat, []))
        for cat in ("de", "intl", "eu", "markt", "innovation")
    }

    data = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "ticker": merged_ticker,
        "hero": hero or existing.get("hero"),
        "columns": merged_columns,
    }

    with open(NEWS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    update_archive(data)

    print("news.json aktualisiert:", data["updated"])


if __name__ == "__main__":
    main()
