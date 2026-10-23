# Test Sonuçları

**Tarih:** 23.10.2026 · **Ortam:** Python 3.13, SQLite, Linux · **Sonuç:** `93 passed`

```
pip install -e ".[dev]"
python -m pytest tests -v
```

GitHub'da `tests.yml` iş akışı aynı testleri Python 3.11, 3.12 ve 3.13 ile her push ve pull request'te çalıştırır,
ardından paketi derleyip kurar.

## Kapsam

Testler gerçek ağa çıkmaz. Kaynak sitelerin yanıtları, gerçek sayfa yapılarını taklit eden ve açıkça
"TEST VERİSİ" olarak etiketlenmiş fixture'lardan gelir:
- Resmî Gazete fihristi: windows-1254 kodlama, `<span>`'lara bölünmüş başlıklar, mükerrer sayı, HTML ve PDF belgeler.
- EPİAŞ duyuru listesi.
- ETKB RSS akışı (tarih alanı olmadan, gerçek akış gibi).

E-postalar yerel bir SMTP sunucusuna gerçekten gönderilir. AI çağrıları iki şekilde test edilir: iş akışı
testlerinde sahte bir istemci, istemci testlerinde ise sahte Gemini uç noktasına giden gerçek HTTP istekleri.

| Konu | Testler |
|---|---|
| Enerji mevzuatı tespiti ve filtre | `enerji_mevzuati_tespit_analiz_ve_eposta`: yönetmelik, tebliğ, EPDK kararı, PDF, mükerrer sayı bulunur; üniversite, kamulaştırma, karayolu, AYM ve ilanlar elenir. Ayrıca `baslik_ilgililigi` (10 durum), `metin_kontrolu`, `kisa_kokler_tam_kelime_eslesir` |
| Rutin karar ayrımı | Uçtan uca testte 15001 ve 15002 sayılı kararlar AI'ya gitmeden "rutin" listesine girer; `rutin_karar_ayrimi` (5 durum), `rutin_karar_her_zaman_dusuk` |
| Değişiklik ve yürürlük | `degisen_maddeler`, `degisiklik_turu_ve_hedef` (6 durum), `yururluk_tarihi_cikarimi` (9 kalıp), `yururluk_tarihi_tahmin_edilmez`, `uyum_suresi_kesin_ve_hesaplanan`, `farkli_yururluk_tarihlerinde_sure_baslangici_tahmin_edilmez` |
| Tekrar önleme | `ertesi_gun_yalnizca_yeni_kayitlar_ve_kaynaklar_arasi_tekillestirme`: EPİAŞ duyurusu 15000 sayılı karar numarasıyla eşleşip kopya sayılır; yalnızca RSS'teki yeni kayıt raporlanır. Ayrıca `karar_numarasi_eslesmesi`, `eposta_gonderilemezse_degisiklikler_kaybolmaz` |
| Ücretsiz LLM zinciri | `kota_dolunca_siradaki_ucretsiz_saglayiciya_gecer`, `tum_saglayicilarda_kota_dolarsa_kayit_kaybolmaz_ve_sonra_denenir`, `gunluk_tavan_asilmaz`, `saglayiciya_gore_metin_kisaltilir`, `zincir_yalnizca_anahtari_olan_ucretsiz_saglayicilari_kurar` |
| İstemci HTTP davranışı | `istemci_openai_uyumlu_istek_gonderir`, `istemci_json_modu_desteklenmezse_duz_metne_doner`, `istemci_429_kisa_beklemede_tekrar_uzunda_kota_hatasi`, `istemci_yetki_hatasi_kalici_hatadir`, `check_llm_komutu` |
| AI doğruluğu ve güvenlik | `gecersiz_json_ayni_saglayicida_hata_listesiyle_yeniden_istenir`, `uydurma_alinti_ve_tarih_yakalanir`, `dogru_alinti_kabul_edilir`, `istem_enjeksiyonu_etkisizlestirilir`, `sema_normallestirme_ve_alan_eslestirme`, `on_eleme_belirsiz_belgeyi_eler`, `rss_ayristirma_ve_xml_saldirisi` |
| Önem motoru | `piyasa_geneli_yakin_tarihli_fiyat_degisikligi_kritik`, `sektorel_etki_kritik_olmaz`, `uzak_tarihli_genel_degisiklik_acil_degil`, `tek_sirket_etkisi_dusuk_ve_ai_farki_incelemeye` |
| Hata yönetimi | `eksik_gun_telafisi_ve_uyari`, `kaynak_erisilemezse_bos_rapor_gonderilmez`, `robots_engeline_uyulur`, `bir_kaynak_hatasi_digerlerini_engellemez`, `eszamanli_calistirma_engellenir`, `ai_olmadan_kural_tabanli_rapor`, `fihrist_yapi_degisirse_sessizce_bos_donmez`, `veritabani_gecici_hatada_yeniden_dener` |
| E-posta ve zamanlama | `eposta_cc_ve_turkce_karakter`, `eposta_gecici_hatada_yeniden_dener`, `eposta_yapilandirma_hatasi_yeniden_denenmez`, `zamanlayici_0800_istanbul`, `zamanlanmis_gorev_calisir`, `acilista_telafi`, `gunluk_gorev_calismazsa_uyari` |
| MCP sunucusu | `guncelle_anahtarsiz_tarar_ve_ai_cagirmaz` (AI çağrısı ve e-posta yok, 30 dk sınırı, `zorla`), `listeleme_filtreleri_ve_rutin_ayrimi`, `arama_detay_ve_metin_sayfalama` (Türkçe büyük harfle arama, geçersiz kimlikte araç hatası), `kaynak_durumu_ve_tarih_cikarimi` (belirsiz tarih tahmin edilmez), `mcp_protokolu_uzerinden_arac_ve_istem` (MCP istemcisiyle araç listesi, açıklamalar, çağrı, hata, istemler), `stdio_sunucusu_baslar_ve_araclari_listeler` (gerçek alt süreç) |
| Paketleme | `pyproject_bagimliliklari_requirements_ile_ayni`, `lisans_ve_komutlar`, `varsayilan_dosyalar_pakette_ve_env_ornegi_esit`, `init_config_kopyalar_ve_ustune_yazmaz`, `yayimlanacak_dosyalarda_gercek_eposta_yok`, `gizli_dosyalar_git_disinda` |
| Ücretsizlik ve güvenlik | `yalnizca_ucretsiz_bagimliliklar`: requirements.txt yalnızca açık kaynak paketler içerir. `yalnizca_ucretsiz_ai_saglayicilari_ve_web_yok`: kodda Anthropic/OpenAI ücretli uç noktası, FastAPI, uvicorn ve PostgreSQL sürücüsü yok. `kimlik_bilgileri_kodda_yok` |

