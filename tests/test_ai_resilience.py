"""AI dayanıklılığı: geçici hatada artan bekleme, 429 bekleme süresi, istek aralığı, devre kesici,
çalıştırma içi gecikmeli yeniden deneme ve AI'sız raporlanan değişikliğin sonradan kurtarılması."""
from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import select

from enerji_takip.analysis import llm as llm_mod
from enerji_takip.analysis.llm import LLMChain, LLMQuotaError, LLMTransientError, Message, OpenAICompatClient
from enerji_takip.db import session_scope
from enerji_takip.models import RegulatoryChange
from tests.conftest import FakeClient, make_pipeline, register_all

BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
URL = f"{BASE}/chat/completions"
OK = httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
DUY = "Elektrik Piyasası Dengeleme ve Uzlaştırma Yönetmeliğinde Değişiklik Yapılmasına Dair Yönetmelik"


@pytest.fixture
def sleeps(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(llm_mod, "_sleep", calls.append)
    monkeypatch.setattr(llm_mod, "_last_request_at", {})
    return calls


def _client(**kw):
    kw.setdefault("min_interval", 0.0)
    return OpenAICompatClient("gemini", "gemini-3.8-flash", BASE, "anahtar", max_input_chars=1000,
                              max_output_tokens=500, timeout=5, **kw)


def _ok(client):
    return client.complete("s", [Message("user", "x")])


# ---- HTTP katmanı --------------------------------------------------------------
def test_503_artan_beklemeyle_yeniden_denenir(sleeps):
    with respx.mock() as router:
        route = router.post(URL).mock(side_effect=[httpx.Response(503), httpx.Response(503), OK])
        assert _ok(_client()) == "{}"
    assert route.call_count == 3
    assert len(sleeps) == 2 and 6 <= sleeps[0] <= 10 and 19 <= sleeps[1] <= 29  # 8 sn, 24 sn (±%20)


def test_zaman_asimi_gecici_hatadir_ve_yeniden_denenir(sleeps):
    with respx.mock() as router:
        router.post(URL).mock(side_effect=[httpx.ReadTimeout("The read operation timed out"), OK])
        assert _ok(_client()) == "{}"
    assert len(sleeps) == 1


def test_surekli_503_gecici_hata_ve_devre_kesici(sleeps):
    client = _client(breaker_threshold=2)
    with respx.mock() as router:
        router.post(URL).mock(return_value=httpx.Response(503))
        for _ in range(2):
            with pytest.raises(LLMTransientError) as exc:
                _ok(client)
    assert "sunucu hatası 503 (3 denemede)" in str(exc.value)
    assert str(exc.value).count("gemini:gemini-3.8-flash") == 1
    assert not client.available
    client.reset_health()
    assert client.available


def test_basari_devre_kesici_sayacini_sifirlar(sleeps):
    client = _client(breaker_threshold=2)
    with respx.mock() as router:
        router.post(URL).mock(side_effect=[httpx.Response(503)] * 3 + [OK])
        with pytest.raises(LLMTransientError):
            _ok(client)
        assert _ok(client) == "{}"
    assert client.consecutive_failures == 0 and client.available


def test_429_govdedeki_retry_delay_kadar_beklenir(sleeps):
    body = {"error": {"code": 429, "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}],
                      "message": "Quota exceeded for metric: generate_content_free_tier_requests, PerMinute"}}
    with respx.mock() as router:
        router.post(URL).mock(side_effect=[httpx.Response(429, json=body), OK])
        assert _ok(_client()) == "{}"
    assert sleeps == [37.0]


def test_429_bekleme_bilgisi_yoksa_kisa_beklemeyle_tekrar(sleeps):
    with respx.mock() as router:
        router.post(URL).mock(side_effect=[httpx.Response(429, text="Resource exhausted"), OK])
        assert _ok(_client()) == "{}"
    assert len(sleeps) == 1 and sleeps[0] <= 10


def test_429_gunluk_kota_beklenmeden_siradakine_birakilir(sleeps):
    body = {"error": {"message": "Quota exceeded", "details": [
        {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}
    with respx.mock() as router:
        route = router.post(URL).mock(return_value=httpx.Response(429, json=body))
        with pytest.raises(LLMQuotaError) as exc:
            _ok(_client())
    assert route.call_count == 1 and not sleeps
    assert "günlük" in str(exc.value)


def test_ayni_saglayiciya_istekler_arasi_asgari_aralik(sleeps, monkeypatch):
    clock = iter([100.0, 101.0, 105.0])
    monkeypatch.setattr(llm_mod, "_monotonic", lambda: next(clock))
    a, b = _client(min_interval=4.0), _client(min_interval=4.0)
    with respx.mock() as router:
        router.post(URL).mock(return_value=OK)
        _ok(a)
        _ok(b)  # aynı sağlayıcı (aynı anahtar) → bekler
    assert sleeps == [3.0]


def test_zincir_devre_kesiciyle_dusen_modeli_atlar():
    class Down(FakeClient):
        available = False

    down, up = Down("gemini", "m1"), FakeClient("groq", "m2", script=["{}"])
    raw, used = LLMChain([down, up]).complete_any("s", lambda c: [Message("user", "x")])
    assert used is up and not down.calls


# ---- Pipeline ------------------------------------------------------------------
def _run(settings, chain, send=False):
    with respx.mock(assert_all_called=False) as router:
        register_all(router)
        return make_pipeline(settings, chain).run(send=send)


def _duy(s):
    return next(c for c in s.scalars(select(RegulatoryChange)) if c.document.title == DUY)


def _fail_duy(times: int):
    """DUY analizini ilk `times` istekte geçici hatayla düşürür, sonra normal yanıt verir."""
    left = [times]

    def responder(messages):
        if DUY in messages[0].content and left[0] > 0:
            left[0] -= 1
            raise LLMTransientError("gemini:m: sunucu hatası 503 (3 denemede)")
        return None

    return responder


def test_gecici_hata_calistirma_icinde_bekleyip_yeniden_denenir(settings):
    client = FakeClient("gemini", "m", script=[_fail_duy(1)] * 50)
    _run(settings, LLMChain([client]))
    with session_scope() as s:
        duy = _duy(s)
        assert duy.status == "analiz_edildi"
        assert not any("AI analizi başarısız" in r for r in duy.review_reasons or [])


def test_hata_mesajinda_model_adi_tekrarlanmaz(settings):
    settings.llm_retry_delay_seconds = -1
    client = FakeClient("gemini", "m", script=[_fail_duy(5)] * 50)
    _run(settings, LLMChain([client]))
    with session_scope() as s:
        reason = next(r for r in _duy(s).review_reasons if r.startswith("AI analizi başarısız"))
    assert reason.count("gemini:m") == 1


def test_aisiz_raporlanan_degisiklik_sonraki_calistirmada_kurtarilir(settings, smtp_server):
    settings.llm_retry_delay_seconds = -1
    broken = FakeClient("gemini", "m", script=[_fail_duy(10)] * 50)
    first = _run(settings, LLMChain([broken]), send=True)
    assert first.report_status == "gonderildi"
    with session_scope() as s:
        assert _duy(s).status == "analiz_basarisiz"

    # Ertesi çalıştırma: sağlayıcı düzelmiş → analiz tamamlanır ve "güncellenen analizler" bölümünde gelir
    second = _run(settings, LLMChain([FakeClient("gemini", "m")]), send=True)
    assert second.report_status == "gonderildi"
    with session_scope() as s:
        duy = _duy(s)
        assert duy.status in ("analiz_edildi", "inceleme")
        assert duy.latest_valid_analysis is not None
    text = smtp_server.messages[-1]["msg"].get_body(("plain",)).get_content()
    html = smtp_server.messages[-1]["msg"].get_body(("html",)).get_content()
    assert "GÜNCELLENEN ANALİZLER (1)" in text and DUY in text and "[GÜNCELLENDİ]" in text
    assert "güncellenen analiz" in html and "Güncellendi" in html

    # Bir kez gösterildikten sonra tekrar gelmez
    sent = len(smtp_server.messages)
    _run(settings, LLMChain([FakeClient("gemini", "m")]), send=True)
    if len(smtp_server.messages) > sent:
        assert "GÜNCELLENEN ANALİZLER" not in smtp_server.messages[-1]["msg"].get_body(("plain",)).get_content()


def test_iki_zaman_asimindan_sonra_beklemeden_vazgecilir(sleeps):
    with respx.mock() as router:
        route = router.post(URL).mock(side_effect=httpx.ReadTimeout("The read operation timed out"))
        with pytest.raises(LLMTransientError):
            _ok(_client(http_retries=4))
    assert route.call_count == 2
