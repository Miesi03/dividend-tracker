"""
Dividend Tracker — kombiniert EDGAR (Ankündigungen) + yfinance (Verlauf)
------------------------------------------------------------------------
Taeglich zwei Pruefungen:

1. EDGAR 8-K Scan  (nur US-Aktien)
   Sucht nach Dividendenankuendigungen die gestern oder heute eingereicht
   wurden. Alert sofort bei Ankuendigung — bis zu 6 Wochen vor Ex-Date.

2. yfinance Fallback  (alle Ticker inkl. internationale)
   Vergleicht neueste Dividende in der Historie mit dem gespeicherten Wert.
   Alert am oder kurz nach dem Ex-Date.
"""

import os, re, json, sqlite3, time, urllib.request, yaml
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

# ── Konfiguration ─────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DB_PATH   = Path("dividend_history.db")
WATCHLIST = Path("watchlist.yaml")

# Prozentuale Mindest-Aenderung fuer Alert (0.5 = 0,5 %)
MIN_CHANGE_PCT = 0.5

# SEC EDGAR User-Agent (Pflicht laut SEC-Regeln)
SEC_UA = "DividendTracker private-use contact@example.com"

# ── Datenbank ─────────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS dividends (
            ticker    TEXT NOT NULL,
            ex_date   TEXT NOT NULL,
            amount    REAL NOT NULL,
            source    TEXT DEFAULT 'yfinance',
            saved_at  TEXT NOT NULL,
            PRIMARY KEY (ticker, ex_date)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS edgar_seen (
            adsh      TEXT PRIMARY KEY,
            ticker    TEXT NOT NULL,
            filed_at  TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT NOT NULL,
            alert_type  TEXT NOT NULL,
            ex_date     TEXT,
            old_amount  REAL,
            new_amount  REAL NOT NULL,
            change_pct  REAL,
            alerted_at  TEXT NOT NULL
        )
    """)
    con.commit()
    return con

def get_stored(con, ticker, limit=2):
    return con.execute(
        "SELECT ex_date, amount FROM dividends WHERE ticker=? ORDER BY ex_date DESC LIMIT ?",
        (ticker, limit)
    ).fetchall()

def upsert(con, ticker, ex_date, amount, source='yfinance'):
    con.execute(
        "INSERT OR REPLACE INTO dividends (ticker, ex_date, amount, source, saved_at) VALUES (?,?,?,?,?)",
        (ticker, ex_date, amount, source, datetime.now(timezone.utc).isoformat())
    )
    con.commit()

def edgar_already_seen(con, adsh):
    return con.execute("SELECT 1 FROM edgar_seen WHERE adsh=?", (adsh,)).fetchone() is not None

def mark_edgar_seen(con, adsh, ticker):
    con.execute(
        "INSERT OR IGNORE INTO edgar_seen (adsh, ticker, filed_at) VALUES (?,?,?)",
        (adsh, ticker, datetime.now(timezone.utc).isoformat())
    )
    con.commit()

def save_alert(con, ticker, alert_type, new_amount, ex_date=None, old_amount=None, change_pct=None):
    con.execute(
        "INSERT INTO alerts (ticker, alert_type, ex_date, old_amount, new_amount, change_pct, alerted_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (ticker, alert_type, ex_date, old_amount, new_amount, change_pct,
         datetime.now(timezone.utc).isoformat())
    )
    con.commit()

# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": SEC_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def strip_html(html):
    text = re.sub(r'<[^>]+>', ' ', html if isinstance(html, str) else html.decode('utf-8', errors='ignore'))
    return re.sub(r'\s+', ' ', text).strip()

# ── EDGAR CIK-Lookup ──────────────────────────────────────────────────────────

_cik_map = {}

def load_cik_map():
    global _cik_map
    if _cik_map:
        return _cik_map
    try:
        print("  [EDGAR] Lade CIK-Verzeichnis von SEC...")
        data = json.loads(http_get("https://www.sec.gov/files/company_tickers.json"))
        _cik_map = {e['ticker'].upper(): str(e['cik_str']).zfill(10) for e in data.values()}
        print(f"  [EDGAR] {len(_cik_map)} Ticker geladen")
    except Exception as e:
        print(f"  [EDGAR] CIK-Laden fehlgeschlagen: {e}")
    return _cik_map

# ── EDGAR 8-K Scanner ─────────────────────────────────────────────────────────

DIV_AMOUNT_PATTERNS = [
    r'declared\s+a\s+(?:regular\s+)?(?:quarterly|monthly|annual|semi-annual)?\s*(?:cash\s+)?dividend\s+of\s+\$?([\d.]+)',
    r'(?:quarterly|monthly|annual)\s+(?:cash\s+)?dividend\s+of\s+\$?([\d.]+)\s+per\s+(?:common\s+)?share',
    r'\$?([\d.]+)\s+per\s+(?:common\s+)?share[^.]*dividend',
    r'dividend\s+(?:of\s+|increased\s+to\s+)\$?([\d.]+)\s+per\s+share',
]

DIV_DATE_PATTERNS = [
    r'ex[-\s]?dividend\s+date[^\d]*(\w+ \d+,?\s*\d{4})',
    r'ex[-\s]?dividend[^\d]*(\w+ \d+,?\s*\d{4})',
    r'payable\s+(\w+ \d+,?\s*\d{4})',
    r'payment\s+date[^\d]*(\w+ \d+,?\s*\d{4})',
]

CHANGE_PATTERNS = [
    r'increase(?:d)?\s+(?:of\s+)?(\d+(?:\.\d+)?)\s*(?:percent|%)',
    r'(\d+(?:\.\d+)?)\s*(?:percent|%)\s+increase',
    r'decrease(?:d)?\s+(?:of\s+)?(\d+(?:\.\d+)?)\s*(?:percent|%)',
    r'(\d+(?:\.\d+)?)\s*(?:percent|%)\s+decrease',
    r'raise(?:d)?\s+(?:the\s+)?dividend\s+by\s+(\d+(?:\.\d+)?)\s*(?:percent|%)',
]

def extract_dividend_info(text):
    """Extrahiert Dividendenbetrag und weitere Infos aus dem Filing-Text."""
    amount = None
    for pat in DIV_AMOUNT_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            try:
                val = float(m.group(1))
                if 0.001 < val < 1000:  # Plausibilitaetscheck
                    amount = val
                    break
            except ValueError:
                continue

    change_pct_text = None
    for pat in CHANGE_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            direction = 'increase' if 'increase' in pat or 'raise' in pat else 'decrease'
            change_pct_text = (float(m.group(1)), direction)
            break

    return amount, change_pct_text


def fetch_8k_text(cik_int, adsh, docname):
    """Laedt den Text eines 8-K Dokuments."""
    acc_path = adsh.replace('-', '')
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_path}/{docname}"
    try:
        raw = http_get(url, timeout=12)
        return strip_html(raw)
    except Exception:
        return ""


def scan_edgar_for_ticker(con, ticker, cik_padded, company_name, lookback_days=2):
    """
    Prueft ob ein Ticker in den letzten `lookback_days` Tagen ein
    dividenden-relevantes 8-K eingereicht hat.
    Gibt Liste von Alert-Dicts zurueck.
    """
    alerts = []
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()

    try:
        data = json.loads(http_get(
            f"https://data.sec.gov/submissions/CIK{cik_padded}.json", timeout=15
        ))
    except Exception as e:
        print(f"  [{ticker}] EDGAR-Fehler: {e}")
        return alerts

    recent   = data['filings']['recent']
    forms    = recent.get('form', [])
    dates    = recent.get('filingDate', [])
    accnums  = recent.get('accessionNumber', [])
    items    = recent.get('items', [''] * len(forms))
    prim_doc = recent.get('primaryDocument', [''] * len(forms))

    for i in range(len(forms)):
        if forms[i] != '8-K':
            continue
        if dates[i] < cutoff:
            break  # Neueste zuerst, frueh abbrechen
        adsh = accnums[i]
        if edgar_already_seen(con, adsh):
            continue

        # items 8.01 = Sonstige Bekanntmachungen (oft Dividenden)
        # Wir pruefen alle 8-K der letzten Tage auf Dividend-Keywords
        doc = prim_doc[i] if i < len(prim_doc) else ''
        if not doc:
            continue

        cik_int = int(cik_padded)
        text = fetch_8k_text(cik_int, adsh, doc)
        time.sleep(0.1)  # Hoeflich gegenueber SEC-Server

        if 'dividend' not in text.lower():
            mark_edgar_seen(con, adsh, ticker)
            continue

        amount, change_info = extract_dividend_info(text)
        mark_edgar_seen(con, adsh, ticker)

        if amount is None:
            continue

        # Vergleich mit letztem gespeicherten Wert
        stored = get_stored(con, ticker, limit=1)
        old_amount = stored[0][1] if stored else None

        # Berechne prozentuale Aenderung
        if old_amount and old_amount > 0:
            calc_pct = (amount - old_amount) / old_amount * 100
        else:
            calc_pct = None

        # Nutze SEC-Text-Prozent wenn vorhanden, sonst berechnet
        if change_info:
            pct_val, direction = change_info
            reported_pct = pct_val if direction == 'increase' else -pct_val
        else:
            reported_pct = calc_pct

        # Ist es eine Aenderung?
        is_change = (reported_pct is not None and abs(reported_pct) >= MIN_CHANGE_PCT)
        is_new    = (old_amount is None)

        if not is_change and not is_new:
            print(f"  [{ticker}] EDGAR: Dividende {amount:.4f} — unveraendert")
            continue

        print(f"  [{ticker}] *** EDGAR-Ankuendigung: {amount:.4f}"
              + (f" ({reported_pct:+.1f}%)" if reported_pct else "") + " ***")

        save_alert(con, ticker, 'edgar_announcement', amount,
                   old_amount=old_amount, change_pct=reported_pct)

        alerts.append({
            "ticker":       ticker,
            "company":      company_name,
            "amount":       amount,
            "old_amount":   old_amount,
            "change_pct":   reported_pct,
            "filing_date":  dates[i],
            "source":       "edgar",
        })

    return alerts


# ── yfinance Verlaufs-Check ───────────────────────────────────────────────────

def check_yfinance(con, ticker):
    """Vergleicht neueste yfinance-Dividende mit gespeichertem Wert."""
    try:
        import yfinance as yf
        divs = yf.Ticker(ticker).dividends
        if divs is None or divs.empty:
            return None
        history = sorted(
            [(ts.date().isoformat(), float(v)) for ts, v in divs.items()],
            reverse=True
        )
    except Exception as e:
        print(f"  [{ticker}] yfinance-Fehler: {e}")
        return None

    if not history:
        print(f"  [{ticker}] keine Dividendendaten")
        return None

    latest_date, latest_amount = history[0]
    upsert(con, ticker, latest_date, latest_amount, source='yfinance')

    rows = get_stored(con, ticker, limit=2)
    if len(rows) < 2:
        print(f"  [{ticker}] yfinance Baseline: {latest_amount:.4f} ({latest_date})")
        return None

    curr_date, curr_amount = rows[0]
    prev_date, prev_amount = rows[1]

    if prev_amount == 0:
        return None

    change_pct = (curr_amount - prev_amount) / prev_amount * 100

    if abs(change_pct) < MIN_CHANGE_PCT:
        print(f"  [{ticker}] yfinance: {curr_amount:.4f} — unveraendert ({change_pct:+.2f}%)")
        return None

    direction = "Erhoehung" if change_pct > 0 else "Senkung"
    print(f"  [{ticker}] *** yfinance {direction}: {change_pct:+.2f}% ***")

    save_alert(con, ticker, 'yfinance_change', curr_amount,
               ex_date=curr_date, old_amount=prev_amount, change_pct=change_pct)

    return {
        "ticker":     ticker,
        "company":    ticker,
        "amount":     curr_amount,
        "old_amount": prev_amount,
        "change_pct": change_pct,
        "ex_date":    curr_date,
        "prev_date":  prev_date,
        "source":     "yfinance",
    }

# ── Telegram ──────────────────────────────────────────────────────────────────

def fmt_amount(amount):
    """Formatiert einen Dividendenbetrag."""
    return f"{amount:.4f}".rstrip('0').rstrip('.')


def format_edgar_alert(c):
    arrow  = "↑" if (c.get('change_pct') or 0) >= 0 else "↓"
    pct    = c.get('change_pct')
    amount = c['amount']
    old    = c.get('old_amount')
    company = c.get('company', c['ticker'])

    lines = [f"📢 <b>{company} ({c['ticker']}) — Dividendenankuendigung</b>"]
    lines.append(f"{arrow} Neue Dividende: <code>${fmt_amount(amount)}</code> pro Aktie")

    if old and pct is not None:
        lines.append(f"   Vorherige Dividende: <code>${fmt_amount(old)}</code>")
        change_word = "Erhoehung" if pct > 0 else "Senkung"
        lines.append(f"   Das ist eine <b>{change_word} von {abs(pct):.1f}%</b> zur letzten Ausschuettung")
    elif old:
        lines.append(f"   Vorherige Dividende: <code>${fmt_amount(old)}</code>")
    else:
        lines.append(f"   (Erstmalige Erfassung — kein Vergleichswert)")

    lines.append(f"   Eingereicht: {c.get('filing_date','?')} | Quelle: SEC EDGAR 8-K")
    return "\n".join(lines)


def format_yfinance_alert(c):
    pct    = c['change_pct']
    arrow  = "↑" if pct > 0 else "↓"
    word   = "Erhoehung" if pct > 0 else "Senkung"

    lines = [f"{arrow} <b>{c['ticker']} — Dividenden{word} bestaetigt</b>"]
    lines.append(f"   Neue Dividende: <code>${fmt_amount(c['amount'])}</code>  (Ex-Date: {c['ex_date']})")
    lines.append(f"   Vorher:         <code>${fmt_amount(c['old_amount'])}</code>  (Ex-Date: {c['prev_date']})")
    lines.append(f"   Aenderung: <b>{pct:+.1f}%</b> | Quelle: yfinance")
    return "\n".join(lines)


def format_message(edgar_alerts, yf_alerts):
    today = date.today().strftime("%d.%m.%Y")
    parts = [f"<b>Dividend Tracker — {today}</b>\n"]

    if edgar_alerts:
        parts.append("─── EDGAR-Ankuendigungen ───")
        for c in edgar_alerts:
            parts.append(format_edgar_alert(c))

    if yf_alerts:
        parts.append("\n─── Bestaetigt via yfinance ───")
        for c in yf_alerts:
            parts.append(format_yfinance_alert(c))

    total = len(edgar_alerts) + len(yf_alerts)
    parts.append(f"\n{total} Meldung(en) heute.")
    return "\n\n".join(parts)


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [Telegram] Token/Chat-ID fehlt")
        return
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"  [Telegram] {'OK' if r.status == 200 else r.status}")
    except Exception as e:
        print(f"  [Telegram] Fehler: {e}")

# ── Watchlist ─────────────────────────────────────────────────────────────────

def load_watchlist():
    if not WATCHLIST.exists():
        raise FileNotFoundError(f"{WATCHLIST} nicht gefunden")
    with open(WATCHLIST) as f:
        data = yaml.safe_load(f)
    tickers = data.get("tickers", [])
    if not tickers:
        raise ValueError("Watchlist ist leer")
    return [str(t).strip().upper() for t in tickers]

# ── Hauptprogramm ─────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*50}")
    print(f"Dividend Tracker — {now}")
    print(f"{'='*50}\n")

    tickers = load_watchlist()
    print(f"Watchlist: {len(tickers)} Ticker\n")

    con     = init_db()
    cik_map = load_cik_map()

    edgar_alerts = []
    yf_alerts    = []

    # ── Phase 1: EDGAR 8-K Scan (US-Ticker) ──────────────────────────────────
    print("\n── Phase 1: EDGAR 8-K Scan ──────────────────────")
    for ticker in tickers:
        cik = cik_map.get(ticker)
        if not cik:
            print(f"  [{ticker}] kein CIK — uebersprungen (nicht-US?)")
            continue

        # Firmenname aus submissions holen (gecacht nach erstem Aufruf)
        company_name = ticker  # Fallback
        try:
            sub = json.loads(http_get(
                f"https://data.sec.gov/submissions/CIK{cik}.json", timeout=15
            ))
            company_name = sub.get('name', ticker)
        except Exception:
            pass

        print(f"  [{ticker}] {company_name}")
        alerts = scan_edgar_for_ticker(con, ticker, cik, company_name, lookback_days=2)
        edgar_alerts.extend(alerts)
        time.sleep(0.15)  # Rate-Limiting SEC

    # ── Phase 2: yfinance Verlaufs-Check (alle Ticker) ───────────────────────
    print("\n── Phase 2: yfinance Verlaufs-Check ─────────────")
    # Ticker die bereits per EDGAR gefunden wurden ueberspringen
    edgar_tickers = {a['ticker'] for a in edgar_alerts}

    for ticker in tickers:
        print(f"  [{ticker}]")
        change = check_yfinance(con, ticker)
        if change and ticker not in edgar_tickers:
            yf_alerts.append(change)

    # ── Ergebnis ──────────────────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"EDGAR-Ankuendigungen: {len(edgar_alerts)}")
    print(f"yfinance-Aenderungen: {len(yf_alerts)}")
    print(f"{'='*50}\n")

    if edgar_alerts or yf_alerts:
        msg = format_message(edgar_alerts, yf_alerts)
        print("Telegram-Nachricht:\n")
        print(msg)
        print()
        send_telegram(msg)
    else:
        print("Keine Dividendenaenderungen heute — kein Alert.")

    con.close()
    print("\n=== Fertig ===\n")


if __name__ == "__main__":
    main()
