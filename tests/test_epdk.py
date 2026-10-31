"""EPDK Kurul Kararları: sayfa içi AJAX (GetFastAccessList) ve Word belgesi okuma. TEST VERİSİ kullanılır."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date

import httpx
import respx

from enerji_takip.http import FetchResult, Fetcher
from enerji_takip.sources.base import docx_to_text, result_to_text
from enerji_takip.sources.epdk import EpdkKararlarAdapter, parse_leaves

PAGE = "https://www.epdk.gov.tr/Detay/Icerik/3-0-39-3/son-kurul-kararlari/elektrik"
POST = "https://www.epdk.gov.tr/Detay/GetFastAccessList"
HTML = """
<ul class="text-liste"><li class="accordion-pop">
 <a data-toggle="collapse" data-target="#k1" data-type="False">Önlisans ve Lisans</a>
 <div id="k1" class="collapse">
  <a data-toggle="collapse" data-target="#k2" data-id="33883" onclick="ShowDetailList(this);">Başvuru &nbsp;</a>
  <a data-toggle="collapse" data-target="#k3" data-id="33884" onclick="ShowDetailList(this);">Asgari sermaye</a>
  <a data-toggle="collapse" data-target="#k4" data-id="33883" onclick="ShowDetailList(this);">Başvuru (yinelenen)</a>
 </div></li></ul>"""


def _item(number, title, d="08.08.2024", with_files=True, cid="AAA+/="):
    return {
        "Title": title, "Number": number, "Date": d, "DateValue": "/Date(1723064400000)/",
        "RgDate": "14.08.2024", "RgNumber": "32632", "Url": None,
        "FastAccessDetail": ([
            {"ContentId": cid, "IsFileType": 1}, {"ContentId": "PDF/ID+=", "IsFileType": 2},
        ] if with_files else []),
    }


def _fetcher(settings):
    return Fetcher(settings)


def test_yapraklar_cikarilir_ve_tekillesir():
    assert parse_leaves(HTML) == [("33883", "Başvuru"), ("33884", "Asgari sermaye")]


def test_scan_kararlari_toplar_pdf_tercih_edilir_ve_tekillestirir(settings):
    ad = EpdkKararlarAdapter("epdk", {"page_url": PAGE, "post_url": POST, "max_age_days": 100000})
    with respx.mock() as r:
        r.get("https://www.epdk.gov.tr/robots.txt").respond(404)
        r.get(PAGE).respond(200, text=HTML)
        r.post(POST).mock(side_effect=lambda req: httpx.Response(200, json={"Type": 1, "model": (
            [_item("12799-9", "Liste Hk."), _item("12799-8", "Belgesiz", with_files=False)]
            if json.loads(req.content)["fId"] == "33883" else [_item("12799-9", "Liste Hk.")])}))
        out = ad.scan(_fetcher(settings), cursor=None, today=date(2024, 9, 1), state={})
    assert out.complete and out.access_status == "erisilebilir"
    assert len(out.documents) == 1  # aynı numara iki yaprakta da var → tek kayıt
    d = out.documents[0]
    assert d.external_id == "epdk-karar:12799-9"
    assert "12799-9 sayılı Kurul Kararı" in d.title
    assert d.url == "https://www.epdk.gov.tr/Detay/DownloadDocument?id=PDF/ID%2B="  # '+' kodlanır
    assert d.pdf_url.endswith("id=AAA%2B/=")  # Word yedek
    assert d.rg_date == date(2024, 8, 14) and d.rg_number == "32632" and d.published_date == date(2024, 8, 8)
    assert d.doc_type == "KURUL_KARARI" and d.section == "Başvuru"
    assert out.coverage["belgesiz_kayit"] == 1 and out.warnings


def test_eski_kararlar_pencere_disinda_kalir(settings):
    ad = EpdkKararlarAdapter("epdk", {"page_url": PAGE, "post_url": POST, "max_age_days": 30})
    with respx.mock() as r:
        r.get("https://www.epdk.gov.tr/robots.txt").respond(404)
        r.get(PAGE).respond(200, text=HTML)
        r.post(POST).respond(200, json={"model": [_item("1-1", "Eski", d="01.01.2024"), _item("2-1", "Yeni", d="20.08.2024")]})
        out = ad.scan(_fetcher(settings), cursor=None, today=date(2024, 9, 1), state={})
    assert [d.external_id for d in out.documents] == ["epdk-karar:2-1"]


def test_bir_yaprak_hatasi_digerlerini_engellemez_ve_eksik_isaretlenir(settings):
    ad = EpdkKararlarAdapter("epdk", {"page_url": PAGE, "post_url": POST, "max_age_days": 100000})
    with respx.mock() as r:
        r.get("https://www.epdk.gov.tr/robots.txt").respond(404)
        r.get(PAGE).respond(200, text=HTML)
        r.post(POST).mock(side_effect=lambda req: httpx.Response(500) if json.loads(req.content)["fId"] == "33883"
                          else httpx.Response(200, json={"model": [_item("5-1", "Tamam")]}))
        out = ad.scan(_fetcher(settings), cursor="2024-08-30", today=date(2024, 9, 1), state={})
    assert not out.complete and len(out.documents) == 1
    assert out.new_cursor == "2024-08-30" and out.coverage["hatali_yapraklar"] == 1


def test_sayfa_yapisi_degisirse_acik_hata(settings):
    ad = EpdkKararlarAdapter("epdk", {"page_url": PAGE, "post_url": POST})
    with respx.mock() as r:
        r.get("https://www.epdk.gov.tr/robots.txt").respond(404)
        r.get(PAGE).respond(200, text="<html>boş</html>")
        out = ad.scan(_fetcher(settings), cursor=None, today=date(2024, 9, 1), state={})
    assert not out.complete and "kategori" in out.errors[0]


def _docx(paragraphs):
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {ns}><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def test_docx_metni_okunur():
    data = _docx(["Kurul Kararı", "Karar No: 12799", "Ek-1 listesi onaylanmıştır."])
    assert docx_to_text(data) == "Kurul Kararı\nKarar No: 12799\nEk-1 listesi onaylanmıştır."
    res = FetchResult("https://x/DownloadDocument?id=A", 200, data, "", "application/octet-stream")
    assert res.is_docx and not res.is_pdf
    assert "12799" in result_to_text(res)


def test_eski_doc_acik_hatayla_reddedilir():
    res = FetchResult("https://x/d", 200, b"\xd0\xcf\x11\xe0" + b"0" * 50, "", "application/msword")
    try:
        result_to_text(res)
    except ValueError as exc:
        assert ".doc" in str(exc)
    else:
        raise AssertionError("hata bekleniyordu")


def test_isfiletype_bos_kayit_belgesiz_sayilmaz():
    ad = EpdkKararlarAdapter("epdk", {"page_url": PAGE})
    item = {"Title": "T", "Number": "9-1", "Date": "01.10.2026", "FastAccessDetail": [{"ContentId": "Z1=", "IsFileType": None}]}
    docs, no_file = ad.parse_items({"model": [item]}, "x", since=None)
    assert no_file == 0 and docs[0].url.endswith("id=Z1=")
