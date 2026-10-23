# Veri Tabanı Şeması

SQLAlchemy 2.0 modelleri: `enerji_takip/models.py`. Varsayılan veri tabanı tek dosyalık SQLite'tır (GitHub
Actions'ta `veri` dalında saklanır). Tablolar ilk çalıştırmada `init_db()` ile oluşturulur. Zaman damgaları UTC
saklanır, e-postada Europe/Istanbul'a çevrilir. JSON sütunları SQLite'ta metin olarak tutulur.

```
regulatory_sources 1───* regulatory_documents 1───* regulatory_changes 1───* ai_analyses
                                                         │
                                                         *
email_reports 1───* email_report_items *───────────────┘
      │ 1
      *── email_delivery_logs
scan_runs 1───* monitoring_logs   ·   scan_runs 1───* email_reports   ·   system_settings (anahtar/değer)
```

Şartnamedeki tablo adlarıyla eşleşme: RegulatorySources → `regulatory_sources`, RegulatoryDocuments →
`regulatory_documents`, RegulatoryChanges → `regulatory_changes`, AIAnalysis → `ai_analyses`, EmailReports →
`email_reports` (+ `email_report_items`), EmailDeliveryLogs → `email_delivery_logs`, MonitoringLogs →
`monitoring_logs` (+ çalıştırma özeti için `scan_runs`), SystemSettings → `system_settings`.

## regulatory_sources — izlenen kaynaklar

| Sütun | Tür | Boş olabilir | Not |
|---|---|---|---|
| id | INTEGER | hayır | PK |
| key | VARCHAR(64) | hayır | UNIQUE; sources.yaml anahtarı |
| name / url / category | VARCHAR | hayır | Kaynak adı, URL, kategori |
| method | VARCHAR(50) | hayır | Kontrol yöntemi: `rg_fihrist`, `html_listing`, `rss` |
| frequency | VARCHAR(50) | hayır | Kontrol sıklığı (`gunluk`) |
| enabled / is_primary | BOOLEAN | hayır | Birincil kaynak taranamazsa rapor "eksik" |
| access_status | VARCHAR(30) | hayır | Erişilebilirlik: `erisilebilir`, `erisilemiyor`, `robots_engeli`, `erisim_reddedildi`, `javascript_gerekli`, `devre_disi`… |
| last_checked_at / last_success_at | DATETIME | evet | Son kontrol / son başarılı kontrol |
| last_error | TEXT | evet | Son hata |
| cursor | VARCHAR(100) | evet | RG: kesintisiz taranan son gün |
| baseline_done | BOOLEAN | hayır | Liste kaynaklarında ilk tarama tamamlandı mı |
| notes, config | TEXT, JSON | — | Adaptör ayarları; `config._state` çalışma durumu |

## regulatory_documents — görülen her belge

| Sütun | Tür | Not |
|---|---|---|
| id | VARCHAR(36) | PK (UUID) |
| source_id | INTEGER | FK → regulatory_sources |
| dedup_key | VARCHAR(64) | UNIQUE; RG: `sha256(rg|YYYYAAGG-N)`, diğer: `sha256(url|kanonik URL)` |
| external_id | VARCHAR(100) | Belge kimliği (ör. `20261023-3`, `20261023M1-1`) |
| title, doc_type, section, institution | | Mevzuat adı, türü (YONETMELIK, TEBLIG…), fihrist bölümü, kurum |
| rg_date, rg_number, is_mukerrer, published_date | | RG tarihi ve sayısı, mükerrer, yayımlanma tarihi |
| url, pdf_url | VARCHAR(600) | Orijinal resmî kaynak |
| full_text, text_hash, text_status | | Mevzuat metni, SHA-256, `tam` / `kisa` / `okunamadi` / `alinmadi` |
| relevance, relevance_score, matched_categories, matched_keywords, relevance_reason | | `alakali` / `alakasiz` / `atlandi` / `baslangic` / `kopya` ve gerekçesi |
| is_baseline, fetch_error | | |
| first_seen_at, last_checked_at | DATETIME | İlk tespit / son kontrol |