## GitHub Actions veri kalıcılığı (elle doğrulandı)

İş akışındaki "veriyi yükle" ve "veriyi kaydet" adımları yerel bir git deposunda (bare origin) birebir çalıştırıldı:

```
== 1. çalıştırma        → veri dalı oluşturuldu
== 2. çalıştırma        → veri/enerji_takip.db, raporlar/, loglar/ geri yüklendi; scan_runs: 2
== veri dalı geçmişi    → 1 commit (force push; depo büyümüyor)
```

## Paket kurulumu ve MCP (elle doğrulandı)

```
python -m build                         → wheel + sdist; twine check: PASSED
temiz venv: pip install "…whl[mcp]"     → enerji-takip --help, init-config (~/.enerji-mevzuat-takip/config/*.yaml + .env), check-config
enerji-mevzuat-mcp (stdio, JSON-RPC)    → initialize ✔, tools/list: 7 araç, tools/call guncelle ✔
                                          stdout'ta yalnızca 2 JSON-RPC satırı; günlükler stderr'de
uvx --from <wheel> --with "mcp>=2,<3" enerji-mevzuat-mcp → tools/list: 7 araç
```

Bu ortamda kaynak sitelere erişim olmadığı için `guncelle` "kismi / tam_tarama=false" döndü ve her kaynağın
hatasını `kaynak_durumu` ile raporladı; sunucu çökmedi.

## Bu ortamda yapılamayanlar

| Konu | Neden | Siz nasıl doğrularsınız |
|---|---|---|
| Canlı kaynaklardan veri | Geliştirme ortamının ağı .gov.tr ve epias.com.tr'ye çıkışa izin vermiyor | `check-sources --all`, ardından `run --no-send` (yerelde) veya iş akışını "E-posta gönderilsin mi: hayır" ile elle tetikleyin |
| Gerçek Gemini/Groq çağrısı | API anahtarı yok; uydurulmadı. İstek biçimi sahte uç noktayla test edildi | `check-llm` |
| GitHub Actions'ın kendisi | GitHub'da çalıştırılmadı; adımların kabuk mantığı yerelde denendi | Actions → Run workflow |
| PyPI yayımı ve `uvx --from enerji-mevzuat-takip[mcp]` | Paket henüz PyPI'de değil; aynı komut yerel wheel ile denendi | İlk Release'ten sonra README'deki komut |
