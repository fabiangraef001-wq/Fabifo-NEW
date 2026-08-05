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

5) Leitzinsen (Fed/EZB) aendern sich nur nach Sitzungen (ca. 8x im Jahr).
   Dafuer lohnt sich kein automatischer Abruf - trag sie unten einfach von
   Hand nach, wenn es eine neue Entscheidung gab.

6) Zusaetzlich zu news.json legt das Skript taeglich einen Schnappschuss unter
   archive/YYYY-MM-DD.json ab (siehe update_archive()), damit die Seite auch
   fruehere Tage anzeigen kann. Die letzten 30 Tage werden aufgehoben, aeltere
   automatisch geloescht.

7) assets.json enthaelt die ETF-Seite (9 gezielt gepruefte Fonds, siehe
   ETF_STATIC_INFO) und die Aktien&Krypto-Seite (Bitcoin/Rheinmetall/Nvidia,
   siehe STOCK_STATIC_INFO). TER/Fondsgroesse/Kategorie sind von Hand
   gepflegt, Kurs+Verlauf werden taeglich frisch abgerufen.

8) "Unabhängige News" ist eine eigene Ansicht (news.json-Feld "independent"),
   gespeist aus netzpolitik.org (RSS, CC-BY-NC-SA 4.0 - nicht-kommerzielle
   Weiterverwendung mit Nennung ausdruecklich erlaubt). Bewusst getrennt von
   den redaktionellen News-Spalten, da es eine andere Quelle/Perspektive ist.
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

# Welche Ticker-Kachel zu welchen Stichworten in den News passt. Nur Kacheln,
# deren Stichwort heute tatsaechlich in einer Schlagzeile/einem Text vorkommt,
# werden im Ticker angezeigt - der Ticker soll sich nach den Nachrichten
# richten statt immer alles fest zu zeigen.
TICKER_RELEVANCE_KEYWORDS = {
    "dax":       ["dax"],
    "nasdaq100": ["nasdaq"],
    "sp500":     ["s&p 500", "s&p500", "wall street"],
    "brent":     ["öl", "ölpreis", "brent", "opec"],
    "gold":      ["gold"],
    "eurusd":    ["dollar", "euro-dollar", "eur/usd", "wechselkurs", "dollarkurs"],
    "fed_rate":  ["fed", "notenbank", "zinsentscheidung", "federal reserve", "leitzins"],
    "ecb_rate":  ["ezb", "europäische zentralbank"],
}
# Falls an einem Tag gar kein Stichwort passt (z.B. sehr ruhiger Nachrichtentag),
# damit der Ticker nicht komplett leer ist:
TICKER_RELEVANCE_FALLBACK = ["dax", "fed_rate", "ecb_rate"]


def compute_relevant_ticker_keys(hero, columns):
    """Durchsucht Titel+Text aller heutigen Meldungen (Hero + alle Spalten)
    nach den Stichworten aus TICKER_RELEVANCE_KEYWORDS und gibt die Liste der
    dazu passenden Ticker-Kacheln zurueck."""
    texts = []
    if hero:
        texts.append(hero.get("title", ""))
        texts.append(hero.get("text", ""))
    for items in columns.values():
        for item in items:
            texts.append(item.get("title", ""))
            texts.append(item.get("text", ""))
    combined = " ".join(texts).lower()

    relevant = [key for key, keywords in TICKER_RELEVANCE_KEYWORDS.items()
                if any(kw in combined for kw in keywords)]
    return relevant or TICKER_RELEVANCE_FALLBACK

ASSETS_JSON_PATH = "assets.json"

