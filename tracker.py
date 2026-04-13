"""
Dividend Change Tracker
-----------------------
Laedt taeglich Dividendendaten via FMP API, vergleicht mit gespeicherten
Werten und schickt eine Telegram-Nachricht bei Aenderungen.
"""

import os
import json
import sqlite3
import requests
import yaml
from datetime import datetime, date
from pathlib import Path

# ── Konfiguration ────────────────────────────────────────────────────────────

FMP_KEY         = os.environ.get("FMP_KEY", "")
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE_URL  = "https://financialmodelingprep.com/stable"
DB_PATH   = Path("dividend_history.db")
WATCHLIST = Path("watchlist.yaml")

# Minimale prozentuale Aenderung fuer einen Alert (z.B. 0.5 = 0,5%)
MIN_CHANGE_PCT = 0.5

# ── Datenbank ─────────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS dividends (
            ticker      TEXT NOT NULL,
            ex_date     TEXT NOT NULL,
            amount      REAL NOT NULL,
            fetched_at  TEXT NOT NULL,
            PRIMARY KEY (ticker, ex_date)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT NOT NULL,
            ex_date     TEXT NOT NULL,
            old_amount  REAL,
            new_amount  REAL NOT NULL,
            change_pct  REAL,
            alerted_at  TEXT NOT NULL
        )
    """)
    con.commit()
    return con


def get_last_two(con, ticker):
    """Gibt die zwei neuesten gespeicherten Dividenden fuer einen Ticker zurueck."""
    rows = con.execute(
        "SELECT ex_date, amount FROM dividends WHERE ticker = ? ORDER BY ex_date DESC LIMIT 2",
        (ticker,)
    ).fetchall()
    return rows  # [(ex_date, amount), ...]


def upsert_dividend(con, ticker, ex_date, amount):
    con.execute(
        "INSERT OR REPLACE INTO dividends (ticker, ex_date, amount, fetched_at) VALUES (?, ?, ?, ?)",
        (ticker, ex_date, amount, datetime.utcnow().isoformat())
    )
    con.commit()


def save_alert(con, ticker, ex_date, old_amount, new_amount, change_pct):
    con.execute(
        "INSERT INTO alerts (ticker, ex_date, old_amount, new_amount, change_pct, alerted_at) VALUES (?, ?, ?, ?, ?, ?)",
        (ticker, ex_date, old_amount, new_amount, change_pct, datetime.utcnow().isoformat())
    )
    con.commit()

# ── FMP API ───────────────────────────────────────────────────────────────────

def fetch_dividend_history(ticker):
    """
    Holt die Dividendenhistorie eines Tickers vom neuen FMP /stable/ Endpunkt.
    Gibt eine Liste von Dicts zurueck oder None bei Fehler/kein Zugang.

    Neuer Endpunkt (seit Aug 2025):
      GET /stable/dividends-company?symbol=MSFT&apikey=KEY
    Antwort: Liste von Objekten mit date, adjDividend, dividend, ...
    """
    url = f"{BASE_URL}/dividends-company"
    try:
        resp = requests.get(url, params={"symbol": ticker, "apikey": FMP_KEY}, timeout=15)
        if resp.status_code == 403:
            print(f"  [{ticker}] kein FMP-Zugang (403) – uebersprungen")
            return None
        if resp.status_code == 401:
            print(f"  [{ticker}] API-Key ungueltig (401) – FMP_KEY pruefen")
            return None
        if resp.status_code != 200:
            print(f"  [{ticker}] HTTP {resp.status_code} – uebersprungen")
            return None
        data = resp.json()
        # Neue API gibt direkt eine Liste zurueck (kein "historical"-Wrapper)
        if isinstance(data, dict):
            # Fehler-Antwort z.B. {"Error Message": "..."}
            err = data.get("Error Message", data.get("message", ""))
            if err:
                print(f"  [{ticker}] API-Fehler: {err}")
            return None
        if not isinstance(data, list) or len(data) == 0:
            print(f"  [{ticker}] keine Dividendendaten gefunden")
            return None
        # Sortiert nach date absteigend (neueste zuerst)
        data.sort(key=lambda x: x.get("date", ""), reverse=True)
        return data
    except requests.RequestException as e:
        print(f"  [{ticker}] Netzwerkfehler: {e}")
        return None

# ── Aenderungs-Erkennung ──────────────────────────────────────────────────────

def detect_change(con, ticker, history):
    """
    Vergleicht neueste API-Dividende mit dem gespeicherten Wert.
    Gibt ein Change-Dict zurueck oder None wenn keine relevante Aenderung.
    """
    if not history:
        return None

    latest = history[0]
    ex_date = latest.get("date", "")
    # FMP liefert 'adjDividend' (split-adjustiert) und 'dividend' (raw)
    new_amount = latest.get("adjDividend") or latest.get("dividend", 0)

    if not new_amount or new_amount <= 0:
        return None

    # Wert in DB speichern / aktualisieren
    upsert_dividend(con, ticker, ex_date, new_amount)

    # Letzten zwei DB-Eintraege holen um Aenderung zu erkennen
    rows = get_last_two(con, ticker)
    if len(rows) < 2:
        # Erster Eintrag fuer diesen Ticker – Baseline gesetzt, kein Alert
        print(f"  [{ticker}] Baseline gesetzt: {new_amount:.4f} ({ex_date})")
        return None

    prev_ex_date, prev_amount = rows[1]

    if prev_amount == 0:
        return None

    change_pct = (new_amount - prev_amount) / prev_amount * 100

    if abs(change_pct) < MIN_CHANGE_PCT:
        print(f"  [{ticker}] keine wesentliche Aenderung ({change_pct:+.2f}%)")
        return None

    direction = "Erhoehung" if change_pct > 0 else "Senkung"
    print(f"  [{ticker}] *** Dividenden-{direction}: {change_pct:+.2f}% ***")

    save_alert(con, ticker, ex_date, prev_amount, new_amount, change_pct)

    return {
        "ticker":      ticker,
        "direction":   direction,
        "change_pct":  change_pct,
        "new_amount":  new_amount,
        "old_amount":  prev_amount,
        "ex_date":     ex_date,
        "prev_ex_date": prev_ex_date,
    }

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [Telegram] Token oder Chat-ID fehlt – Nachricht nicht gesendet")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print("  [Telegram] Nachricht gesendet")
        else:
            print(f"  [Telegram] Fehler {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        print(f"  [Telegram] Netzwerkfehler: {e}")


def format_alert_message(changes):
    today = date.today().strftime("%d.%m.%Y")
    lines = [f"<b>Dividenden-Alert – {today}</b>\n"]

    for c in changes:
        arrow = "↑" if c["change_pct"] > 0 else "↓"
        lines.append(
            f"{arrow} <b>{c['ticker']}</b>  {c['change_pct']:+.1f}%\n"
            f"   Neu:  <code>{c['new_amount']:.4f}</code>  (Ex-Date: {c['ex_date']})\n"
            f"   Vorher: <code>{c['old_amount']:.4f}</code>  (Ex-Date: {c['prev_ex_date']})\n"
        )

    lines.append(f"\n{len(changes)} Aenderung(en) erkannt.")
    return "\n".join(lines)

# ── Hauptprogramm ─────────────────────────────────────────────────────────────

def load_watchlist():
    if not WATCHLIST.exists():
        raise FileNotFoundError(f"{WATCHLIST} nicht gefunden")
    with open(WATCHLIST) as f:
        data = yaml.safe_load(f)
    tickers = data.get("tickers", [])
    if not tickers:
        raise ValueError("Watchlist ist leer")
    return [t.strip().upper() for t in tickers]


def main():
    print(f"\n=== Dividend Tracker – {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} ===\n")

    if not FMP_KEY:
        raise EnvironmentError("FMP_KEY nicht gesetzt")

    tickers = load_watchlist()
    print(f"Tracke {len(tickers)} Ticker: {', '.join(tickers)}\n")

    con = init_db()
    changes = []

    for ticker in tickers:
        print(f"[{ticker}]")
        history = fetch_dividend_history(ticker)
        change  = detect_change(con, ticker, history)
        if change:
            changes.append(change)

    print(f"\n──────────────────────────────")
    print(f"Ergebnis: {len(changes)} Aenderung(en) erkannt\n")

    if changes:
        message = format_alert_message(changes)
        print("Telegram-Nachricht:\n")
        print(message)
        send_telegram(message)
    else:
        print("Keine Dividendenaenderungen – kein Alert gesendet.")

    con.close()
    print("\n=== Fertig ===\n")


if __name__ == "__main__":
    main()
