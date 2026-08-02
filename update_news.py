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

2) Nachrichten kommen aus dem oeffentlichen RSS-Feed der Tagesschau
   (https://www.tagesschau.de/xml/rss2) - RSS ist explizit fuer genau diese
   Art der Weiterverwendung gedacht (anders als die interne App-API der
   Tagesschau, die private, nicht-oeffentliche Nutzung verlangt - siehe
   https://github.com/bundesAPI/tagesschau-api). Trotzdem gilt: das ist ein
   automatischer Headline-Import, keine redaktionelle Auswahl. Die Einteilung
   in "Deutschland" / "International" / "Maerkte" laeuft ueber eine simple
   Stichwortsuche im Titel - eine grobe Heuristik, keine echte Kategorisierung.

3) Leitzinsen (Fed/EZB) aendern sich nur nach Sitzungen (ca. 8x im Jahr).
   Dafuer lohnt sich kein automatischer Abruf - trag sie unten einfach von
   Hand nach, wenn es eine neue Entscheidung gab.
"""

import json
import re
from datetime import datetime, timezone

import feedparser
import yfinance as yf

NEWS_JSON_PATH = "news.json"

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
MAX_ITEMS_PER_COLUMN = 6


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
    """Liest den Tagesschau-RSS-Feed und verteilt die Meldungen grob auf die
    drei Spalten. Gibt (hero, columns) zurueck; hero ist die erste gefundene
    Meldung insgesamt."""
    feed = feedparser.parse(RSS_FEED_URL)
    columns = {"de": [], "intl": [], "markt": []}
    hero = None

    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        summary = re.sub("<[^<]+?>", "", entry.get("summary", "")).strip()

        if hero is None:
            hero = {"eyebrow": "TOP-MELDUNG", "title": title, "text": summary[:400]}

        cat = categorize(title)
        if len(columns[cat]) < MAX_ITEMS_PER_COLUMN:
            columns[cat].append({"source": "Tagesschau", "title": title, "text": summary[:280]})

        if all(len(v) >= MAX_ITEMS_PER_COLUMN for v in columns.values()):
            break

    return hero, columns


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

    data = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "ticker": merged_ticker,
        "hero": hero or existing.get("hero"),
        "columns": columns if any(columns.values()) else existing.get("columns", {}),
    }

    with open(NEWS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("news.json aktualisiert:", data["updated"])


if __name__ == "__main__":
    main()
