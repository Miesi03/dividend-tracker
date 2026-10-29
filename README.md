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
Erkennt neue Ex-Dates in der Dividendenhistorie und bewertet die neue Zahlung. Greift auch für internationale Aktien, bei denen EDGAR nicht verfügbar ist.

**So wird verglichen:**
- Entspricht die neue Zahlung der vorherigen, gibt es keine Meldung. Rundungsabweichungen bis 0,0005 werden ignoriert, weil yfinance auf 3 Nachkommastellen rundet (z. B. 0,0625 $ → 0,063 $).
- Weicht sie ab, wird mit der Zahlung aus dem **Vorjahreszeitraum** verglichen. So werden schwankende Interims- und Schlussdividenden (z. B. ASML, LVMH, Novo Nordisk) nicht als Kürzung oder Erhöhung gemeldet, eine echte Erhöhung aber schon.
- Gibt es keine Vorjahreszahlung (z. B. bei jährlicher Zahlung), wird mit der vorherigen Zahlung verglichen.
- Verglichen wird nur innerhalb der yfinance-Historie, die nach Aktiensplits rückwirkend bereinigt ist. Ein Split (z. B. Amphenol 2:1) löst daher keine Schein-Kürzung aus.
- Zusätzlich zeigt die Meldung die Summe der letzten 12 Monate im Vergleich zum Vorjahr.

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
| Euronext Amsterdam | `.AS` | `ASML.AS` |
| Borsa Italiana (Mailand) | `.MI` | `RACE.MI` |
| Taiwan | `.TW` | `2330.TW` |
| Korea (KOSPI) | `.KS` | `005930.KS`, `000660.KS` |

**Achtung bei ausländischen Aktien mit US-Listing (ADRs):** Ein Ticker wie `ASML` (Nasdaq) liefert die Dividende in USD umgerechnet. Bleibt die Dividende in Euro gleich, schwankt der USD-Betrag trotzdem mit dem Wechselkurs, und der Tracker meldet eine scheinbare Erhöhung oder Senkung. Deshalb immer das Heimatlisting verwenden, z. B. `ASML.AS` statt `ASML`, `RACE.MI` statt `RACE` oder `2330.TW` statt `TSM`. Ausnahme: Firmen, die ihre Dividende selbst in USD festlegen (z. B. `AEM`, `PAAS`), bleiben am besten beim US-Ticker.

**Abweichende Ankündigungswährung:** Legt eine Firma ihre Dividende in einer anderen Währung fest als die Börse, an der sie gelistet ist (z. B. Höegh Autoliners: USD, Oslo-Kurs in NOK), lässt sie sich in `watchlist.yaml` unter `declared_currency` eintragen. Der Tracker rechnet die Beträge dann zum Wechselkurs der Woche vor dem Ex-Date zurück (Genauigkeit ca. ±1 %) und meldet erst Änderungen ab 2 %.

```yaml
declared_currency:
  HAUTO.OL: USD
```

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

Beim nächsten Lauf werden die neuen Titel automatisch erkannt und Baselines gesetzt. Ticker, die aus der Watchlist entfernt wurden, löscht der Tracker automatisch aus der Datenbank.

---

## Datenbank

Die SQLite-Datenbank `dividend_history.db` speichert für jeden Ticker das letzte bekannte Ex-Dividend-Date mit Betrag. Sie wird nach jedem Lauf automatisch ins Repository committet und dient als Gedächtnis, ob eine Dividende neu ist. Der Betrag wird außerdem für den Vergleich mit EDGAR-Ankündigungen genutzt.

---

## Abhängigkeiten

```
yfinance    # Dividendenhistorie und Kursdaten
pyyaml      # Watchlist lesen
```

Kein bezahlter API-Key erforderlich. SEC EDGAR ist kostenlos und öffentlich zugänglich.
