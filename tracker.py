"""
Dividend Tracker
Kombiniert Nasdaq (angekündigte US-Dividenden), SEC EDGAR (8-K-Meldungen)
und yfinance (Dividendenhistorie, alle Börsen).

Ablauf pro Lauf:
  Phase 1  EDGAR    – neue 8-K-Meldungen der US-Werte (inkl. Anhang EX-99)
  Phase 2  pro Ticker:
             yfinance – Dividendenhistorie laden (splitbereinigt)
             Nasdaq   – angekündigte, noch nicht gezahlte US-Dividenden
             Ex-Date  – neue Zahlung in der Historie bewerten
  Ergebnis Telegram-Nachricht(en), Problemliste, montags Wochenstatus
"""

import os, re, sys, html, json, sqlite3, time, statistics, urllib.error, urllib.request, yaml
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

# ── Konfiguration ─────────────────────────────────────────────────────────────

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_LIMIT    = 4000   # Telegram erlaubt max. 4096 Zeichen pro Nachricht
IN_ACTIONS        = os.environ.get("GITHUB_ACTIONS") == "true"
DB_PATH           = Path("dividend_history.db")
WATCHLIST         = Path("watchlist.yaml")
MIN_CHANGE_PCT    = 0.5
ROUNDING_TOL      = 0.0005  # yfinance rundet Dividenden auf 3 Nachkommastellen (z. B. 0.0625 → 0.063)
FX_MIN_CHANGE_PCT = 2.0     # Schwelle für zurückgerechnete Beträge (Rückrechnung ist auf ca. ±1 % genau)
SPECIAL_FACTOR    = 2.5     # ab diesem Vielfachen der üblichen Zahlung: vermutlich Sonderdividende
EDGAR_LOOKBACK    = 7       # Tage; bereits gesehene Meldungen merkt sich die DB
HEARTBEAT_WEEKDAY = 0       # Montag: Wochenstatus auch ohne Meldungen
YF_FAIL_RATIO     = 0.25    # ab diesem Anteil fehlgeschlagener Ticker schlägt der Lauf fehl
YF_SLEEP_SEC      = 2.0     # Pause zwischen yfinance-Abfragen → verhindert Yahoo 429

# Die SEC verlangt eine echte Kontaktadresse im User-Agent
# (https://www.sec.gov/os/accessing-edgar-data) → GitHub-Secret SEC_CONTACT_EMAIL
SEC_CONTACT = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
SEC_UA      = f"DividendTracker private-use {SEC_CONTACT or 'admin@example.com'}"

# Wird in main() aus watchlist.yaml befüllt
DECLARED_CURRENCY = {}

# Probleme des Laufs → landen in der Telegram-Nachricht und im Actions-Log
PROBLEMS = []

def problem(text):
    print(f"  PROBLEM: {text}")
    PROBLEMS.append(text)

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

