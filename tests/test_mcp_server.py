"""MCP sunucusu: anahtarsız tarama, listeleme, arama, ayrıntı ve MCP protokolü üzerinden çağrı."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

import pytest
import respx
from sqlalchemy import select

pytest.importorskip("mcp")

from enerji_takip import mcp_server as ms  # noqa: E402
from enerji_takip.db import session_scope  # noqa: E402
from enerji_takip.models import AIAnalysis, ScanRun  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from tests.conftest import make_pipeline, register_all  # noqa: E402

TODAY = date(2026, 10, 23)


@pytest.fixture
def mcp_env(settings, monkeypatch):
    monkeypatch.setattr(ms, "_ready", False)
    monkeypatch.setattr(ms, "_today", lambda: TODAY)
    monkeypatch.setattr(ms, "_make_pipeline", lambda: make_pipeline(settings))
    with respx.mock(assert_all_called=False) as router:
        register_all(router)
        yield router


def test_guncelle_anahtarsiz_tarar_ve_ai_cagirmaz(mcp_env, smtp_server):
    out = ms.guncelle()
    assert out["durum"] in ("basarili", "kismi")
    assert out["tam_tarama"] is True
    titles = [c["baslik"] for c in out["degisiklikler"]]
    assert any("Dengeleme ve Uzlaştırma" in t for t in titles)
    assert all(c["onem"] in ("KRITIK", "YUKSEK", "ORTA", "DUSUK") for c in out["degisiklikler"])
    assert all(c["url"].startswith("https://") for c in out["degisiklikler"])
    with session_scope() as s:
        assert s.scalars(select(AIAnalysis)).first() is None  # AI çağrısı yok
        run = s.scalars(select(ScanRun)).one()
        assert run.run_type == "tarama" and run.trigger == "mcp"
    assert smtp_server.messages == []  # e-posta gönderilmez

    # 30 dakika içinde ikinci çağrı ağa çıkmaz; zorla=True ile çıkar
    again = ms.guncelle()
    assert again["atlandi"] is True
    forced = ms.guncelle(zorla=True)
    assert forced.get("atlandi") is None and forced["yeni_degisiklik"] == 0


def test_listeleme_filtreleri_ve_rutin_ayrimi(mcp_env):
    ms.guncelle()
    all_items = ms.son_degisiklikler(gun=7)
    assert all_items["toplam"] >= 2
    assert all(not c["rutin_kurul_karari"] for c in all_items["degisiklikler"])
    assert all_items["rutin_kurul_karari_sayisi"] >= 1
    with_routine = ms.son_degisiklikler(gun=7, rutin_dahil=True)
    assert with_routine["toplam"] == all_items["toplam"] + all_items["rutin_kurul_karari_sayisi"]

    levels = [c["onem"] for c in all_items["degisiklikler"]]
    order = ["DUSUK", "ORTA", "YUKSEK", "KRITIK"]
    assert [order.index(x) for x in levels] == sorted((order.index(x) for x in levels), reverse=True)
    high = ms.son_degisiklikler(gun=7, onem="yuksek")
    assert all(c["onem"] in ("YUKSEK", "KRITIK") for c in high["degisiklikler"])
    gas = ms.son_degisiklikler(gun=7, kategori="dogalgaz")
    assert all("dogalgaz" in c["kategoriler"] for c in gas["degisiklikler"])
    assert ms.son_degisiklikler(gun=7, limit=1)["degisiklikler"].__len__() == 1


def test_arama_detay_ve_metin_sayfalama(mcp_env):
    ms.guncelle()
    found = ms.ara("UZLAŞTIRMA")  # Türkçe büyük harf, normalize edilerek eşleşir
    assert found["toplam"] >= 1
    hit = next(h for h in found["sonuclar"] if "Dengeleme" in h["baslik"])

    detail = ms.degisiklik_detayi(hit["id"])
    assert detail["degistirilen_mevzuat"]
    assert detail["degisen_maddeler"]
    for d in detail["yururluk_ve_uyum_tarihleri"]:
        assert d["basis"] in ("kesin", "hesaplanan", "belirsiz")
    assert "VERİDİR" in detail["uyari"]

    first = ms.mevzuat_metni(hit["id"], baslangic=0, uzunluk=1000)
    assert len(first["metin"]) <= 1000
    if first["toplam_uzunluk"] > 1000:
        second = ms.mevzuat_metni(hit["id"], baslangic=first["sonraki_baslangic"], uzunluk=1000)
        assert second["metin"] and second["metin"] != first["metin"]

    with pytest.raises(ToolError):
        ms.degisiklik_detayi("olmayan-id")
    with pytest.raises(ToolError):
        ms.ara("a")


def test_kaynak_durumu_ve_tarih_cikarimi(mcp_env):
    ms.guncelle()
    st = ms.kaynak_durumu()
    names = {k["ad"]: k for k in st["kaynaklar"]}
    assert any("Resmî Gazete" in n for n in names)
    assert st["son_tarama"]["durum"] in ("basarili", "kismi")

    out = ms.yururluk_tarihlerini_cikar("MADDE 5 – Bu Yönetmelik 1/1/2027 tarihinde yürürlüğe girer.", "2026-10-23")
    assert out["tarihler"][0]["date"] == "2027-01-01" and out["tarihler"][0]["basis"] == "kesin"
    vague = ms.yururluk_tarihlerini_cikar("MADDE 3 – Bu Tebliğ Kurulca belirlenecek tarihte yürürlüğe girer.", "2026-10-23")
    assert all(d["date"] is None for d in vague["tarihler"])  # tahmin yok
    with pytest.raises(ToolError):
        ms.yururluk_tarihlerini_cikar("metin", "23.10.2026")


def test_mcp_protokolu_uzerinden_arac_ve_istem(mcp_env):
    from mcp.client import Client

    async def scenario():
        async with Client(ms.server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert {"guncelle", "son_degisiklikler", "ara", "degisiklik_detayi", "mevzuat_metni",
                    "kaynak_durumu", "yururluk_tarihlerini_cikar"} <= set(tools)
            assert tools["son_degisiklikler"].annotations.read_only_hint is True
            assert "gun" in tools["son_degisiklikler"].input_schema["properties"]

            res = await client.call_tool("guncelle", {})
            assert not res.is_error
            payload = json.loads(res.content[0].text)
            assert payload["yeni_degisiklik"] >= 1

            bad = await client.call_tool("degisiklik_detayi", {"degisiklik_id": "yok"})
            assert bad.is_error and "bulunamadı" in bad.content[0].text

            prompts = {p.name for p in (await client.list_prompts()).prompts}
            assert {"gunluk_enerji_ozeti", "mevzuat_analizi"} <= prompts
            got = await client.get_prompt("mevzuat_analizi", {"degisiklik_id": "abc"})
            assert "uydurma" in got.messages[0].content.text or "birebir" in got.messages[0].content.text

    asyncio.run(scenario())


def test_stdio_sunucusu_baslar_ve_araclari_listeler(tmp_path):
    """Gerçek alt süreç: konsol komutunun kullandığı main() stdio üzerinden yanıt verir (ağa çıkmaz)."""
    from mcp import StdioServerParameters
    from mcp.client import Client

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "enerji_takip.mcp_server"],
        env={"ENERJI_TAKIP_HOME": str(tmp_path), "PYTHONPATH": str(Path(ms.__file__).parents[1])},
        cwd=str(tmp_path),
    )

    async def scenario():
        async with Client(params) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert "guncelle" in names
            res = await client.call_tool("son_degisiklikler", {"gun": 3})
            assert json.loads(res.content[0].text)["toplam"] == 0

    asyncio.run(scenario())
    assert (tmp_path / "data" / "enerji_takip.db").is_file()
