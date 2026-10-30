[README.md](https://github.com/user-attachments/files/26780049/README.md)
# Dividend Tracker

Automatisierter Dividenden-Tracker mit täglichem Monitoring via GitHub Actions.  
Erkennt Dividendenankündigungen und -änderungen und sendet sofort eine Telegram-Benachrichtigung.

---

## Wie es funktioniert

Der Tracker läuft Mo–Fr um 08:00 Uhr MEZ vollautomatisch:

**Phase 1 — SEC EDGAR (US-Aktien)**  
Scannt neue 8-K-Meldungen der letzten 7 Tage inklusive Anhang EX-99 (dort steht meist die Pressemitteilung). Ausgewertet werden nur Sätze mit „dividend“, damit z. B. ein Umsatzplus aus einem Quartalsbericht nicht als Dividendenerhöhung gelesen wird. Viele Firmen (z. B. Microsoft, Coca-Cola, PepsiCo) reichen Dividendenbeschlüsse aber gar nicht als 8-K ein, deshalb gibt es zusätzlich Nasdaq.

**Phase 2 — pro Ticker**
- **yfinance (alle Börsen):** lädt die splitbereinigte Dividendenhistorie.
- **Nasdaq (US-Aktien):** liefert angekündigte, noch nicht gezahlte Dividenden mit Betrag, Ankündigungsdatum, Ex-Date und Zahltag. So wird z. B. eine Erhöhung schon kurz nach der Ankündigung gemeldet und nicht erst zum Ex-Date. Für europäische Aktien gibt es keine vergleichbare freie Quelle.
- **Neues Ex-Date:** Taucht eine neue Zahlung in der yfinance-Historie auf, wird sie bewertet. War sie bereits angekündigt und gemeldet, kommt keine zweite Meldung.

**So wird verglichen:**
- Entspricht die neue Zahlung der vorherigen, gibt es keine Meldung. Rundungsabweichungen bis 0,0005 werden ignoriert, weil yfinance auf 3 Nachkommastellen rundet (z. B. 0,0625 $ → 0,063 $).
- Weicht sie ab, wird mit der Zahlung aus dem **Vorjahreszeitraum** verglichen. So werden schwankende Interims- und Schlussdividenden (z. B. ASML, LVMH, Novo Nordisk) nicht als Kürzung oder Erhöhung gemeldet, eine echte Erhöhung aber schon.
- Gibt es keine Vorjahreszahlung (z. B. bei jährlicher Zahlung), wird mit der vorherigen Zahlung verglichen.
- Verglichen wird nur innerhalb der yfinance-Historie, die nach Aktiensplits rückwirkend bereinigt ist. Ein Split (z. B. Amphenol 2:1) löst daher keine Schein-Kürzung aus.
- Zusätzlich zeigt die Meldung die Summe der letzten 12 Monate im Vergleich zum Vorjahr.

**Sonderdividenden:**  
Weder yfinance noch Nasdaq kennzeichnen Sonderdividenden. Der Tracker erkennt sie deshalb auf zwei Wegen und schreibt das immer in die Meldung:
- 💰 **Sonderdividende (laut Ankündigung):** Die SEC-Meldung spricht ausdrücklich von „special dividend“.
- ⚠️ **Vermutlich Sonderdividende oder starke Erhöhung:** Der Betrag ist mindestens 2,5-mal so hoch wie die üblichen Zahlungen, und im Umkreis von gut einem Jahr gab es keine vergleichbar hohe Zahlung. Bei einer einzelnen, sehr starken Erhöhung (z. B. NVIDIA 2026: 0,01 $ → 0,25 $) lässt sich das anfangs nicht unterscheiden.

Erkannte Sonderdividenden fließen nicht in spätere Vergleiche ein. Die nächste reguläre Zahlung erscheint also nicht als „Kürzung“.

**Überwachung:**
- Probleme (yfinance-Fehler, falsche Ticker, SEC oder Nasdaq nicht erreichbar) stehen in der Telegram-Nachricht.
- Schlägt Telegram fehl oder fallen mindestens 25 % der Ticker aus, endet der Lauf mit Fehler, und GitHub verschickt eine E-Mail.
- Montags kommt ein Wochenstatus, auch ohne Meldungen. Er nennt überfällige Dividenden, die auf eine Aussetzung hindeuten können.
- Lange Nachrichten werden automatisch auf mehrere Telegram-Nachrichten verteilt (Limit 4.096 Zeichen).

---

## Beispiel-Alert

```
Dividend Tracker — 29.10.2026

── Neue Ankündigungen ──

📢 MICROSOFT CORP (MSFT)
↑ Dividendenankündigung via Nasdaq
   Neue Dividende: $0.98 pro Aktie
   Vorjahreszeitraum: $0.91  (Ex-Date: 20-11-2025)
   Vorherige Zahlung: $0.91
   ➜ Das ist eine Erhöhung von 7.7% ggü. Vorjahr
   Angekündigt am:  15-09-2026
   Ex-Dividende:    19-11-2026
   Zahltag:         10-12-2026
   Zahlungsweise:   Quartalsweise

── Neue Ex-Dates (Kursdaten) ──

📢 ASML HOLDING (ASML.AS)
↑ Neue Dividende laut Kursdaten (yfinance)
   Neue Dividende: €1.88 pro Aktie  (Ex-Date: 27-07-2026)
   Vorjahreszeitraum: €1.60  (Ex-Date: 29-07-2025)
   ➜ Das ist eine Erhöhung von 17.5% ggü. Vorjahr
   Vorherige Zahlung: €2.70  (Ex-Date: 24-04-2026, -30.4%)
   Zahlungsweise:   Quartalsweise

2 Meldung(en) heute.
```

---

## Projektstruktur

```
dividend-tracker/
├── tracker.py              # Hauptskript (EDGAR, Nasdaq, yfinance)
├── watchlist.yaml          # Watchlist mit allen Tickern
├── requirements.txt        # Python-Abhängigkeiten (feste Versionen)
├── requirements-dev.txt    # zusätzlich pytest
├── dividend_history.db     # SQLite-Datenbank (automatisch erstellt)
├── tests/
│   └── test_tracker.py     # Offline-Tests der Bewertungslogik
└── .github/
    ├── dependabot.yml      # monatliche Update-Vorschläge
    └── workflows/
        ├── daily.yml       # GitHub Actions Zeitplan
        └── tests.yml       # Tests bei jedem Pull Request
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
| `SEC_CONTACT_EMAIL` | Optional, aber empfohlen: deine E-Mail-Adresse. Die SEC verlangt eine echte Kontaktadresse im User-Agent und kann generische Absender sperren. |

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

Die SQLite-Datenbank `dividend_history.db` speichert für jeden Ticker das letzte bekannte Ex-Dividend-Date mit Betrag. Sie wird nach jedem Lauf automatisch ins Repository committet und dient als Gedächtnis, ob eine Dividende neu ist. Der Betrag wird außerdem für den Vergleich mit EDGAR-Ankündigungen genutzt. Außerdem merkt sie sich bereits gemeldete Ankündigungen (gegen Doppelmeldungen) und die Firmennamen der Nicht-US-Werte.

---

## Abhängigkeiten

```
yfinance    # Dividendenhistorie und Kursdaten
curl_cffi   # Browser-Imitation gegen Yahoo-Rate-Limiting, auch für Nasdaq
pandas      # wird von yfinance benötigt
pyyaml      # Watchlist lesen
```

Die Versionen in `requirements.txt` sind fest vorgegeben, weil yfinance seine Schnittstelle häufig ändert. Dependabot schlägt Updates monatlich als Pull Request vor, die Tests prüfen sie vor dem Merge.

Kein bezahlter API-Key erforderlich. SEC EDGAR und Nasdaq sind kostenlos und öffentlich zugänglich.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Die Tests laufen ohne Netzwerkzugriff und bilden echte Fälle nach (z. B. ASML, Amphenol-Split, Vertiv-Rundung, Costco-Sonderdividende).
