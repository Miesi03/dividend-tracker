"""Offline-Tests für die Bewertungslogik (ohne Netzwerkzugriff).
Die Szenarien sind echten Dividendenverläufen nachgebildet.
Ausführen: python -m pytest
"""

import sqlite3
from datetime import date

import pytest

import tracker as T


def desc(history):
    return sorted(history, reverse=True)


def evaluate(history, **kw):
    h = desc(history)
    return T.evaluate_change(h, specials=T.detect_specials(h), **kw)


# ── Vergleichslogik ───────────────────────────────────────────────────────────

ASML = [("2025-04-28", 1.84), ("2025-07-29", 1.60), ("2025-10-28", 1.60),
        ("2026-02-10", 1.60), ("2026-04-24", 2.70), ("2026-07-27", 1.88),
        ("2026-10-26", 1.88)]

@pytest.mark.parametrize("name, history, expected", [
    ("ASML Interim nach Schlussdividende", ASML[:6], 17.5),
    ("ASML unverändert", ASML, None),
    ("ASML Schlussdividende", ASML[:5], 46.7),
    ("APH nach Split, splitbereinigt",
     [("2025-06-17", .0825), ("2025-09-16", .0825), ("2025-12-16", .125),
      ("2026-03-23", .125), ("2026-06-23", .125)], None),
    ("VRT Rundung", [("2026-06-15", .063), ("2026-09-14", .063)], None),
    ("LVMH Interim nach Schluss",
     [("2024-04-23", 7.5), ("2024-12-02", 5.5), ("2025-04-24", 7.5), ("2025-12-02", 5.5)], None),
    ("MSFT Erhöhung",
     [("2025-08-21", .83), ("2025-11-20", .91), ("2026-02-19", .91), ("2026-05-21", .91),
      ("2026-08-20", .91), ("2026-11-19", .98)], 7.7),
    ("MSFT Quartal nach Erhöhung", [("2025-08-21", .83), ("2025-11-20", .91), ("2026-02-19", .91)], None),
    ("Echte Kürzung",
     [("2025-03-01", 1.0), ("2025-06-01", 1.0), ("2025-09-01", 1.0), ("2025-12-01", 1.0),
      ("2026-03-01", .5)], -50.0),
    ("Ferrari jährlich", [("2025-04-22", 2.986), ("2026-04-20", 3.615)], 21.1),
    ("Novo Interim", [("2025-03-28", 7.95), ("2025-08-15", 3.75), ("2026-03-27", 7.95),
                      ("2026-08-14", 3.75)], None),
])
def test_evaluate_change(name, history, expected):
    result = evaluate(history)
    if expected is None:
        assert result is None, name
    else:
        assert result is not None, name
        assert result["pct"] == pytest.approx(expected, abs=0.1), name


def test_fx_threshold_ignores_small_conversion_noise():
    h = [("2026-03-02", 0.5190), ("2026-05-18", 0.5240)]   # +1 %, Rückrechnungsrauschen
    assert evaluate(h, differ_pct=T.FX_MIN_CHANGE_PCT) is None


def test_ttm_sums_counts_payments_not_days():
    # CTAS: Ex-Dates verschieben sich leicht, Vorjahreszahlung liegt 364 Tage zurück
    h = desc([("2024-08-15", .39), ("2024-11-15", .39), ("2025-02-14", .39), ("2025-05-15", .39),
              ("2025-08-15", .45), ("2025-11-14", .45), ("2026-02-13", .45), ("2026-05-15", .45),
              ("2026-08-14", .52)])
    cur, prev = T.ttm_sums(h, "Quartalsweise")
    assert cur == pytest.approx(.52 + 3 * .45)
    assert prev == pytest.approx(.45 + 3 * .39)


# ── Sonderdividenden ──────────────────────────────────────────────────────────

COST = [("2023-02-09", .90), ("2023-04-27", 1.02), ("2023-07-27", 1.02), ("2023-10-26", 1.02),
        ("2023-12-27", 15.0), ("2024-02-01", 1.02), ("2024-04-25", 1.16), ("2024-07-26", 1.16),
        ("2024-11-01", 1.16), ("2025-02-07", 1.16)]

def test_costco_special_detected():
    assert T.detect_specials(desc(COST)) == {"2023-12-27"}

def test_special_is_reported_as_special():
    result = evaluate(COST[:5])
    assert result["special"] is True
    assert result["prev"] == ("2023-10-26", 1.02)

def test_payment_after_special_compares_with_regular():
    # Feb 2024 (1.02) direkt nach 15 $ Sonderdividende → keine "Kürzung um 93 %"
    assert evaluate(COST[:6]) is None

def test_new_regular_level_is_not_special():
    # NVIDIA 2026: 0.01 → 0.25, danach wieder 0.25 → neues Niveau, keine Sonderdividende
    nvda = [("2025-06-11", .01), ("2025-09-11", .01), ("2025-12-04", .01), ("2026-03-11", .01),
            ("2026-06-04", .25), ("2026-09-10", .25)]
    assert T.detect_specials(desc(nvda)) == set()

def test_seasonal_final_dividend_is_not_special():
    sk = [("2025-02-26", 1500.0), ("2025-05-28", 375.0), ("2025-08-28", 375.0),
          ("2025-11-27", 375.0), ("2026-02-26", 1875.0)]
    assert T.detect_specials(desc(sk)) == set()