# WICHTIGER HINWEIS: Diese 9 ETFs wurden gezielt recherchiert und gegen mehrere
# Quellen (justETF, Deutsche Boerse, Yahoo Finance) geprueft - Ticker, ISIN,
# TER und Fondsgroesse sind Stand August 2026 verifiziert. Bewusst reduziert
# von urspruenglich 25 auf diese 9 geprueften, statt viele ungeprüfte
# Positionen zu zeigen. TER/Fondsgroesse aendern sich selten und werden von
# Hand gepflegt; Kurs/Verlauf holt das Skript taeglich automatisch. Schlaegt
# ein Ticker bei Yahoo Finance trotzdem mal fehl, wird nur diese Kachel ohne
# Kurs angezeigt - der Rest laeuft normal weiter.
ETF_STATIC_INFO = {
    "gerd":    {"name": "L&G Gerd Kommer Multifactor",          "ticker": "GERD.DE", "isin": "IE0001UQQ933", "ter": 0.45, "fund_size_mrd": 1.35,  "category": "Welt (Multifaktor)", "distribution": "thesaurierend"},
    "vwce":    {"name": "Vanguard FTSE All-World",              "ticker": "VWCE.DE", "isin": "IE00BK5BQT80", "ter": 0.22, "fund_size_mrd": 17.0,  "category": "Welt",            "distribution": "thesaurierend"},
    "eunl":    {"name": "iShares Core MSCI World",              "ticker": "EUNL.DE", "isin": "IE00B4L5Y983", "ter": 0.20, "fund_size_mrd": 126.0, "category": "Welt",            "distribution": "thesaurierend"},
    "xmwo":    {"name": "Xtrackers MSCI World",                 "ticker": "XDWD.DE", "isin": "IE00BJ0KDQ92", "ter": 0.12, "fund_size_mrd": 22.0,  "category": "Welt",            "distribution": "thesaurierend"},
    "xaix":    {"name": "Xtrackers Artificial Intelligence & Big Data", "ticker": "XAIX.DE", "isin": "IE00BGV5VN51", "ter": 0.35, "fund_size_mrd": 7.45, "category": "Thema (Tech)",  "distribution": "thesaurierend"},
    "sxr8":    {"name": "iShares Core S&P 500",                 "ticker": "SXR8.DE", "isin": "IE0031442068", "ter": 0.07, "fund_size_mrd": 85.0,  "category": "USA",             "distribution": "thesaurierend"},
    "is3n":    {"name": "iShares Core MSCI EM IMI",             "ticker": "IS3N.DE", "isin": "IE00BKM4GZ66", "ter": 0.18, "fund_size_mrd": 25.0,  "category": "Schwellenländer", "distribution": "thesaurierend"},
    "eqqq":    {"name": "Invesco Nasdaq-100",                   "ticker": "EQQQ.DE", "isin": "IE0032077012", "ter": 0.30, "fund_size_mrd": 11.0,  "category": "USA Tech",        "distribution": "ausschüttend"},
    "gold":    {"name": "Xetra-Gold",                           "ticker": "4GLD.DE", "isin": "DE000A0S9GB0", "ter": 0.36, "fund_size_mrd": 20.0,  "category": "Rohstoffe",       "distribution": "-"},
}

STOCK_STATIC_INFO = {
    # Bitcoin-ETP-Wahl (ETC Group BTCetc) mit normaler Sorgfalt recherchiert,
    # aber nicht so tiefgehend gegengeprueft wie die 9 ETFs oben. Rheinmetall
    # (RHM) und Nvidia (NVDA) sind sehr bekannte Standard-Ticker, ISIN-Level
    # nicht extra verifiziert. Nvidia notiert in USD (NASDAQ), daher eigenes
    # "currency"-Feld - die anderen beiden sind Xetra/EUR und brauchen keins.
    "bitcoin":    {"name": "Bitcoin (BTCetc ETP)", "ticker": "BTCE.DE", "category": "Krypto"},
    "rheinmetall":{"name": "Rheinmetall",          "ticker": "RHM.DE",  "category": "Aktie"},
    "nvidia":     {"name": "Nvidia",               "ticker": "NVDA",    "category": "Aktie", "currency": "$"},
}

MARKT_KEYWORDS = ["aktie", "börse", "dax", "nasdaq", "zins", "leitzins", "konjunktur",
                   "inflation", "ölpreis", "wirtschaft", "fed", "ezb", "notenbank", "kurs"]
INTL_KEYWORDS = ["usa", "china", "iran", "israel", "ukraine", "russland", "gaza", "nahost",
                  "trump", "eu-", "brüssel", "nato", "krieg", "korea"]

