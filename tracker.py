"""
Dividend Tracker
Kombiniert SEC EDGAR (fruehzeitige Ankuendigungen) + yfinance (Verlauf)

Fixes vs. urspruengliche Version:
  - curl_cffi-Session (Browser-Imitation) gegen Yahoo Rate-Limiting
  - tk.info entfernt → Firmennamen kommen aus EDGAR-Cache (spart 52 HTTP-Calls)
  - time.sleep(2) zwischen yfinance-Abfragen
  - Explizites Error-Logging statt stilles except-Schlucken
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
ROUNDING_TOL     = 0.0005  # yfinance rundet Dividenden auf 3 Nachkommastellen (z. B. 0.0625 → 0.063)
FX_MIN_CHANGE_PCT = 2.0    # Schwelle für zurückgerechnete Beträge (Rückrechnung ist auf ca. ±1 % genau)
SEC_UA           = "DividendTracker private-use admin@example.com"
YF_SLEEP_SEC     = 2.0   # Pause zwischen yfinance-Abfragen → verhindert Yahoo 429

# ── Währung ───────────────────────────────────────────────────────────────────

def get_currency_symbol(ticker: str) -> str:
    """Gibt das Währungssymbol basierend auf dem Ticker-Suffix zurück.
    yfinance liefert Dividenden bereits in der Heimatwährung der Aktie —
    wir müssen nur das korrekte Symbol anzeigen.
    """
    t = ticker.upper()
    # Euro-Börsen
    if any(t.endswith(s) for s in [
        '.DE',   # Xetra (Deutschland)
        '.PA',   # Euronext Paris
        '.AS',   # Euronext Amsterdam
        '.MI',   # Borsa Italiana
        '.BR',   # Euronext Brüssel
        '.LS',   # Euronext Lissabon
        '.MC',   # Bolsa Madrid
        '.HE',   # Nasdaq Helsinki
        '.AT',   # Athex (Griechenland)
        '.VI',   # Wiener Börse
        '.AM',   # Euronext Amsterdam (alt)
        '.IR',   # Euronext Dublin
    ]):
        return '€'
    elif t.endswith('.SW'):
        return 'CHF '   # SIX Swiss Exchange
    elif t.endswith('.OL'):
        return 'NOK '   # Oslo Børs
    elif t.endswith(('.CO', '.CPH')):
        return 'DKK '   # Nasdaq Kopenhagen
    elif t.endswith('.ST'):
        return 'SEK '   # Nasdaq Stockholm
    elif t.endswith(('.TW', '.TWO')):
        return 'NT$'    # Taiwan Stock Exchange / Taipei Exchange
    elif t.endswith(('.KS', '.KQ')):
        return '₩'      # Korea Exchange (KOSPI / KOSDAQ)
    elif t.endswith('.HK'):
        return 'HK$'    # Hongkong Stock Exchange
    elif t.endswith('.T'):
        return '¥'      # Tokyo Stock Exchange
    elif t.endswith('.L'):
        return 'p '     # London Stock Exchange (yfinance liefert Pence, nicht Pfund)
    elif t.endswith('.AX'):
        return 'A$'     # Australian Securities Exchange
    elif t.endswith('.TO') or t.endswith('.V'):
        return 'CA$'    # Toronto Stock Exchange
    else:
        return '$'      # Default: USD (US-Börsen ohne Suffix)

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
    s = f"{amount:.4f}".rstrip("0")
    return s + "0" * max(0, 2 - len(s.split(".")[1]))   # mind. 2 Nachkommastellen

def pct_change(new, old):
    return (new - old) / old * 100 if old else None

def amounts_differ(new, old, min_pct=0.0):
    """True, wenn sich zwei Beträge um mehr als Rundungsungenauigkeit unterscheiden.
    min_pct: zusätzliche relative Mindestabweichung (für zurückgerechnete Beträge).
    """
    if abs(new - old) <= ROUNDING_TOL + 1e-9:
        return False
    pct = pct_change(new, old)
    return pct is None or abs(pct) >= min_pct

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [Telegram] Keine Zugangsdaten gesetzt")
        return
    payload = json.dumps({
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
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

# ── Datumsformat ─────────────────────────────────────────────────────────────

def fmt_date_dmy(iso_date: str) -> str:
    """Konvertiert ISO-Datum (YYYY-MM-DD) zu TT-MM-JJJJ."""
    try:
        dt = datetime.strptime(iso_date[:10], "%Y-%m-%d")
        return dt.strftime("%d-%m-%Y")
    except ValueError:
        return iso_date

# ── Dividendenfrequenz ────────────────────────────────────────────────────────

def detect_frequency(history: list) -> str | None:
    """Leitet die Zahlungsfrequenz aus den letzten Ex-Date-Abständen ab.
    history: [(ex_date_iso, amount), ...] absteigend sortiert.
    Nutzt bis zu 5 aufeinanderfolgende Abstände für Stabilität.
    """
    if len(history) < 2:
        return None
    gaps = []
    for i in range(min(5, len(history) - 1)):
        try:
            d1 = date.fromisoformat(history[i][0])
            d2 = date.fromisoformat(history[i + 1][0])
            gaps.append((d1 - d2).days)
        except (ValueError, TypeError):
            continue
    if not gaps:
        return None
    avg = sum(gaps) / len(gaps)
    if avg < 45:
        return "Monatlich"
    elif avg < 135:
        return "Quartalsweise"
    elif avg < 270:
        return "Halbjährlich"
    else:
        return "Jährlich"

def extract_frequency_edgar(text: str) -> str | None:
    """Extrahiert die Dividendenfrequenz aus einem EDGAR-Meldetext."""
    t = text.lower()
    if "quarterly" in t:
        return "Quartalsweise"
    if "monthly" in t:
        return "Monatlich"
    if "semi-annual" in t or "semiannual" in t or "semi annual" in t:
        return "Halbjährlich"
    if "annual" in t:
        return "Jährlich"
    return None

# ── Ex-Date aus EDGAR-Text ────────────────────────────────────────────────────

_EX_DATE_TEXT_RE = [
    r"ex.dividend\s+date\s+(?:will\s+be\s+|is\s+|of\s+)?([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
    r"ex.date\s+(?:will\s+be\s+|is\s+|of\s+)?([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
    r"ex.dividend\s+date\s+of\s+(\d{4}-\d{2}-\d{2})",
    r"ex-dividend\s+date\s+(?:will\s+be\s+)?(\d{1,2}/\d{1,2}/\d{2,4})",
]

def extract_ex_date_edgar(text: str) -> str | None:
    """Extrahiert das Ex-Dividende-Datum aus einem EDGAR-Meldetext.
    Gibt das Datum als TT-MM-JJJJ zurück oder None.
    """
    for pat in _EX_DATE_TEXT_RE:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        raw = m.group(1).strip()
        for fmt_str in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y",
                        "%b %d %Y", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
            try:
                dt = datetime.strptime(raw, fmt_str)
                return dt.strftime("%d-%m-%Y")
            except ValueError:
                pass
    return None

# ── Nachrichtenformat ─────────────────────────────────────────────────────────

def msg_edgar(ticker, company, amount, old_amount, change_pct, filing_date,
              ex_date=None, frequency=None):
    """EDGAR deckt nur US-Unternehmen ab → Währung ist immer USD ($)."""
    arrow = "↑" if (change_pct or 0) >= 0 else "↓"
    lines = [f"📢 <b>{company} ({ticker})</b>"]
    lines.append(f"{arrow} Dividendenankündigung via SEC EDGAR")
    lines.append(f"   Neue Dividende: <b>${fmt(amount)}</b> pro Aktie")
    if old_amount:
        lines.append(f"   Vorherige Dividende: ${fmt(old_amount)}")
    if change_pct is not None:
        word = "Erhöhung" if change_pct > 0 else "Senkung"
        lines.append(f"   ➜ Das ist eine <b>{word} von {abs(change_pct):.1f}%</b>")
    lines.append(f"   Eingereicht am:  {filing_date}")
    if ex_date:
        lines.append(f"   Ex-Dividende:    {ex_date}")
    if frequency:
        lines.append(f"   Zahlungsweise:   {frequency}")
    return "\n".join(lines)

def msg_yfinance(ticker, company, curr, prev, yoy, change_pct, currency='$',
                 frequency=None, ttm=None, split_note=None, approx=False):
    """Zeigt Dividenden in der Heimatwährung der Aktie an – analog zum EDGAR-Format.
    curr/prev/yoy: (ex_date, amount) der neuen, der vorherigen und der Vorjahres-Zahlung
    (yoy kann None sein). change_pct bezieht sich auf yoy, falls vorhanden, sonst auf prev.
    ttm: (summe_12m, summe_vorjahr_12m) oder None.
    """
    arrow  = "↑" if change_pct > 0 else "↓"
    word   = "Erhöhung" if change_pct > 0 else "Senkung"
    ca     = "ca. " if approx else ""
    cur    = lambda v: f"{ca}{currency}{fmt(v)}"
    lines  = [f"📢 <b>{company} ({ticker})</b>"]
    lines.append(f"{arrow} Dividendenankündigung via yfinance")
    lines.append(f"   Neue Dividende: <b>{cur(curr[1])}</b> pro Aktie  (Ex-Date: {fmt_date_dmy(curr[0])})")
    if yoy:
        lines.append(f"   Vorjahreszeitraum: {cur(yoy[1])}  (Ex-Date: {fmt_date_dmy(yoy[0])})")
        lines.append(f"   ➜ Das ist eine <b>{word} von {abs(change_pct):.1f}%</b> ggü. Vorjahr")
        prev_pct = pct_change(curr[1], prev[1])
        lines.append(f"   Vorherige Zahlung: {cur(prev[1])}  (Ex-Date: {fmt_date_dmy(prev[0])}, {prev_pct:+.1f}%)")
    else:
        lines.append(f"   Vorherige Dividende: {cur(prev[1])}  (Ex-Date: {fmt_date_dmy(prev[0])})")
        lines.append(f"   ➜ Das ist eine <b>{word} von {abs(change_pct):.1f}%</b>")
    if ttm:
        ttm_pct = pct_change(ttm[0], ttm[1])
        lines.append(f"   Summe 12 Monate: {cur(ttm[0])}  (Vorjahr: {cur(ttm[1])}, {ttm_pct:+.1f}%)")
    if frequency:
        lines.append(f"   Zahlungsweise:   {frequency}")
    if split_note:
        lines.append(f"   Hinweis: {split_note}")
    if approx:
        lines.append(f"   Hinweis: in Ankündigungswährung zurückgerechnet, Abweichung ca. ±1 %")
    return "\n".join(lines)

def build_message(edgar_alerts, yf_alerts):
    today = date.today().strftime("%d.%m.%Y")
    parts = [f"<b>Dividend Tracker — {today}</b>"]
    if edgar_alerts:
        parts.append("\n<b>── Neue Ankündigungen (EDGAR) ──</b>")
        parts.extend(edgar_alerts)
    if yf_alerts:
        parts.append("\n<b>── Bestätigt via Kursdaten ──</b>")
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
    (r"(\d+(?:\.\d+)?)\s*(?:percent|%)\s+increase",            "increase"),
    (r"increase[^.]{0,30}(\d+(?:\.\d+)?)\s*(?:percent|%)",    "increase"),
    (r"(\d+(?:\.\d+)?)\s*(?:percent|%)\s+decrease",            "decrease"),
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
        print(f"  EDGAR-Fehler: {e}")
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

        cik_int  = int(cik)
        acc_path = adsh.replace("-", "")
        doc_url  = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_path}/{doc}"
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

        stored     = get_stored(con, ticker, limit=1)
        old_amount = stored[0][1] if stored else None

        # yfinance-Werte sind auf 3 Nachkommastellen gerundet (VRT: 0.0625 vs. 0.063)
        # → Abweichungen im Rundungsbereich gelten nicht als Änderung
        if old_amount and old_amount > 0 and amounts_differ(amount, old_amount):
            calc_pct = pct_change(amount, old_amount)
        else:
            calc_pct = None

        reported_pct = extract_change(text) or calc_pct
        is_new    = old_amount is None
        is_change = reported_pct is not None and abs(reported_pct) >= MIN_CHANGE_PCT

        if not is_new and not is_change:
            print(f"  EDGAR: ${amount:.4f} — keine Aenderung")
            continue

        # Ex-Dividende-Datum und Zahlungsfrequenz aus Meldetext extrahieren
        ex_date   = extract_ex_date_edgar(text)
        frequency = extract_frequency_edgar(text)

        pct_str = f"{reported_pct:+.1f}%" if reported_pct else "neu"
        print(f"  *** EDGAR-Ankuendigung: ${amount:.4f} ({pct_str}) | Ex-Date: {ex_date or '–'} | {frequency or '–'} ***")
        alerts.append(msg_edgar(ticker, company_name, amount, old_amount, reported_pct,
                                dates[i], ex_date, frequency))
    return alerts

# ── yfinance ──────────────────────────────────────────────────────────────────

def check_yfinance(con, ticker, session, company_name: str):
    """
    Prüft einen Ticker auf Dividendenänderungen via yfinance.

    Parameters
    ----------
    session     : curl_cffi.requests.Session — Browser-imitierende Session gegen
                  Yahoo Rate-Limiting. Wird einmalig in main() erstellt und für
                  alle Ticker wiederverwendet.
    company_name: Firmenname aus EDGAR-Cache oder Ticker-Symbol. Wir rufen
                  tk.info bewusst NICHT auf – spart ~52 HTTP-Requests und
                  reduziert Rate-Limit-Risiko erheblich.
    """
    try:
        import yfinance as yf
        import pandas as pd

        tk  = yf.Ticker(ticker, session=session)
        raw = tk.dividends
        if raw is None or (hasattr(raw, 'empty') and raw.empty):
            return None

        # yfinance >= 0.2.x gibt DataFrame zurück, ältere Versionen Series
        if isinstance(raw, pd.DataFrame):
            col   = raw.columns[0]
            items = [(idx, float(row[col])) for idx, row in raw.iterrows()]
        else:
            items = [(ts, float(v)) for ts, v in raw.items()]

        history = []
        for ts, amount in items:
            if hasattr(ts, 'date'):
                ex_date = ts.date().isoformat()
            else:
                ex_date = str(ts)[:10]
            history.append((ex_date, amount))
        history.sort(reverse=True)

    except Exception as e:
        # Explizites Logging statt stilles Schlucken – sichtbar im Actions-Log
        print(f"  [{ticker}] yfinance-Fehler: {type(e).__name__}: {e}")
        return None

    if not history:
        return None

    declared = DECLARED_CURRENCY.get(ticker)
    if declared:
        history = convert_history(tk, history, declared, session)
        if not history:
            return None
        currency = "$" if declared == "USD" else f"{declared} "
        differ_pct = FX_MIN_CHANGE_PCT
    else:
        currency = get_currency_symbol(ticker)   # Heimatwährung des Tickers
        differ_pct = 0.0

    latest_date, latest_amount = history[0]

    # Zahlungsfrequenz aus historischen Ex-Dates ableiten
    frequency = detect_frequency(history)

    # Die DB merkt sich nur, welches Ex-Date bereits bekannt ist. Verglichen wird
    # ausschließlich innerhalb der yfinance-Historie, weil yfinance nach einem
    # Aktiensplit alle alten Beträge rückwirkend bereinigt (APH 2026: 0.25 → 0.125),
    # die in der DB gespeicherten Werte aber nicht.
    existing = get_stored(con, ticker, limit=1)
    upsert(con, ticker, latest_date, latest_amount)

    if not existing:
        print(f"  Baseline gesetzt: {currency}{latest_amount:.4f} ({latest_date})")
        return None
    if latest_date <= existing[0][0] or len(history) < 2:
        print(f"  Unveraendert: {currency}{latest_amount:.4f} ({latest_date})")
        return None

    result = evaluate_change(history, frequency, differ_pct)
    if result is None:
        print(f"  Unveraendert: {currency}{latest_amount:.4f} ({latest_date})")
        return None

    yoy, pct, ttm = result
    direction = "Erhoehung" if pct > 0 else "Senkung"
    basis     = "ggü. Vorjahr" if yoy else "ggü. Vorzahlung"
    print(f"  *** {direction}: {pct:+.2f}% {basis} | {company_name} | {frequency or '–'} ***")
    return msg_yfinance(ticker, company_name, history[0], history[1], yoy, pct,
                        currency, frequency, ttm, recent_split_note(tk),
                        approx=bool(declared))

def find_yoy(history, frequency):
    """Sucht die Zahlung, die etwa ein Jahr vor der neuesten liegt (Vorjahreszeitraum).
    Bei jährlicher Zahlung ist das die vorherige Zahlung → None (kein Mehrwert).
    """
    latest = date.fromisoformat(history[0][0])
    target = latest - timedelta(days=365)
    window = {"Monatlich": 12, "Quartalsweise": 40}.get(frequency, 60)
    best = None
    for ex_date, amount in history[2:]:
        dist = abs((date.fromisoformat(ex_date) - target).days)
        if dist <= window and (best is None or dist < best[0]):
            best = (dist, (ex_date, amount))
    return best[1] if best else None

def ttm_sums(history, frequency):
    """Summe der letzten n Zahlungen (n = Zahlungen pro Jahr) und der n Zahlungen davor.
    Nur wenn beide Blöcke jeweils etwa ein Jahr abdecken, sonst None
    (z. B. bei unregelmäßigen Zahlern oder zu kurzer Historie).
    """
    n = {"Monatlich": 12, "Quartalsweise": 4, "Halbjährlich": 2}.get(frequency)
    if not n or len(history) <= 2 * n:
        return None
    days = lambda a, b: (date.fromisoformat(history[a][0]) - date.fromisoformat(history[b][0])).days
    if not (330 <= days(0, n) <= 400 and 330 <= days(n, 2 * n) <= 400):
        return None
    cur  = sum(a for _, a in history[:n])
    prev = sum(a for _, a in history[n:2 * n])
    return (cur, prev) if prev > 0 else None

def evaluate_change(history, frequency, differ_pct=0.0):
    """Bewertet die neueste Zahlung gegenüber der Historie.

    1. Gleicht sie der vorherigen Zahlung (bis auf Rundung) → keine Änderung.
    2. Gibt es eine Zahlung im Vorjahreszeitraum, wird mit dieser verglichen.
       So werden schwankende Interims-/Schlussdividenden (ASML, LVMH, Novo)
       nicht als Kürzung oder Erhöhung gemeldet.
    3. Sonst Vergleich mit der vorherigen Zahlung.
    Rückgabe: (yoy, pct, ttm) bei einer Änderung, sonst None.
    """
    curr, prev = history[0], history[1]
    if prev[1] == 0 or not amounts_differ(curr[1], prev[1], differ_pct):
        return None
    yoy = find_yoy(history, frequency)
    ref = yoy or prev
    if ref[1] == 0 or not amounts_differ(curr[1], ref[1], differ_pct):
        return None
    pct = pct_change(curr[1], ref[1])
    if abs(pct) < max(MIN_CHANGE_PCT, differ_pct):
        return None
    return yoy, pct, ttm_sums(history, frequency)

def recent_split_note(tk, days=400):
    """Hinweistext, falls in den letzten `days` Tagen ein Aktiensplit stattfand."""
    try:
        splits = tk.splits
        cutoff = date.today() - timedelta(days=days)
        for ts, ratio in sorted(splits.items(), reverse=True):
            if ts.date() < cutoff:
                break
            ratio = float(ratio)
            day   = ts.date().strftime('%d-%m-%Y')
            if ratio >= 1 and ratio.is_integer():
                return f"Aktiensplit {ratio:g}:1 am {day}, Werte splitbereinigt"
            if ratio < 1 and (1 / ratio).is_integer():
                return f"Reverse-Split 1:{1 / ratio:g} am {day}, Werte splitbereinigt"
            # Krumme Faktoren stammen meist von Abspaltungen (Spin-offs)
            return f"Kursanpassung (Faktor {ratio:.3f}, z. B. Abspaltung) am {day}, Werte bereinigt"
    except Exception as e:
        print(f"  Split-Abfrage fehlgeschlagen: {type(e).__name__}: {e}")
    return None

def convert_history(tk, history, declared, session):
    """Rechnet die von yfinance in Börsenwährung gelieferten Dividenden in die
    Ankündigungswährung zurück (z. B. Höegh Autoliners: USD festgelegt, Oslo in NOK).
    Kurs: Mittel der Schlusskurse der 7 Tage vor dem Ex-Date (Yahoo rechnet etwa
    zum Ankündigungszeitpunkt um). Genauigkeit ca. ±1 %.
    """
    try:
        import yfinance as yf
        listing = tk.fast_info["currency"]
        if not listing or listing.upper() == declared:
            return history
        recent = history[:16]   # reicht für Vorjahres- und 12-Monats-Vergleich
        start  = date.fromisoformat(recent[-1][0]) - timedelta(days=10)
        fx     = yf.Ticker(f"{declared}{listing.upper()}=X", session=session) \
                   .history(start=start.isoformat())["Close"]
        fx_by_day = {ts.date(): float(v) for ts, v in fx.items()}
        converted = []
        for ex_date, amount in recent:
            d     = date.fromisoformat(ex_date)
            rates = [r for day, r in fx_by_day.items() if d - timedelta(days=7) <= day <= d]
            if rates:
                converted.append((ex_date, round(amount / (sum(rates) / len(rates)), 4)))
        return converted
    except Exception as e:
        print(f"  Waehrungsumrechnung fehlgeschlagen: {type(e).__name__}: {e}")
        return []

# ── Watchlist ─────────────────────────────────────────────────────────────────

DECLARED_CURRENCY = {}   # wird in main() aus watchlist.yaml befüllt

def load_watchlist():
    with open(WATCHLIST) as f:
        data = yaml.safe_load(f)
    tickers  = [str(t).strip().upper() for t in data.get("tickers", [])]
    declared = {str(k).strip().upper(): str(v).strip().upper()
                for k, v in (data.get("declared_currency") or {}).items()}
    return tickers, declared

def prune_removed(con, tickers):
    """Löscht DB-Einträge von Tickern, die nicht mehr in der Watchlist stehen."""
    if not tickers:
        return
    marks   = ",".join("?" * len(tickers))
    removed = [r[0] for r in con.execute(
        f"SELECT DISTINCT ticker FROM dividends WHERE ticker NOT IN ({marks})", tickers)]
    if removed:
        con.execute(f"DELETE FROM dividends WHERE ticker NOT IN ({marks})", tickers)
        con.execute(f"DELETE FROM edgar_seen WHERE ticker NOT IN ({marks})", tickers)
        con.commit()
        print(f"Aus DB entfernt (nicht mehr in Watchlist): {', '.join(removed)}\n")

# ── Hauptprogramm ─────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*52}")
    print(f"  Dividend Tracker — {now}")
    print(f"{'='*52}\n")

    tickers, declared = load_watchlist()
    DECLARED_CURRENCY.update(declared)
    print(f"Watchlist: {len(tickers)} Ticker\n")

    con     = init_db()
    prune_removed(con, tickers)
    cik_map = get_cik_map()

    edgar_alerts  = []
    yf_alerts     = []
    edgar_tickers = set()
    company_names = {}   # Firmennamen aus EDGAR für yfinance-Phase wiederverwenden

    # ── curl_cffi-Session einmalig erstellen ─────────────────────────────────
    # Imitiert einen echten Chrome-Browser → verhindert Yahoo-Rate-Limiting
    # (429 YFRateLimitError), das insbesondere GitHub-Actions-IPs seit April 2025
    # betrifft. Die Session wird für ALLE yfinance-Calls wiederverwendet.
    try:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session(impersonate="chrome")
        print("  curl_cffi-Session aktiv (Chrome-Imitation)\n")
    except ImportError:
        print("  WARNUNG: curl_cffi nicht gefunden — Rate-Limiting möglich.")
        print("  Bitte 'pip install curl_cffi>=0.7' ausführen.\n")
        session = None

    # ── Phase 1: EDGAR ───────────────────────────────────────────────────────
    print("── Phase 1: EDGAR (Ankuendigungen) ──────────────────")
    for ticker in tickers:
        cik = cik_map.get(ticker)
        if not cik:
            print(f"  [{ticker}] kein US-CIK — wird nur via yfinance geprueft")
            company_names[ticker] = ticker   # Fallback: Ticker als Firmenname
            continue
        try:
            sub          = json.loads(http_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
            company_name = sub.get("name", ticker)
        except Exception:
            company_name = ticker
        company_names[ticker] = company_name   # Cache für Phase 2
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
        company_name = company_names.get(ticker, ticker)
        msg = check_yfinance(con, ticker, session, company_name)
        if msg and ticker not in edgar_tickers:
            yf_alerts.append(msg)
        time.sleep(YF_SLEEP_SEC)   # Pause nach jedem Ticker → verhindert Yahoo 429

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