## regulatory_changes — bildirilecek değişiklikler

| Sütun | Tür | Not |
|---|---|---|
| id | VARCHAR(36) | PK (benzersiz değişiklik kimliği) |
| document_id | VARCHAR(36) | FK → regulatory_documents |
| dedup_key | VARCHAR(64) | UNIQUE; `sha256(chg|belge anahtarı|tür)` |
| change_type | VARCHAR(60) | YENI_KANUN, YENI_YONETMELIK, YENI_TEBLIG, YENI_GENELGE, DEGISIKLIK, YURURLUKTEN_KALDIRMA, IDARI_KARAR, KURUL_KARARI, CB_KARARNAMESI, YARGI_KARARI, ULUSLARARASI_ANLASMA, KURUM_DUYURUSU, DIGER |
| change_aspects | JSON | yeni_yukumluluk, yukumluluk_degisikligi, yeni_uygulama_tarihi, yaptirim, gecis_suresi_istisna, yururlukten_kaldirma, madde_degisikligi, madde_eklenmesi… |
| amended_regulation, amended_regulation_ref | | Değiştirilen mevzuat ve ilk yayımlandığı RG |
| amended_articles | JSON | `[{"madde": "Madde 4", "islem": "değiştirildi"}]` |
| effective_dates | JSON | `[{"kind": "yururluk"|"uyum_suresi", "provision", "date", "basis": kesin|hesaplanan|belirsiz, "source_text", "note"}]` |
| effective_date, effective_date_status | | En erken tarih; `kesin`, `hesaplanan`, `farkli_tarihler`, `kismi`, `belirsiz` |
| categories, departments | JSON | Taksonomi kategorileri; profildeki ilgi alanları (`departments` sütun adı geriye uyumluluk için korundu) |
| status | VARCHAR(30) | `yeni`, `analiz_edildi`, `inceleme`, `analiz_basarisiz`, `rutin` (tek şirkete özgü karar, AI'ya gitmez) |
| note | TEXT | Rutin kararın tek cümlelik özü |
| importance, importance_score, importance_reasons, ai_importance | | KRITIK/YUKSEK/ORTA/DUSUK, puan, gerekçeler, AI önerisi |
| needs_review, review_reasons | | İnceleme kuyruğu |
| analysis_attempts, url_verified | | |
| first_detected_at, last_checked_at, updated_at | DATETIME | |

E-posta gönderim durumu, `email_report_items` + `email_reports.status` üzerinden izlenir.

## ai_analyses — her AI denemesi

provider, model, prompt_version, attempt, raw_output (ham yanıt), parsed (doğrulanmış JSON), is_valid,
validation_errors, grounding_issues (doğrulanamayan alıntı/tarih), input_chars, truncated, duration_ms, created_at.

## email_reports / email_report_items / email_delivery_logs

`email_reports`: report_type (`gunluk`/`kritik`), report_date, period_start/end, subject, html_body, text_body,
total/critical/high_count, is_complete (tam tarama mı), status (`olusturuldu`, `gonderildi`, `basarisiz`, `gonderilmedi`), sent_at.
`email_report_items`: rapor ↔ değişiklik (UNIQUE çift).
`email_delivery_logs`: her gönderim denemesi (kind `rapor`/`yonetici`, attempt, recipients, status, error).

## scan_runs / monitoring_logs / system_settings

`scan_runs`: run_type, trigger (`zamanlanmis`, `telafi`, `cli`), status (`calisiyor`, `basarili`, `kismi`,
`basarisiz`, `yarida_kaldi`), sayılar, coverage (kaynak/gün bazında taranan-başarısız), errors. Aynı anda tek
`calisiyor` kaydı = eşzamanlılık kilidi.
`monitoring_logs`: olay günlüğü (kaynak_tarama, tespit, analiz, eposta, belge_hatasi…).
`system_settings`: `llm_calls:YYYY-AA-GG` (günlük AI çağrı sayacı), `last_heartbeat_alert`.