def test_special_text_is_mandatory_in_message():
    result = evaluate(COST[:5])
    msg = T.msg_yfinance("COST", "COSTCO", result, "$")
    assert "Sonderdividende" in msg
    msg = T.msg_yfinance("COST", "COSTCO", dict(result, special=False), "$", special_explicit=True)
    assert "Sonderdividende</b> (laut Ankündigung)" in msg


# ── Überfällige Dividenden ────────────────────────────────────────────────────

def test_overdue():
    h = desc([("2025-06-01", 1.0), ("2025-09-01", 1.0), ("2025-12-01", 1.0), ("2026-03-01", 1.0)])
    assert T.overdue_days(h, today=date(2026, 5, 1)) is None
    assert T.overdue_days(h, today=date(2026, 9, 1)) == 184 - 91

def test_old_payer_is_not_overdue():
    h = desc([("1990-01-01", 1.0), ("1990-04-01", 1.0), ("1990-07-01", 1.0)])
    assert T.overdue_days(h, today=date(2026, 1, 1)) is None


# ── EDGAR-Texterkennung ───────────────────────────────────────────────────────

def test_strip_html_unescapes_entities():
    assert T.strip_html("<p>dividend of $&#160;0.98&nbsp;per share</p>") == "dividend of $ 0.98 per share"

def test_extract_regular_dividend():
    text = T.dividend_sentences(T.strip_html(
        "Microsoft today announced that its board of directors declared a quarterly dividend "
        "of $&#160;0.98 per share, reflecting an 8 percent increase over the previous quarter's dividend."))
    assert T.extract_amount(text) == 0.98
    assert T.extract_change(text) == 8.0
    assert not T.is_special_text(text)

def test_extract_special_dividend():
    text = T.dividend_sentences(
        "The Board declared a special cash dividend of $15.00 per share, payable January 12.")
    assert T.extract_amount(text) == 15.0
    assert T.is_special_text(text)

def test_revenue_growth_is_not_a_dividend_change():
    # Quartalsbericht: Umsatzplus darf nicht als Dividendenerhöhung gelesen werden
    raw = ("Revenue increased 24% to $17.6 billion. Diluted EPS of $5.20 per share. "
           "Cash dividends declared per common share $ 1.51.")
    text = T.dividend_sentences(raw)
    assert T.extract_change(text) is None
    assert T.extract_amount(text) is None

def test_ex_date_is_iso():
    assert T.extract_ex_date_edgar("The ex-dividend date will be November 19, 2026.") == "2026-11-19"


# ── Telegram-Nachrichten ──────────────────────────────────────────────────────

def test_company_names_are_escaped():
    result = evaluate([("2025-03-01", 1.0), ("2026-03-01", 1.2)])
    msg = T.msg_yfinance("SPGI", "S&P Global <Inc>", result, "$")
    assert "S&amp;P Global &lt;Inc&gt;" in msg
    assert "S&P" not in msg

def test_split_message_respects_limit_and_keeps_blocks_whole():
    blocks = ["<b>Kopf</b>"] + [f"Meldung {i}\n" + "x" * 900 for i in range(10)]
    chunks = T.split_message(blocks, limit=4000)
    assert len(chunks) > 1
    assert all(len(c) <= 4000 + 30 for c in chunks)   # + "(Teil i/n)"
    joined = "".join(chunks)
    assert all(f"Meldung {i}\n" + "x" * 900 in joined for i in range(10))

def test_build_messages_single_when_short():
    assert len(T.build_messages(["a"], ["b"])) == 1


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, text", [(1.6, "1.60"), (0.0625, "0.0625"), (374.0, "374.00"), (3.615, "3.615")])
def test_fmt(value, text):
    assert T.fmt(value) == text

def test_amounts_differ_rounding():
    assert not T.amounts_differ(0.0625, 0.063)
    assert T.amounts_differ(0.272, 0.271)

def test_clean_company_name():
    assert T.clean_company_name("AMPHENOL CORP /DE/") == "AMPHENOL CORP"
    assert T.clean_company_name("DEERE & CO") == "DEERE & CO"

@pytest.mark.parametrize("ticker, symbol", [("ASML.AS", "€"), ("2330.TW", "NT$"), ("005930.KS", "₩"), ("MSFT", "$")])
def test_currency_symbol(ticker, symbol):
    assert T.get_currency_symbol(ticker) == symbol


# ── Datenbank ─────────────────────────────────────────────────────────────────

def test_announcement_dedupe(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "DB_PATH", tmp_path / "test.db")
    con = T.init_db()
    T.save_announcement(con, "MSFT", "2026-11-19", 0.98, False, "nasdaq")
    assert T.find_announcement(con, "MSFT", ex_date="2026-11-20")        # ±3 Tage
    assert T.find_announcement(con, "MSFT", amount=0.98)
    assert not T.find_announcement(con, "MSFT", ex_date="2027-02-18")

def test_edgar_announcement_without_ex_date_matches_by_amount(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "DB_PATH", tmp_path / "test.db")
    con = T.init_db()
    T.save_announcement(con, "VRT", None, 0.0625, False, "edgar")
    assert T.find_announcement(con, "VRT", amount=0.063, ex_date="2026-12-01")   # Rundung
    assert not T.find_announcement(con, "VRT", amount=0.08, ex_date="2026-12-01")

def test_prune_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "DB_PATH", tmp_path / "test.db")
    con = T.init_db()
    T.upsert(con, "ASML", "2026-10-27", 2.108)
    T.upsert(con, "ASML.AS", "2026-10-26", 1.88)
    T.prune_removed(con, ["ASML.AS"])
    assert [r[0] for r in con.execute("SELECT ticker FROM dividends")] == ["ASML.AS"]
