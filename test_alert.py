import os, json, urllib.request

TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

message = """<b>Dividend Tracker — Test 14.04.2026</b>

<b>── Neue Ankuendigungen (EDGAR) ──</b>

📢 <b>PepsiCo (PEP)</b>
↑ Dividendenankuendigung via SEC EDGAR
   Neue Dividende: <b>$1.4850</b> pro Aktie
   Vorherige Dividende: $1.4230
   ➜ Das ist eine <b>Erhoehung von 4.4%</b>
   Eingereicht am: 2026-04-14

<b>── Bestaetigt via Kursdaten ──</b>

↑ <b>MSFT — DividendenErhoehung (+9.6%)</b>
   Neue Dividende: <b>$0.91</b>  (Ex-Date: 2026-02-19)
   Vorher:         $0.83  (Ex-Date: 2025-11-20)

2 Meldung(en) heute."""

payload = json.dumps({
    "chat_id": CHAT_ID,
    "text": message,
    "parse_mode": "HTML",
}).encode()

req = urllib.request.Request(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    data=payload,
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=10) as r:
    result = json.loads(r.read())
    print("OK" if result.get("ok") else result)
