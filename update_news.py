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

5) Innovation-Spalte: allgemeiner heise-Newsticker (IT/Technik/Wissenschaft).
   heise erlaubt RSS-Uebernahme mit Link zum Original ausdruecklich. Keine
   Filterung nach Land oder "echtem Durchbruch" - einfach die aktuellsten
   Meldungen des Feeds.

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


def fetch_ticker():
    """Holt Kurs + 7-Tage-Verlauf je Symbol. Gibt bei Fehlern pro Symbol nichts
    zurueck, statt das ganze Update abzubrechen."""
    result = {}
    for key, meta in TICKER_SYMBOLS.items():
        try:
            hist = yf.Ticker(meta["symbol"]).history(period="8d")["Close"].dropna()
            if len(hist) < 2:
                raise ValueError("zu wenig Kursdaten zurueckbekommen")
            values =
