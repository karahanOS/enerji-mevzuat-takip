"""E-posta boyutu: Gmail 102 KB'ı aşan HTML'i keser; sınır aşılırsa e-postadaki listeler kısaltılır."""
from __future__ import annotations

import respx

from tests.conftest import fake_chain, make_pipeline, register_all


def _run(settings):
    with respx.mock(assert_all_called=False) as router:
        register_all(router)
        return make_pipeline(settings, fake_chain()).run(send=True)


def _bodies(smtp_server):
    msg = smtp_server.messages[-1]["msg"]
    return msg.get_body(("html",)).get_content(), msg.get_body(("plain",)).get_content()


def test_sinir_altinda_rapor_kisaltilmaz(settings, smtp_server):
    settings.report_max_html_bytes = 90_000
    _run(settings)
    html, text = _bodies(smtp_server)
    assert "kısaltıldı" not in html and "kısaltıldı" not in text


def test_sinir_asilirsa_listeler_kisaltilir_tam_hali_kopyada_kalir(settings, smtp_server):
    settings.report_max_html_bytes = 1  # tüm kısaltma adımlarını zorla
    result = _run(settings)
    assert result.report_status == "gonderildi"
    html, text = _bodies(smtp_server)
    assert "E-posta boyut sınırını aşmaması için" in html and "E-posta boyut sınırını aşmaması için" in text
    assert "Liste e-postada gösterilmedi" in html
    assert "Tarife" in html or "Dengeleme" in html  # kayıtların kendisi atılmaz
    preview = sorted(settings.report_preview_dir.glob("*.html"))[-1].read_text(encoding="utf-8")
    assert "kısaltıldı" not in preview and len(preview.encode()) > len(html.encode())


def test_etiket_degiskeni_metne_islenir(settings, smtp_server):
    _run(settings)
    html, text = _bodies(smtp_server)
    assert "{{" not in html and "{{" not in text