RSS_FEED_URL = "https://www.tagesschau.de/xml/rss2"
EU_RSS_URL = "https://ec.europa.eu/commission/presscorner/api/rss?language=de"
NETZPOLITIK_RSS_URL = "https://netzpolitik.org/feed/"
MAX_ITEMS_PER_COLUMN = 6
MAX_INDEPENDENT_ITEMS = 6


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


def fetch_price_history(ticker):
    """Gemeinsame Kurslogik fuer ETFs/Aktien: Kurs + 7-Tage-Verlauf + Tages-
    veraenderung. Gibt (price, change_pct, history) zurueck, oder (None, None,
    None) wenn der Ticker fehlschlaegt."""
    try:
        hist = yf.Ticker(ticker).history(period="8d")["Close"].dropna()
        if len(hist) < 2:
            raise ValueError("zu wenig Kursdaten zurueckbekommen")
        values = [round(float(v), 2) for v in hist.tolist()][-7:]
        change_pct = round((values[-1] - values[-2]) / values[-2] * 100, 2)
        return values[-1], change_pct, values
    except Exception as exc:
        print(f"[warnung] Kurs '{ticker}' nicht abrufbar: {exc}")
        return None, None, None


def fetch_assets():
    """Baut die Listen fuer die ETF- und die Aktien&Krypto-Seite: statische
    Fondsfakten (siehe ETF_STATIC_INFO/STOCK_STATIC_INFO) + taeglich frisch
    abgerufener Kurs/Verlauf."""
    etfs = []
    for key, info in ETF_STATIC_INFO.items():
        entry = {**info, "key": key}
        entry["price"], entry["change_pct"], entry["history"] = fetch_price_history(info["ticker"])
        etfs.append(entry)

    stocks = []
    for key, info in STOCK_STATIC_INFO.items():
        entry = {**info, "key": key}
        entry["price"], entry["change_pct"], entry["history"] = fetch_price_history(info["ticker"])
        stocks.append(entry)

    return etfs, stocks


def fetch_independent_news():
    """Liest netzpolitik.org (unabhaengiges, spendenfinanziertes Investigativ-
    Medium, kein oeffentlich-rechtlicher Sender). Feed steht unter
    CC-BY-NC-SA 4.0 - explizit zur nicht-kommerziellen Weiterverwendung mit
    Nennung gedacht, was genau auf Fabifo zutrifft. Bewusst als eigene,
    separate Ansicht (nicht als Spalte in den News), da es eine bewusst
    andere Perspektive/Quelle als Tagesschau/EU-Kommission sein soll."""
    items = []
    try:
        feed = feedparser.parse(NETZPOLITIK_RSS_URL)
        for entry in feed.entries[:MAX_INDEPENDENT_ITEMS]:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            summary = re.sub("<[^<]+?>", "", entry.get("summary", "")).strip()
            items.append({
                "source": "netzpolitik.org",
                "title": title,
                "text": summary[:280],
                "link": entry.get("link") or None,
            })
    except Exception as exc:
        print(f"[warnung] netzpolitik.org-Feed nicht abrufbar: {exc}")
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

    existing_columns = existing.get("columns", {})
    merged_columns = {
        cat: (columns.get(cat) or existing_columns.get(cat, []))
        for cat in ("de", "intl", "eu", "markt")
    }

    data = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "ticker": merged_ticker,
        "ticker_relevant": compute_relevant_ticker_keys(hero or existing.get("hero"), merged_columns),
        "hero": hero or existing.get("hero"),
        "columns": merged_columns,
        "independent": fetch_independent_news() or existing.get("independent", []),
    }

    with open(NEWS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    update_archive(data)

    etfs, stocks = fetch_assets()
    assets_data = {"updated": data["updated"], "etfs": etfs, "stocks": stocks}
    with open(ASSETS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(assets_data, f, ensure_ascii=False, indent=2)

    print("news.json aktualisiert:", data["updated"])


if __name__ == "__main__":
    main()
