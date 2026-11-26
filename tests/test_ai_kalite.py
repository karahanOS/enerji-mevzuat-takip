"""AI kalite: okunamayan metin AI'a gönderilmez, önceki metne dayanmayan "Eskiden / Şimdi" gösterilmez."""
from __future__ import annotations

from types import SimpleNamespace

from enerji_takip.analysis.analyzer import MIN_TEXT_LETTERS, RegulatoryAnalyzer, _letter_count
from enerji_takip.reporting.builder import _comparison_view

ROW = {"madde": "Karar Metni", "onceki": "Önceki düzenlemede bu hüküm yoktu",
       "yeni": "Taşınmazların acele kamulaştırılması kararlaştırılmıştır.", "pratik_fark": "x", "yapilacak": "y"}


def test_bozuk_pdf_metni_ai_ya_gonderilmez():
    garbage = "\x05\x06\x07\x06\x03\x01" * 20 + "\x19" * 40  # RG 22.10.2026, EPDK karar 14982
    assert _letter_count(garbage) < MIN_TEXT_LETTERS
    analyzer = RegulatoryAnalyzer.__new__(RegulatoryAnalyzer)
    analyzer.chain = object()
    change = SimpleNamespace(document=SimpleNamespace(full_text=garbage), review_reasons=[], status=None,
                             needs_review=False)
    analyzer._add_reason = lambda c, r: c.review_reasons.append(r)
    assert analyzer.analyze(change) is None
    assert change.needs_review and "okunamadı" in change.review_reasons[0]


def test_onceki_metin_yokken_ai_karsilastirmasi_gosterilmez():
    chg = SimpleNamespace(amended_articles=[], change_type="YENI")
    assert _comparison_view(chg, {"karsilastirma": [ROW]})["comparison"] == []


def test_onceki_metin_varsa_ve_konsolidede_gosterilir():
    with_prev = SimpleNamespace(amended_articles=[{"madde": "5", "eski": "eski hüküm", "yeni": "yeni"}],
                                change_type="DEGISIKLIK")
    assert len(_comparison_view(with_prev, {"karsilastirma": [ROW]})["comparison"]) == 1
    cons = SimpleNamespace(amended_articles=[], change_type="KONSOLIDE_GUNCELLEME")
    assert len(_comparison_view(cons, {"karsilastirma": [ROW]})["comparison"]) == 1


def test_metnin_kendisindeki_eski_yeni_ibare_gosterilir():
    """Pakistan protokolü: önceki metin ayrıca alınmadı ama değişiklik metni eski ibareyi alıntılıyor."""
    row = dict(ROW, onceki="Pakistan LNG Terminals Limited", yeni="Pakistan LNG Limited (PLL)")
    chg = SimpleNamespace(amended_articles=[], change_type="DEGISIKLIK")
    assert len(_comparison_view(chg, {"karsilastirma": [row, ROW]})["comparison"]) == 1
