[README.md](https://github.com/user-attachments/files/26780049/README.md)
# Dividend Tracker

Automatisierter Dividenden-Tracker mit täglichem Monitoring via GitHub Actions.  
Erkennt Dividendenankündigungen und -änderungen und sendet sofort eine Telegram-Benachrichtigung.

---

## Wie es funktioniert

Der Tracker läuft täglich Mo–Fr um 08:00 Uhr MEZ vollautomatisch und prüft in zwei Phasen:

**Phase 1 — SEC EDGAR (US-Aktien)**  
Scannt neue 8-K Formulare bei der US-Börsenaufsicht. Unternehmen müssen Dividendenankündigungen dort einreichen — oft 3–6 Wochen vor dem Ex-Dividend-Date. Der Tracker erkennt das sofort.

**Phase 2 — yfinance (alle Ticker inkl. Europa)**  
Vergleicht die neueste Dividende in der Kurshistorie mit dem gespeicherten Wert. Greift auch für internationale Aktien, bei denen EDGAR nicht verfügbar ist.

Bei einer Änderung landet eine Nachricht direkt auf dem Handy via Telegram.

---

## Beispiel-Alert

```
Dividend Tracker — 14.04.2026

── Neue Ankündigungen (EDGAR) ──

📢 PepsiCo (PEP)
↑ Dividendenankündigung via SEC EDGAR
   Neue Dividende: $1.4850 pro Aktie
   Vorherige Dividende: $1.4230
   ➜ Das ist eine Erhöhung von 4.4%
   Eingereicht am: 2026-04-14

── Bestätigt via Kursdaten ──

↑ MSFT — Dividendenerhöhung (+9.6%)
   Neue Dividende: $0.91  (Ex-Date: 2026-02-19)
   Vorher:         $0.83  (Ex-Date: 2025-11-20)

2 Meldung(en) heute.
```

---

## Projektstruktur

```
dividend-tracker/
├── tracker.py              # Hauptskript (EDGAR + yfinance Logik)
├── watchlist.yaml          # Watchlist mit allen Tickern
├── requirements.txt        # Python-Abhängigkeiten
├── dividend_history.db     # SQLite-Datenbank (automatisch erstellt)
└── .github/
    └── workflows/
        └── daily.yml       # GitHub Actions Zeitplan
```

---

## Einrichtung

### 1. Repository klonen oder forken

### 2. Secrets in GitHub hinterlegen

`Settings → Secrets and variables → Actions → New repository secret`

| Secret | Beschreibung |
|---|---|
| `TELEGRAM_TOKEN` | Bot-Token von @BotFather |
| `TELEGRAM_CHAT_ID` | Deine persönliche Chat-ID |

**Telegram-Bot einrichten:**
1. @BotFather in Telegram suchen → `/newbot` → Token kopieren
2. Den Bot einmal anschreiben (`/start`)
3. Chat-ID ermitteln: `https://api.telegram.org/bot<TOKEN>/getUpdates` → `result[0].message.chat.id`

### 3. Watchlist anpassen

`watchlist.yaml` editieren. Ticker-Format je nach Börse:

| Börse | Format | Beispiel |
|---|---|---|
| US-Börsen (NYSE, NASDAQ) | Reiner Ticker | `MSFT`, `KO`, `V` |
| Xetra (Deutschland) | `.DE` | `ALV.DE`, `SAP.DE` |
| Euronext Paris | `.PA` | `MC.PA`, `OR.PA` |
| SIX (Schweiz) | `.SW` | `SIKA.SW`, `NOVN.SW` |
| Oslo | `.OL` | `HAUTO.OL` |
| Kopenhagen | `.CO` | `NOVO-B.CO` |

**Tipp:** Den korrekten Ticker immer auf [finance.yahoo.com](https://finance.yahoo.com) prüfen — der dort angezeigte Ticker funktioniert direkt.

### 4. Ersten Lauf starten

`Actions → Dividend Tracker → Run workflow`

Beim ersten Lauf werden Baselines gesetzt (kein Alert). Ab dem zweiten Lauf werden Änderungen erkannt und gemeldet.

---

## Automatischer Zeitplan

Der Workflow läuft Mo–Fr um 07:00 UTC (= 08:00 Uhr MEZ / 09:00 Uhr MESZ).

Den Zeitplan anpassen in `.github/workflows/daily.yml`:
```yaml
- cron: "0 7 * * 1-5"   # UTC-Zeit
```

---

## Watchlist erweitern

Einfach neue Ticker in `watchlist.yaml` eintragen:

```yaml
tickers:
  - AAPL
  - JNJ
  - OR.PA    # L'Oréal
  - WKL.AS   # Wolters Kluwer
```

Beim nächsten Lauf werden die neuen Titel automatisch erkannt und Baselines gesetzt.

---

## Datenbank

Die SQLite-Datenbank `dividend_history.db` speichert für jeden Ticker den letzten bekannten Dividendenwert und das Ex-Dividend-Date. Sie wird nach jedem Lauf automatisch ins Repository committet und dient als Gedächtnis für den Vergleich beim nächsten Lauf.

---

## Abhängigkeiten

```
yfinance    # Dividendenhistorie und Kursdaten
pyyaml      # Watchlist lesen
```

Kein bezahlter API-Key erforderlich. SEC EDGAR ist kostenlos und öffentlich zugänglich.
