"""
Dividend Tracker
Kombiniert SEC EDGAR (fruehzeitige Ankuendigungen) + yfinance (Verlauf)
"""

import os, re, json, sqlite3, time, urllib.request, yaml
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

# ── Konfiguration ─────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DB_PATH          = Path("dividend_history.db")
WATCHLIST        = Path("watchlist.yaml")
MIN_CHANGE_PCT   = 0.5
SEC_UA           = "DividendTracker private-use admin@example.com"

# ── Datenbank ─────────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS dividends (
            ticker   TEXT NOT NULL,
            ex_date  TEXT NOT NULL,
            amount   REAL NOT NULL,
            source   TEXT DEFAULT 'yfinance',
            saved_at TEXT NOT NULL,
            PRIMARY KEY (ticker, ex_date)
        );
        CREATE TABLE IF NOT EXISTS edgar_seen (
            adsh     TEXT PRIMARY KEY,
            ticker   TEXT NOT NULL,
            seen_at  TEXT NOT NULL
        );
    """)
    con.commit()
    return con

def get_stored(con, ticker, limit=2):
    return con.execute(
        "SELECT ex_date, amount FROM dividends WHERE ticker=? ORDER BY ex_date DESC LIMIT ?",
        (ticker, limit)
    ).fetchall()

def upsert(con, ticker, ex_date, amount, source="yfinance"):
    con.execute(
        "INSERT OR REPLACE INTO dividends (ticker, ex_date, amount, source, saved_at) VALUES (?,?,?,?,?)",
        (ticker, ex_date, amount, source, datetime.now(timezone.utc).isoformat())
    )
    con.commit()

def seen(con, adsh):
    return bool(con.execute("SELECT 1 FROM edgar_seen WHERE adsh=?", (adsh,)).fetchone())

def mark_seen(con, adsh, ticker):
    con.execute(
        "INSERT OR IGNORE INTO edgar_seen (adsh, ticker, seen_at) VALUES (?,?,?)",
        (adsh, ticker, datetime.now(timezone.utc).isoformat())
    )
    con.commit()

# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": SEC_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def strip_html(raw):
    text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="ignore")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def fmt(amount):
    return f"{amount:.4f}".rstrip("0").rstrip(".")

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [Telegram] Keine Zugangsdaten gesetzt")
        return
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            ok = json.loads(r.read()).get("ok", False)
            print(f"  [Telegram] {'✓ Gesendet' if ok else '✗ Fehler'}")
    except Exception as e:
        print(f"  [Telegram] Fehler: {e}")

# ── Nachrichtenformat ─────────────────────────────────────────────────────────

def msg_edgar(ticker, company, amount, old_amount, change_pct, filing_date):
    arrow = "↑" if (change_pct or 0) >= 0 else "↓"
    lines = [f"📢 <b>{company} ({ticker})</b>"]
    lines.append(f"{arrow} Dividendenankuendigung via SEC EDGAR")
    lines.append(f"   Neue Dividende: <b>${fmt(amount)}</b> pro Aktie")
    if old_amount:
        lines.append(f"   Vorherige Dividende: ${fmt(old_amount)}")
        if change_pct is not None:
            word = "Erhoehung" if change_pct > 0 else "Senkung"
            lines.append(f"   ➜ Das ist eine <b>{word} von {abs(change_pct):.1f}%</b>")
    lines.append(f"   Eingereicht am: {filing_date}")
    return "\n".join(lines)

def msg_yfinance(ticker, amount, old_amount, change_pct, ex_date, prev_date):
    arrow = "↑" if change_pct > 0 else "↓"
    word  = "Erhoehung" if change_pct > 0 else "Senkung"
    lines = [f"{arrow} <b>{ticker} — Dividenden{word} ({change_pct:+.1f}%)</b>"]
    lines.append(f"   Neue Dividende: <b>${fmt(amount)}</b>  (Ex-Date: {ex_date})")
    lines.append(f"   Vorher:         ${fmt(old_amount)}  (Ex-Date: {prev_date})")
    return "\n".join(lines)

def build_message(edgar_alerts, yf_alerts):
    today = date.today().strftime("%d.%m.%Y")
    parts = [f"<b>Dividend Tracker — {today}</b>"]
    if edgar_alerts:
        parts.append("\n<b>── Neue Ankuendigungen (EDGAR) ──</b>")
        parts.extend(edgar_alerts)
    if yf_alerts:
        parts.append("\n<b>── Bestaetigt via Kursdaten ──</b>")
        parts.extend(yf_alerts)
    parts.append(f"\n{len(edgar_alerts) + len(yf_alerts)} Meldung(en) heute.")
    return "\n\n".join(parts)

# ── SEC EDGAR ─────────────────────────────────────────────────────────────────

_cik_cache = {}

def get_cik_map():
    global _cik_cache
    if _cik_cache:
        return _cik_cache
    try:
        print("  Lade SEC CIK-Verzeichnis...")
        data = json.loads(http_get("https://www.sec.gov/files/company_tickers.json"))
        _cik_cache = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in data.values()}
        print(f"  {len(_cik_cache)} Ticker im Verzeichnis")
    except Exception as e:
        print(f"  CIK-Laden fehlgeschlagen: {e}")
    return _cik_cache

AMOUNT_RE = [
    r"declared\s+a\s+(?:regular\s+)?(?:quarterly|monthly|annual|semi.annual)?\s*(?:cash\s+)?dividend\s+of\s+\$?([\d.]+)",
    r"(?:quarterly|monthly|annual)\s+(?:cash\s+)?dividend\s+of\s+\$?([\d.]+)\s+per\s+(?:common\s+)?share",
    r"\$?([\d.]+)\s+per\s+(?:common\s+)?share[^.]{0,60}dividend",
]
CHANGE_RE = [
    (r"(\d+(?:\.\d+)?)\s*(?:percent|%)\s+increase", "increase"),
    (r"increase[^.]{0,30}(\d+(?:\.\d+)?)\s*(?:percent|%)", "increase"),
    (r"(\d+(?:\.\d+)?)\s*(?:percent|%)\s+decrease", "decrease"),
    (r"increase[d]?\s+(?:the\s+)?dividend\s+by\s+(\d+(?:\.\d+)?)", "increase"),
]

def extract_amount(text):
    for pat in AMOUNT_RE:
        m = re.search(pat, text, re.I)
        if m:
            try:
                v = float(m.group(1))
                if 0.001 < v < 500:
                    return v
            except ValueError:
                pass
    return None

def extract_change(text):
    for pat, direction in CHANGE_RE:
        m = re.search(pat, text, re.I)
        if m:
            try:
                pct = float(m.group(1))
                return pct if direction == "increase" else -pct
            except ValueError:
                pass
    return None

def scan_edgar(con, ticker, cik, company_name, lookback_days=2):
    alerts = []
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    try:
        sub = json.loads(http_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
    except Exception as e:
        print(f"    EDGAR-Fehler: {e}")
        return alerts

    rec   = sub["filings"]["recent"]
    forms = rec.get("form", [])
    dates = rec.get("filingDate", [])
    accs  = rec.get("accessionNumber", [])
    pdocs = rec.get("primaryDocument", [""] * len(forms))

    for i in range(len(forms)):
        if forms[i] != "8-K":
            continue
        if dates[i] < cutoff:
            break
        adsh = accs[i]
        if seen(con, adsh):
            continue

        doc = pdocs[i] if i < len(pdocs) else ""
        if not doc:
            mark_seen(con, adsh, ticker)
            continue

        cik_int   = int(cik)
        acc_path  = adsh.replace("-", "")
        doc_url   = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_path}/{doc}"

        try:
            text = strip_html(http_get(doc_url, timeout=12))
        except Exception:
            mark_seen(con, adsh, ticker)
            continue

        time.sleep(0.12)

        if "dividend" not in text.lower():
            mark_seen(con, adsh, ticker)
            continue

        amount = extract_amount(text)
        mark_seen(con, adsh, ticker)

        if amount is None:
            continue

        stored    = get_stored(con, ticker, limit=1)
        old_amount = stored[0][1] if stored else None

        if old_amount and old_amount > 0:
            calc_pct = (amount - old_amount) / old_amount * 100
        else:
            calc_pct = None

        reported_pct = extract_change(text) or calc_pct

        is_new    = old_amount is None
        is_change = reported_pct is not None and abs(reported_pct) >= MIN_CHANGE_PCT

        if not is_new and not is_change:
            print(f"    EDGAR: ${amount:.4f} — keine Aenderung")
            continue

        pct_str = f"{reported_pct:+.1f}%" if reported_pct else "neu"
        print(f"    *** EDGAR-Ankuendigung: ${amount:.4f} ({pct_str}) ***")

        alerts.append(msg_edgar(ticker, company_name, amount, old_amount, reported_pct, dates[i]))

    return alerts

# ── yfinance ──────────────────────────────────────────────────────────────────

def check_yfinance(con, ticker):
    try:
        import yfinance as yf
        import pandas as pd
        raw = yf.Ticker(ticker).dividends
        if raw is None or (hasattr(raw, 'empty') and raw.empty):
            return None
        # yfinance >= 0.2.x gibt DataFrame zurueck, aeltere Versionen Series
        if isinstance(raw, pd.DataFrame):
            # DataFrame: Index = Timestamp, erste Spalte = Betrag
            col = raw.columns[0]
            items = [(idx, float(row[col])) for idx, row in raw.iterrows()]
        else:
            # Series (aelteres Format)
            items = [(ts, float(v)) for ts, v in raw.items()]
        # Index kann Timestamp oder String sein
        history = []
        for ts, amount in items:
            if hasattr(ts, 'date'):
                ex_date = ts.date().isoformat()
            else:
                ex_date = str(ts)[:10]
            history.append((ex_date, amount))
        history.sort(reverse=True)
    except Exception as e:
        print(f"    yfinance-Fehler: {e}")
        return None

    if not history:
        return None

    latest_date, latest_amount = history[0]

    # Pruefen ob der neueste Ex-Date bereits bekannt ist
    existing = get_stored(con, ticker, limit=1)

    if not existing:
        # Erster Lauf: die zwei neuesten Ex-Dates als Baseline speichern
        for ex_date, amount in history[:2]:
            upsert(con, ticker, ex_date, amount)
        rows = get_stored(con, ticker, limit=2)
        if len(rows) < 2:
            print(f"    Baseline gesetzt: ${latest_amount:.4f} ({latest_date})")
            return None
        # Ersten Vergleich direkt aus gespeicherten Daten machen
        curr_date, curr_amount = rows[0]
        prev_date, prev_amount = rows[1]
    else:
        # Folgelaeufe: nur neuesten Ex-Date speichern
        upsert(con, ticker, latest_date, latest_amount)
        rows = get_stored(con, ticker, limit=2)
        if len(rows) < 2:
            print(f"    Baseline gesetzt: ${latest_amount:.4f} ({latest_date})")
            return None
        curr_date, curr_amount = rows[0]
        prev_date, prev_amount = rows[1]
        # Kein Alert wenn der neueste Ex-Date derselbe wie beim letzten Lauf
        if curr_date == existing[0][0] and curr_amount == existing[0][1]:
            print(f"    Unveraendert: ${curr_amount:.4f} (+0.00%)")
            return None

    if prev_amount == 0:
        return None

    pct = (curr_amount - prev_amount) / prev_amount * 100

    if abs(pct) < MIN_CHANGE_PCT:
        print(f"    Unveraendert: ${curr_amount:.4f} ({pct:+.2f}%)")
        return None

    direction = "Erhoehung" if pct > 0 else "Senkung"
    print(f"    *** {direction}: {pct:+.2f}% ***")
    return msg_yfinance(ticker, curr_amount, prev_amount, pct, curr_date, prev_date)

# ── Watchlist ─────────────────────────────────────────────────────────────────

def load_watchlist():
    with open(WATCHLIST) as f:
        data = yaml.safe_load(f)
    return [str(t).strip().upper() for t in data.get("tickers", [])]

# ── Hauptprogramm ─────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*52}")
    print(f"  Dividend Tracker  —  {now}")
    print(f"{'='*52}\n")

    tickers = load_watchlist()
    print(f"Watchlist: {len(tickers)} Ticker\n")

    con     = init_db()
    cik_map = get_cik_map()

    edgar_alerts = []
    yf_alerts    = []
    edgar_tickers = set()

    # ── Phase 1: EDGAR ───────────────────────────────────────────────────────
    print("── Phase 1: EDGAR (Ankuendigungen) ──────────────────")
    for ticker in tickers:
        cik = cik_map.get(ticker)
        if not cik:
            print(f"  [{ticker}] kein US-CIK — wird nur via yfinance geprueft")
            continue

        try:
            sub          = json.loads(http_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
            company_name = sub.get("name", ticker)
        except Exception:
            company_name = ticker

        print(f"  [{ticker}] {company_name}")
        alerts = scan_edgar(con, ticker, cik, company_name)
        if alerts:
            edgar_alerts.extend(alerts)
            edgar_tickers.add(ticker)
        time.sleep(0.1)

    # ── Phase 2: yfinance ─────────────────────────────────────────────────────
    print("\n── Phase 2: yfinance (Verlaufs-Check) ───────────────")
    for ticker in tickers:
        print(f"  [{ticker}]")
        msg = check_yfinance(con, ticker)
        if msg and ticker not in edgar_tickers:
            yf_alerts.append(msg)

    # ── Ergebnis ──────────────────────────────────────────────────────────────
    print(f"\n{'='*52}")
    print(f"  EDGAR-Ankuendigungen : {len(edgar_alerts)}")
    print(f"  yfinance-Aenderungen : {len(yf_alerts)}")
    print(f"{'='*52}\n")

    if edgar_alerts or yf_alerts:
        message = build_message(edgar_alerts, yf_alerts)
        print("Telegram:\n" + message + "\n")
        send_telegram(message)
    else:
        print("Keine Aenderungen heute — kein Alert.")

    con.close()
    print("\n=== Fertig ===")

if __name__ == "__main__":
    main()
