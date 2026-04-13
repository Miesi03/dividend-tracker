# Dividend Tracker

Taeglich automatisierter Dividenden-Aenderungs-Tracker.  
Laedt Daten via [FMP API](https://financialmodelingprep.com), speichert den Verlauf
in einer lokalen SQLite-Datenbank und sendet bei Dividendenaenderungen eine
Telegram-Nachricht.

## Einrichtung

### 1. FMP API-Key holen
1. Konto anlegen auf [financialmodelingprep.com](https://financialmodelingprep.com)
2. API-Key aus dem Dashboard kopieren (Free: 250 Calls/Tag)

### 2. Telegram-Bot einrichten
1. Telegram oeffnen → **@BotFather** suchen
2. `/newbot` → Namen vergeben → Token kopieren
3. Den Bot einmal anschreiben (`/start`)
4. Chat-ID ermitteln:  
   `https://api.telegram.org/bot<TOKEN>/getUpdates`  
   → `result[0].message.chat.id` ist deine Chat-ID

### 3. GitHub Secrets setzen
Im Repository: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Wert |
|---|---|
| `FMP_KEY` | Dein FMP API-Key |
| `TELEGRAM_TOKEN` | Dein Telegram Bot-Token |
| `TELEGRAM_CHAT_ID` | Deine Telegram Chat-ID |

### 4. Watchlist anpassen
`watchlist.yaml` editieren – einen Ticker pro Zeile.

### 5. Ersten Lauf starten
**Actions → Dividend Tracker – Daily Check → Run workflow**

Beim ersten Lauf werden Baselines gesetzt (kein Alert).  
Ab dem zweiten Lauf werden Aenderungen erkannt und gemeldet.

## Projektstruktur

```
dividend-tracker/
├── tracker.py                    # Hauptskript
├── watchlist.yaml                # Deine Ticker
├── requirements.txt              # Python-Abhaengigkeiten
├── dividend_history.db           # SQLite-DB (automatisch erstellt)
└── .github/
    └── workflows/
        └── daily.yml             # GitHub Actions Zeitplan
```

## Beispiel-Alert (Telegram)

```
Dividenden-Alert – 14.04.2026

↑ PEP  +7.1%
   Neu:  1.0550  (Ex-Date: 2026-03-07)
   Vorher: 0.9850  (Ex-Date: 2025-12-06)

↓ MU  -50.0%
   Neu:  0.0050  (Ex-Date: 2026-04-01)
   Vorher: 0.0100  (Ex-Date: 2026-01-02)

2 Aenderung(en) erkannt.
```

## Hinweise

- **Nicht-US-Ticker** (XETRA, Euronext, SIX): FMP Free liefert hier haeufig
  einen 403-Fehler. Das Skript ueberspringt diese automatisch.
- **NOVO B**: Leerzeichen im Ticker-Symbol kann Probleme verursachen.
  Alternativ `NVO` (US-ADR) verwenden.
- **Erste Ausfuehrung**: Setzt nur Baselines, sendet keine Alerts.
- **Datenbankpersistenz**: Die `dividend_history.db` wird nach jedem Lauf
  automatisch ins Repository committet.