def is_us_ticker(ticker: str) -> bool:
    return "." not in ticker

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
        -- Angekündigte Dividenden (Nasdaq/EDGAR), damit sie nicht doppelt gemeldet werden
        CREATE TABLE IF NOT EXISTS announcements (
            ticker   TEXT NOT NULL,
            ex_date  TEXT NOT NULL,   -- ISO-Datum, leer wenn unbekannt (EDGAR)
            amount   REAL NOT NULL,
            special  INTEGER NOT NULL DEFAULT 0,
            source   TEXT NOT NULL,
            seen_at  TEXT NOT NULL,
            PRIMARY KEY (ticker, ex_date, source)
        );
        CREATE TABLE IF NOT EXISTS names (
            ticker   TEXT PRIMARY KEY,
            name     TEXT NOT NULL
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

def save_announcement(con, ticker, ex_date, amount, special, source):
    con.execute(
        "INSERT OR REPLACE INTO announcements (ticker, ex_date, amount, special, source, seen_at) "
        "VALUES (?,?,?,?,?,?)",
        (ticker, ex_date or "", amount, int(special), source, datetime.now(timezone.utc).isoformat())
    )
    con.commit()

def find_announcement(con, ticker, amount=None, ex_date=None, within_days=75):
    """Sucht eine bereits gespeicherte Ankündigung zu derselben Zahlung.
    Treffer, wenn das Ex-Date bis auf 3 Tage passt oder (ohne Ex-Date) der Betrag
    gleich ist und die Ankündigung höchstens `within_days` alt ist.
    Rückgabe: (ex_date, amount, special, source) oder None.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat()
    rows = con.execute(
        "SELECT ex_date, amount, special, source FROM announcements "
        "WHERE ticker=? AND seen_at>=? ORDER BY seen_at DESC", (ticker, cutoff)
    ).fetchall()
    for row in rows:
        if ex_date and row[0]:
            if abs(days_between(ex_date, row[0])) <= 3:
                return row
        elif amount is not None and not amounts_differ(amount, row[1]):
            return row
    return None

def get_name(con, ticker):
    row = con.execute("SELECT name FROM names WHERE ticker=?", (ticker,)).fetchone()
    return row[0] if row else None

def save_name(con, ticker, name):
    con.execute("INSERT OR REPLACE INTO names (ticker, name) VALUES (?,?)", (ticker, name))
    con.commit()

# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": SEC_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def strip_html(raw):
    text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="ignore")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)   # &#160; → Leerzeichen, &amp; → & usw.
    return re.sub(r"\s+", " ", text).strip()

def esc(text):
    """Maskiert &, < und > für Telegram (parse_mode HTML), z. B. in 'S&P Global'."""
    return html.escape(str(text), quote=False)

def clean_company_name(name):
    """'AMPHENOL CORP /DE/' → 'AMPHENOL CORP' (SEC hängt teils den Gründungsstaat an)."""
    return re.sub(r"\s*/[A-Z]{2,3}/?\s*$", "", name or "").strip()

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

def days_between(a, b):
    return (date.fromisoformat(a) - date.fromisoformat(b)).days

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [Telegram] Keine Zugangsdaten gesetzt")
        return not IN_ACTIONS   # lokal kein Fehler, in GitHub Actions schon
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
        return ok
    except urllib.error.HTTPError as e:
        # Telegram nennt den Grund im Body, z. B. "can't parse entities"
        print(f"  [Telegram] Fehler {e.code}: {e.read().decode(errors='ignore')[:300]}")
    except Exception as e:
        print(f"  [Telegram] Fehler: {e}")
    return False

def split_message(blocks, limit=TELEGRAM_LIMIT):
    """Packt Textblöcke (Überschriften, einzelne Meldungen) in Nachrichten ≤ limit.
    Eine Meldung wird nie mittendrin getrennt; ein einzelner, zu langer Block wird gekürzt.
    """
    chunks, cur = [], ""
    for block in blocks:
        block = block if len(block) <= limit else block[:limit - 1] + "…"
        candidate = f"{cur}\n\n{block}" if cur else block
        if len(candidate) > limit:
            chunks.append(cur)
            cur = block
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    if len(chunks) > 1:
        chunks = [f"{c}\n\n<i>(Teil {i}/{len(chunks)})</i>" for i, c in enumerate(chunks, 1)]
    return chunks

# ── Datumsformat ─────────────────────────────────────────────────────────────

def fmt_date_dmy(iso_date: str) -> str:
    """Konvertiert ISO-Datum (YYYY-MM-DD) zu TT-MM-JJJJ."""
    try:
        dt = datetime.strptime(iso_date[:10], "%Y-%m-%d")
        return dt.strftime("%d-%m-%Y")
    except ValueError:
        return iso_date

# ── Dividendenfrequenz ────────────────────────────────────────────────────────

FREQ_GAP = {"Monatlich": 30, "Quartalsweise": 91, "Halbjährlich": 182, "Jährlich": 365}

def avg_gap(history: list) -> float | None:
    """Mittlerer Abstand der letzten (bis zu 5) Ex-Dates in Tagen."""
    gaps = [days_between(history[i][0], history[i + 1][0])
            for i in range(min(5, len(history) - 1))]
    return sum(gaps) / len(gaps) if gaps else None

def detect_frequency(history: list) -> str | None:
    """Leitet die Zahlungsfrequenz aus den letzten Ex-Date-Abständen ab.
    history: [(ex_date_iso, amount), ...] absteigend sortiert.
    """
    avg = avg_gap(history)
    if avg is None:
        return None
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

# ── Sonderdividenden ──────────────────────────────────────────────────────────

def detect_specials(history: list) -> set:
    """Ex-Dates, die vermutlich Sonderdividenden sind.

    Weder yfinance noch Nasdaq kennzeichnen Sonderdividenden (Costcos 15 $ im
    Dezember 2023 steht dort wie eine normale Zahlung). Erkennung daher über:
      - Betrag ≥ SPECIAL_FACTOR × Median der Zahlungen im Umkreis von ±400 Tagen
      - und keine ähnlich hohe Zahlung (≥ 60 %) im Umkreis von gut einem Jahr.
        Sonst ist es eine reguläre, saisonal höhere Schlussdividende (z. B. ASML)
        oder ein neues, dauerhaft höheres Niveau (z. B. NVIDIA 2026: 0.01 → 0.25).
    Die allererste erhöhte Zahlung lässt sich davon nicht unterscheiden → die
    Meldung nennt beide Möglichkeiten.
    """
    specials = set()
    for ex_date, amount in history:
        near = [(e, a) for e, a in history if e != ex_date]
        neighbours = [a for e, a in near if abs(days_between(e, ex_date)) <= 400]
        if len(neighbours) < 2:
            continue   # zu wenig Vergleichswerte, z. B. jährliche Zahler
        base = statistics.median(neighbours)
        if base <= 0 or amount < SPECIAL_FACTOR * base:
            continue
        similar = any(abs(days_between(e, ex_date)) <= 425 and a >= 0.6 * amount
                      for e, a in near)
        if not similar:
            specials.add(ex_date)
    return specials

# ── Bewertung ─────────────────────────────────────────────────────────────────

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
    if not (330 <= days_between(history[0][0], history[n][0]) <= 400
            and 330 <= days_between(history[n][0], history[2 * n][0]) <= 400):
        return None
    cur  = sum(a for _, a in history[:n])
    prev = sum(a for _, a in history[n:2 * n])
    return (cur, prev) if prev > 0 else None

def evaluate_change(history, frequency=None, differ_pct=0.0, specials=frozenset()):
    """Bewertet die neueste Zahlung gegenüber der Historie.

    0. Ist sie eine (vermutliche) Sonderdividende → immer melden, verglichen
       mit der letzten regulären Zahlung.
    1. Sonderdividenden fließen nicht in Vergleiche ein.
    2. Gleicht sie der vorherigen regulären Zahlung (bis auf Rundung) → keine Änderung.
    3. Gibt es eine Zahlung im Vorjahreszeitraum, wird mit dieser verglichen.
       So werden schwankende Interims-/Schlussdividenden (ASML, LVMH, Novo)
       nicht als Kürzung oder Erhöhung gemeldet.
    4. Sonst Vergleich mit der vorherigen Zahlung.
    Rückgabe: dict(curr, prev, yoy, pct, ttm, special) bei einer Meldung, sonst None.
    """
    curr    = history[0]
    regular = [h for h in history[1:] if h[0] not in specials]
    if not regular:
        return None
    if frequency is None:
        frequency = detect_frequency([curr] + regular)

    if curr[0] in specials:
        prev = regular[0]
        return dict(curr=curr, prev=prev, yoy=None, pct=pct_change(curr[1], prev[1]),
                    ttm=None, special=True)

    series = [curr] + regular
    prev   = series[1]
    if prev[1] == 0 or not amounts_differ(curr[1], prev[1], differ_pct):
        return None
    yoy = find_yoy(series, frequency)
    ref = yoy or prev
    if ref[1] == 0 or not amounts_differ(curr[1], ref[1], differ_pct):
        return None
    pct = pct_change(curr[1], ref[1])
    if abs(pct) < max(MIN_CHANGE_PCT, differ_pct):
        return None
    return dict(curr=curr, prev=prev, yoy=yoy, pct=pct,
                ttm=ttm_sums(series, frequency), special=False)

def overdue_days(history, specials=frozenset(), today=None):
    """Tage, die die nächste reguläre Zahlung überfällig ist (None = nicht überfällig).
    Ignoriert Ticker, deren letzte Zahlung über 3 Jahre zurückliegt (keine Dividendenzahler).
    """
    regular = [h for h in history if h[0] not in specials]
    if len(regular) < 3:
        return None
    today = today or date.today()
    since = (today - date.fromisoformat(history[0][0])).days
    gap   = FREQ_GAP.get(detect_frequency(regular), 365)
    late  = since - gap
    if since > 3 * 365 or late <= gap * 0.5 + 30:
        return None
    return late

# ── Ex-Date aus EDGAR-Text ────────────────────────────────────────────────────

_EX_DATE_TEXT_RE = [
    r"ex.dividend\s+date\s+(?:will\s+be\s+|is\s+|of\s+)?([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
    r"ex.date\s+(?:will\s+be\s+|is\s+|of\s+)?([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
    r"ex.dividend\s+date\s+of\s+(\d{4}-\d{2}-\d{2})",
    r"ex-dividend\s+date\s+(?:will\s+be\s+)?(\d{1,2}/\d{1,2}/\d{2,4})",
]

def extract_ex_date_edgar(text: str) -> str | None:
    """Extrahiert das Ex-Dividende-Datum aus einem EDGAR-Meldetext.
    Gibt das Datum als ISO-Datum (JJJJ-MM-TT) zurück oder None.
    """
    for pat in _EX_DATE_TEXT_RE:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        raw = m.group(1).strip()
        for fmt_str in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y",
                        "%b %d %Y", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(raw, fmt_str).date().isoformat()
            except ValueError:
                pass
    return None

# ── Nachrichtenformat ─────────────────────────────────────────────────────────

SPECIAL_EXPLICIT = "💰 <b>Sonderdividende</b> (laut Ankündigung)"
SPECIAL_GUESS    = ("⚠️ <b>Vermutlich Sonderdividende</b> oder starke Erhöhung "
                    f"(mind. {SPECIAL_FACTOR:g}× die übliche Zahlung, nicht ausdrücklich gekennzeichnet)")

def msg_announcement(ticker, company, source, amount, old_amount, change_pct, currency='$',
                     ex_date=None, pay_date=None, decl_date=None, frequency=None,
                     special=None, yoy=None):
    """Angekündigte Dividende (Nasdaq oder EDGAR), noch vor dem Ex-Date.
    special: None, 'explicit' oder 'guess'.
    """
    up    = (change_pct or 0) >= 0
    lines = [f"📢 <b>{esc(company)} ({esc(ticker)})</b>"]
    lines.append(f"{'↑' if up else '↓'} Dividendenankündigung via {source}")
    if special:
        lines.append(f"   {SPECIAL_EXPLICIT if special == 'explicit' else SPECIAL_GUESS}")
    lines.append(f"   Neue Dividende: <b>{currency}{fmt(amount)}</b> pro Aktie")
    if special and old_amount:
        lines.append(f"   Letzte reguläre Dividende: {currency}{fmt(old_amount)}")
    elif yoy:
        lines.append(f"   Vorjahreszeitraum: {currency}{fmt(yoy[1])}  (Ex-Date: {fmt_date_dmy(yoy[0])})")
        if old_amount:
            lines.append(f"   Vorherige Zahlung: {currency}{fmt(old_amount)}")
    elif old_amount:
        lines.append(f"   Vorherige Dividende: {currency}{fmt(old_amount)}")
    if change_pct is not None and not special:
        word  = "Erhöhung" if change_pct > 0 else "Senkung"
        basis = " ggü. Vorjahr" if yoy else ""
        lines.append(f"   ➜ Das ist eine <b>{word} von {abs(change_pct):.1f}%</b>{basis}")
    if decl_date:
        lines.append(f"   Angekündigt am:  {fmt_date_dmy(decl_date)}")
    if ex_date:
        lines.append(f"   Ex-Dividende:    {fmt_date_dmy(ex_date)}")
    if pay_date:
        lines.append(f"   Zahltag:         {fmt_date_dmy(pay_date)}")
    if frequency:
        lines.append(f"   Zahlungsweise:   {frequency}")
    return "\n".join(lines)

def msg_yfinance(ticker, company, result, currency='$', frequency=None,
                 split_note=None, approx=False, special_explicit=False):
    """Neue Zahlung aus der yfinance-Historie, in der Heimatwährung der Aktie.
    result: Rückgabe von evaluate_change().
    """
    curr, prev, yoy, change_pct = result["curr"], result["prev"], result["yoy"], result["pct"]
    special = special_explicit or result["special"]
    arrow  = "↑" if change_pct > 0 else "↓"
    word   = "Erhöhung" if change_pct > 0 else "Senkung"
    ca     = "ca. " if approx else ""
    cur    = lambda v: f"{ca}{currency}{fmt(v)}"
    lines  = [f"📢 <b>{esc(company)} ({esc(ticker)})</b>"]
    lines.append(f"{'💰' if special else arrow} Neue Dividende laut Kursdaten (yfinance)")
    if special:
        lines.append(f"   {SPECIAL_EXPLICIT if special_explicit else SPECIAL_GUESS}")
    lines.append(f"   Neue Dividende: <b>{cur(curr[1])}</b> pro Aktie  (Ex-Date: {fmt_date_dmy(curr[0])})")
    if special:
        lines.append(f"   Letzte reguläre Dividende: {cur(prev[1])}  (Ex-Date: {fmt_date_dmy(prev[0])})")
    elif yoy:
        lines.append(f"   Vorjahreszeitraum: {cur(yoy[1])}  (Ex-Date: {fmt_date_dmy(yoy[0])})")
        lines.append(f"   ➜ Das ist eine <b>{word} von {abs(change_pct):.1f}%</b> ggü. Vorjahr")
        prev_pct = pct_change(curr[1], prev[1])
        lines.append(f"   Vorherige Zahlung: {cur(prev[1])}  (Ex-Date: {fmt_date_dmy(prev[0])}, {prev_pct:+.1f}%)")
    else:
        lines.append(f"   Vorherige Dividende: {cur(prev[1])}  (Ex-Date: {fmt_date_dmy(prev[0])})")
        lines.append(f"   ➜ Das ist eine <b>{word} von {abs(change_pct):.1f}%</b>")
    if result.get("ttm"):
        ttm = result["ttm"]
        lines.append(f"   Summe 12 Monate: {cur(ttm[0])}  (Vorjahr: {cur(ttm[1])}, {pct_change(*ttm):+.1f}%)")
    if frequency:
        lines.append(f"   Zahlungsweise:   {frequency}")
    if split_note:
        lines.append(f"   Hinweis: {split_note}")
    if approx:
        lines.append(f"   Hinweis: in Ankündigungswährung zurückgerechnet, Abweichung ca. ±1 %")
    return "\n".join(lines)

def build_messages(ann_alerts, ex_alerts, problems=(), status=None):
    """Baut die Telegram-Nachricht(en); bei Bedarf auf mehrere Teile verteilt."""
    today  = date.today().strftime("%d.%m.%Y")
    blocks = [f"<b>Dividend Tracker — {today}</b>"]
    if ann_alerts:
        blocks.append("<b>── Neue Ankündigungen ──</b>")
        blocks.extend(ann_alerts)
    if ex_alerts:
        blocks.append("<b>── Neue Ex-Dates (Kursdaten) ──</b>")
        blocks.extend(ex_alerts)
    if problems:
        blocks.append("<b>── ⚠️ Probleme ──</b>")
        blocks.append("\n".join(f"• {esc(p)}" for p in problems))
    if status:
        blocks.append("<b>── ✅ Wochenstatus ──</b>")
        blocks.append(status)
    blocks.append(f"{len(ann_alerts) + len(ex_alerts)} Meldung(en) heute.")
    return split_message(blocks)

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
        problem(f"SEC-Tickerverzeichnis nicht ladbar ({type(e).__name__}) → EDGAR übersprungen")
    return _cik_cache

_NUM = r"\$\s?(\d*\.?\d+)"
AMOUNT_RE = [
    r"declared\s+(?:a|an)\s+(?:regular\s+|special\s+|increased\s+|one-time\s+)?"
    r"(?:quarterly\s+|monthly\s+|annual\s+|semi.annual\s+|interim\s+|final\s+)?"
    r"(?:cash\s+)?dividend\s+of\s+" + _NUM,
    r"(?:quarterly|monthly|annual|special)\s+(?:cash\s+)?dividend\s+(?:of|to)\s+" + _NUM
    + r"\s+per\s+(?:common\s+)?share",
    r"dividend\s+of\s+" + _NUM + r"\s+per\s+(?:common\s+)?share",
]

CHANGE_RE = [
    (r"(\d+(?:\.\d+)?)\s*(?:percent|%)\s+increase",                 "increase"),
    (r"increase[^.]{0,30}?(\d+(?:\.\d+)?)\s*(?:percent|%)",         "increase"),
    (r"(\d+(?:\.\d+)?)\s*(?:percent|%)\s+(?:decrease|reduction)",   "decrease"),
    (r"increase[d]?\s+(?:the\s+)?(?:quarterly\s+)?dividend\s+by\s+(\d+(?:\.\d+)?)\s*(?:percent|%)", "increase"),
]

SPECIAL_RE = r"\b(?:special|one-time|extraordinary)\s+(?:cash\s+)?dividend"

def dividend_sentences(text):
    """Nur Sätze mit 'dividend' – verhindert, dass z. B. ein Umsatzplus von 24 %
    aus einem Quartalsbericht als Dividendenerhöhung gelesen wird.
    """
    return " ".join(s for s in re.split(r"(?<=[.;])\s+", text) if "dividend" in s.lower())

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

def is_special_text(text):
    return bool(re.search(SPECIAL_RE, text, re.I))

def filing_text(cik, adsh, primary_doc):
    """Text des 8-K inkl. Anhängen EX-99 (dort steht meist die Pressemitteilung)."""
    base  = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace('-', '')}/"
    parts = [strip_html(http_get(base + primary_doc, timeout=12))]
    time.sleep(0.12)
    try:
        index = json.loads(http_get(base + "index.json", timeout=12))
        exhibits = [x["name"] for x in index["directory"]["item"]
                    if re.search(r"ex-?99", x["name"], re.I) and x["name"].lower().endswith((".htm", ".html", ".txt"))]
        for name in exhibits[:3]:
            time.sleep(0.12)
            parts.append(strip_html(http_get(base + name, timeout=12)))
    except Exception as e:
        print(f"  EDGAR-Anhänge nicht ladbar: {type(e).__name__}")
    return " ".join(parts)

def scan_edgar(con, ticker, cik, company_name, sub):
    """Prüft neue 8-K-Meldungen eines US-Werts auf Dividendenankündigungen.
    sub: bereits geladene submissions-JSON (spart einen Abruf pro Ticker).
    """
    alerts = []
    cutoff = (date.today() - timedelta(days=EDGAR_LOOKBACK)).isoformat()
    rec    = sub["filings"]["recent"]
    forms  = rec.get("form", [])
    dates  = rec.get("filingDate", [])
    accs   = rec.get("accessionNumber", [])
    pdocs  = rec.get("primaryDocument", [""] * len(forms))

    for i in range(len(forms)):
        if dates[i] < cutoff:
            break
        if forms[i] != "8-K":
            continue
        adsh = accs[i]
        if seen(con, adsh):
            continue
        doc = pdocs[i] if i < len(pdocs) else ""
        if not doc:
            mark_seen(con, adsh, ticker)
            continue
        try:
            text = filing_text(cik, adsh, doc)
        except Exception as e:
            # Nicht als gesehen markieren → nächster Lauf versucht es erneut
            print(f"  EDGAR-Dokument nicht ladbar ({type(e).__name__}) – neuer Versuch beim nächsten Lauf")
            continue
        mark_seen(con, adsh, ticker)

        div_text = dividend_sentences(text)
        if not div_text:
            continue
        amount = extract_amount(div_text)
        if amount is None:
            print(f"  EDGAR {dates[i]}: Dividende erwähnt, aber kein Betrag erkannt")
            continue

        special = is_special_text(div_text)
        ex_date = extract_ex_date_edgar(div_text)
        if find_announcement(con, ticker, amount=amount, ex_date=ex_date):
            print(f"  EDGAR: ${amount:.4f} — bereits bekannt")
            continue

        stored     = get_stored(con, ticker, limit=1)
        old_amount = stored[0][1] if stored else None
        # yfinance-Werte sind auf 3 Nachkommastellen gerundet (VRT: 0.0625 vs. 0.063)
        # → Abweichungen im Rundungsbereich gelten nicht als Änderung
        if old_amount and old_amount > 0 and amounts_differ(amount, old_amount):
            calc_pct = pct_change(amount, old_amount)
        else:
            calc_pct = None
        reported_pct = extract_change(div_text) or calc_pct
        save_announcement(con, ticker, ex_date, amount, special, "edgar")

        is_change = reported_pct is not None and abs(reported_pct) >= MIN_CHANGE_PCT
        if not (special or is_change or old_amount is None):
            print(f"  EDGAR: ${amount:.4f} — keine Aenderung")
            continue

        frequency = extract_frequency_edgar(div_text)
        pct_str   = f"{reported_pct:+.1f}%" if reported_pct else ("Sonderdividende" if special else "neu")
        print(f"  *** EDGAR-Ankuendigung: ${amount:.4f} ({pct_str}) | Ex-Date: {ex_date or '–'} ***")
        alerts.append(msg_announcement(ticker, company_name, "SEC EDGAR", amount, old_amount,
                                       reported_pct, "$", ex_date=ex_date, decl_date=dates[i],
                                       frequency=frequency,
                                       special="explicit" if special else None))
    return alerts

# ── Nasdaq (angekündigte US-Dividenden) ───────────────────────────────────────

def _us_date(s):
    try:
        return datetime.strptime(s, "%m/%d/%Y").date().isoformat()
    except (TypeError, ValueError):
        return None

def fetch_nasdaq(ticker, session):
    """Dividenden laut Nasdaq, inkl. angekündigter, noch nicht gezahlter.
    Rückgabe: [dict(ex_date, amount, decl_date, pay_date)], absteigend nach Ex-Date.
    """
    r = session.get(f"https://api.nasdaq.com/api/quote/{ticker}/dividends",
                    params={"assetclass": "stocks"}, timeout=20)
    data = (r.json().get("data") or {}).get("dividends") or {}
    rows = []
    for row in data.get("rows") or []:
        ex_date = _us_date(row.get("exOrEffDate"))
        try:
            amount = float(str(row.get("amount", "")).replace("$", "").replace(",", ""))
        except ValueError:
            continue
        if ex_date and row.get("currency", "USD") == "USD":
            rows.append(dict(ex_date=ex_date, amount=amount,
                             decl_date=_us_date(row.get("declarationDate")),
                             pay_date=_us_date(row.get("paymentDate"))))
    return sorted(rows, key=lambda x: x["ex_date"], reverse=True)

def check_nasdaq(con, ticker, session, company_name, history):
    """Meldet angekündigte Dividenden, deren Ex-Date noch nicht in der yfinance-Historie ist.
    So wird z. B. Microsofts Erhöhung auf 0,98 $ (angekündigt 15.09.2026, Ex-Date
    19.11.2026) zwei Monate früher erkannt. Rückgabe: (alerts, ok)
    """
    try:
        rows = fetch_nasdaq(ticker, session)
    except Exception as e:
        print(f"  Nasdaq-Fehler: {type(e).__name__}: {e}")
        return [], False
    known  = history[0][0] if history else ""
    alerts = []
    for row in rows:
        if row["ex_date"] <= known or row["ex_date"] < (date.today() - timedelta(days=3)).isoformat():
            break   # bereits in der Historie oder zu alt
        if find_announcement(con, ticker, ex_date=row["ex_date"]):
            continue
        prior = find_announcement(con, ticker, amount=row["amount"], within_days=30)
        if not history:
            save_announcement(con, ticker, row["ex_date"], row["amount"], False, "nasdaq")
            continue
        hyp      = [(row["ex_date"], row["amount"])] + history
        specials = detect_specials(hyp)
        result   = evaluate_change(hyp, specials=specials)
        special  = bool(prior and prior[2]) or row["ex_date"] in specials
        save_announcement(con, ticker, row["ex_date"], row["amount"], special, "nasdaq")
        if prior:
            print(f"  Nasdaq: ${row['amount']:.4f} — bereits via {prior[3]} gemeldet")
            continue
        if result is None:
            print(f"  Nasdaq: ${row['amount']:.4f} angekuendigt (Ex {row['ex_date']}) — keine Aenderung")
            continue
        print(f"  *** Nasdaq-Ankuendigung: ${row['amount']:.4f} ({result['pct']:+.1f}%) ***")
        alerts.append(msg_announcement(
            ticker, company_name, "Nasdaq", row["amount"], result["prev"][1],
            result["pct"], "$", ex_date=row["ex_date"], pay_date=row["pay_date"],
            decl_date=row["decl_date"], frequency=detect_frequency(history),
            special="guess" if result["special"] else None,
            yoy=result["yoy"]))
    return alerts, True

# ── yfinance ──────────────────────────────────────────────────────────────────

def fetch_history(ticker, session):
    """Lädt die (splitbereinigte) Dividendenhistorie, absteigend sortiert.
    Rückgabe: (tk, history, status) mit status 'ok', 'no_dividends', 'invalid' oder 'error'.
    """
    try:
        import yfinance as yf
        import pandas as pd

        tk  = yf.Ticker(ticker, session=session)
        raw = tk.dividends
        if raw is None or (hasattr(raw, 'empty') and raw.empty):
            # Kein Dividendenzahler oder falscher Ticker? Kursdaten unterscheiden das.
            prices = tk.history(period="1mo")
            return tk, [], "no_dividends" if not prices.empty else "invalid"

        # yfinance >= 0.2.x gibt DataFrame zurück, ältere Versionen Series
        if isinstance(raw, pd.DataFrame):
            col   = raw.columns[0]
            items = [(idx, float(row[col])) for idx, row in raw.iterrows()]
        else:
            items = [(ts, float(v)) for ts, v in raw.items()]

        history = []
        for ts, amount in items:
            ex_date = ts.date().isoformat() if hasattr(ts, 'date') else str(ts)[:10]
            history.append((ex_date, amount))
        history.sort(reverse=True)
        return tk, history, "ok"

    except Exception as e:
        # Explizites Logging statt stilles Schlucken – sichtbar im Actions-Log
        print(f"  [{ticker}] yfinance-Fehler: {type(e).__name__}: {e}")
        return None, [], "error"

def check_yfinance(con, ticker, tk, history, session, company_name):
    """Prüft, ob die Historie ein neues Ex-Date enthält, und bewertet die Zahlung.
    Rückgabe: Meldungstext oder None.
    """
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
    specials  = detect_specials(history)
    frequency = detect_frequency([h for h in history if h[0] not in specials])

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

    result = evaluate_change(history, frequency, differ_pct, specials)

    # Bereits angekündigt (Nasdaq/EDGAR) und Betrag gleich → nicht erneut melden
    # (Abgleich über Ex-Date oder, bei EDGAR-Meldungen ohne Ex-Date, über den Betrag)
    ann = find_announcement(con, ticker, amount=latest_amount, ex_date=latest_date, within_days=120)
    explicit = bool(ann and ann[2])
    if ann and not declared and not amounts_differ(latest_amount, ann[1]):
        print(f"  Neues Ex-Date {latest_date} — bereits via {ann[3]} angekuendigt")
        return None

    if result is None and not explicit:
        print(f"  Unveraendert: {currency}{latest_amount:.4f} ({latest_date})")
        return None
    if result is None:   # laut Ankündigung Sonderdividende, Betrag aber unauffällig
        prev   = next((h for h in history[1:] if h[0] not in specials), history[1])
        result = dict(curr=history[0], prev=prev, yoy=None,
                      pct=pct_change(latest_amount, prev[1]), ttm=None, special=True)

    label = "Sonderdividende" if result["special"] or explicit else (
        "Erhoehung" if result["pct"] > 0 else "Senkung")
    print(f"  *** {label}: {result['pct']:+.2f}% | {company_name} | {frequency or '–'} ***")
    return msg_yfinance(ticker, company_name, result, currency, frequency,
                        recent_split_note(tk), approx=bool(declared),
                        special_explicit=explicit)

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
        problem(f"{tk.ticker}: Währungsumrechnung fehlgeschlagen ({type(e).__name__})")
        return []

def resolve_name(con, ticker, tk):
    """Firmenname für Nicht-US-Werte: einmalig aus yfinance holen und in der DB merken."""
    name = get_name(con, ticker)
    if name or tk is None:
        return name or ticker
    try:
        info = tk.get_info()
        name = info.get("longName") or info.get("shortName")
        # shortName ist teils mit Leerzeichen aufgefüllt: "Allianz SE                    v"
        name = re.split(r"\s{2,}", name.strip())[0] if name else None
    except Exception as e:
        print(f"  Firmenname nicht ladbar: {type(e).__name__}")
        name = None
    if name:
        save_name(con, ticker, name)
    return name or ticker

# ── Watchlist ─────────────────────────────────────────────────────────────────

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
        for table in ("dividends", "edgar_seen", "announcements", "names"):
            con.execute(f"DELETE FROM {table} WHERE ticker NOT IN ({marks})", tickers)
        con.commit()
        print(f"Aus DB entfernt (nicht mehr in Watchlist): {', '.join(removed)}\n")

# ── Hauptprogramm ─────────────────────────────────────────────────────────────

def main() -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*52}")
    print(f"  Dividend Tracker — {now}")
    print(f"{'='*52}\n")

    tickers, declared = load_watchlist()
    DECLARED_CURRENCY.update(declared)
    print(f"Watchlist: {len(tickers)} Ticker\n")
    if not SEC_CONTACT:
        print("  HINWEIS: SEC_CONTACT_EMAIL nicht gesetzt — SEC kann generische Absender sperren.\n")

    con     = init_db()
    prune_removed(con, tickers)
    cik_map = get_cik_map()

    ann_alerts    = []
    ex_alerts     = []
    company_names = {}   # Firmennamen aus EDGAR für Phase 2 wiederverwenden

    # ── curl_cffi-Session einmalig erstellen ─────────────────────────────────
    # Imitiert einen echten Chrome-Browser → verhindert Yahoo-Rate-Limiting
    # (429 YFRateLimitError), das insbesondere GitHub-Actions-IPs seit April 2025
    # betrifft. Die Session wird für ALLE yfinance- und Nasdaq-Calls wiederverwendet.
    try:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session(impersonate="chrome")
        print("  curl_cffi-Session aktiv (Chrome-Imitation)\n")
    except ImportError:
        problem("curl_cffi nicht installiert → Yahoo-Rate-Limiting und kein Nasdaq-Abruf")
        session = None

    # ── Phase 1: EDGAR ───────────────────────────────────────────────────────
    print("── Phase 1: EDGAR (8-K-Meldungen) ───────────────────")
    edgar_errors = 0
    for ticker in tickers:
        cik = cik_map.get(ticker)
        if not cik:
            continue
        try:
            sub = json.loads(http_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
        except Exception as e:
            print(f"  [{ticker}] EDGAR-Fehler: {type(e).__name__}: {e}")
            edgar_errors += 1
            continue
        company_name = clean_company_name(sub.get("name")) or ticker
        company_names[ticker] = company_name   # Cache für Phase 2
        print(f"  [{ticker}] {company_name}")
        ann_alerts.extend(scan_edgar(con, ticker, cik, company_name, sub))
        time.sleep(0.1)
    if edgar_errors:
        problem(f"EDGAR: {edgar_errors} Ticker nicht abrufbar")

    # ── Phase 2: yfinance + Nasdaq ───────────────────────────────────────────
    print("\n── Phase 2: yfinance + Nasdaq ───────────────────────")
    failed, invalid, nasdaq_checked, nasdaq_errors, overdue = [], [], 0, 0, []
    for ticker in tickers:
        print(f"  [{ticker}]")
        tk, history, status = fetch_history(ticker, session)
        company_name = company_names.get(ticker) or resolve_name(con, ticker, tk)
        if status == "error":
            failed.append(ticker)
        elif status == "invalid":
            invalid.append(ticker)
        if is_us_ticker(ticker) and session is not None and status != "invalid":
            alerts, ok = check_nasdaq(con, ticker, session, company_name, history)
            ann_alerts.extend(alerts)
            nasdaq_checked += 1
            nasdaq_errors  += not ok
        if history:
            msg = check_yfinance(con, ticker, tk, history, session, company_name)
            if msg:
                ex_alerts.append(msg)
            late = overdue_days(history, detect_specials(history))
            if late:
                overdue.append(f"{ticker} ({late} Tage)")
        time.sleep(YF_SLEEP_SEC)   # Pause nach jedem Ticker → verhindert Yahoo 429

    if failed:
        problem(f"yfinance-Fehler bei {len(failed)} Ticker(n): {', '.join(failed)}")
    if invalid:
        problem(f"Keine Kursdaten (Ticker falsch oder delistet?): {', '.join(invalid)}")
    if nasdaq_checked and nasdaq_errors == nasdaq_checked:
        problem("Nasdaq nicht erreichbar → keine Frühwarnung für US-Werte")

    # ── Ergebnis ──────────────────────────────────────────────────────────────
    print(f"\n{'='*52}")
    print(f"  Ankuendigungen      : {len(ann_alerts)}")
    print(f"  Neue Ex-Dates       : {len(ex_alerts)}")
    print(f"  Probleme            : {len(PROBLEMS)}")
    print(f"{'='*52}\n")

    status = None
    if date.today().weekday() == HEARTBEAT_WEEKDAY:
        ok_count = len(tickers) - len(failed) - len(invalid)
        status = f"Tracker läuft: {ok_count}/{len(tickers)} Ticker erfolgreich geprüft."
        if overdue:
            status += ("\nÜberfällige Dividende (ausgesetzt?): " + esc(", ".join(overdue)))

    exit_code = 0
    if ann_alerts or ex_alerts or PROBLEMS or status:
        messages = build_messages(ann_alerts, ex_alerts, PROBLEMS, status)
        for message in messages:
            print("Telegram:\n" + message + "\n")
            if not send_telegram(message):
                exit_code = 1
    else:
        print("Keine Aenderungen heute — kein Alert.")

    # Lauf als fehlgeschlagen markieren → GitHub verschickt eine E-Mail
    if len(failed) >= max(1, YF_FAIL_RATIO * len(tickers)) or not cik_map:
        exit_code = 1
    con.close()
    print(f"\n=== Fertig (Exit-Code {exit_code}) ===")
    return exit_code

if __name__ == "__main__":
    sys.exit(main())
