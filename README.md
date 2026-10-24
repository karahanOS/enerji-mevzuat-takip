# enerji-mevzuat-takip

[![Testler](https://github.com/karahanOS/enerji-mevzuat-takip/actions/workflows/tests.yml/badge.svg)](https://github.com/karahanOS/enerji-mevzuat-takip/actions/workflows/tests.yml)
![Lisans: MIT](https://img.shields.io/badge/lisans-MIT-blue)

Türkiye elektrik ve enerji piyasası mevzuatını takip eden açık kaynak araç. Resmî Gazete'yi (asıl ve mükerrer
sayılar) ve EPDK, EPİAŞ, ETKB ve TEİAŞ duyurularını tarar. Enerjiyle ilgili yeni düzenlemeleri bulur, değişen
maddeleri ve yürürlük tarihlerini çıkarır, piyasa etkisine göre önem seviyesi verir.

İki şekilde kullanılır:

| | **MCP sunucusu** | **Günlük e-posta** |
|---|---|---|
| Ne alırsınız | Claude, Cursor gibi bir yapay zekâ asistanına "bu hafta hangi enerji mevzuatı çıktı?" diye sorarsınız | Her sabah 08:00'de önem sırasına dizilmiş Türkçe rapor e-postası |
| API anahtarı | **Gerekmez.** Özeti ve yorumu kendi asistanınız yapar | Ücretsiz Gemini/Groq anahtarı (isteğe bağlı) + Gmail uygulama şifresi |
| Kurulum | Tek satır yapılandırma | GitHub'da "Use this template" + 3 secret |
| Nerede çalışır | Bilgisayarınızda, siz sorduğunuzda | GitHub Actions'ta, bilgisayarınız kapalıyken de |

Tüm bileşenler ücretsizdir (bkz. [Maliyet](#maliyet--her-bileşen-ücretsiz)).

> Hukuki görüş veya yatırım tavsiyesi değildir. Bağlayıcı olan, Resmî Gazete'de ve kurum sitelerinde yayımlanan metindir.

---

## Ne yapar

* **Kaynaklar:** Resmî Gazete (HTML ve PDF), EPDK duyuruları, EPİAŞ elektrik piyasası duyuruları, ETKB
  duyuruları, TEİAŞ duyuruları (ETKB RSS adaptörü hazır ama varsayılan olarak kapalı). Ayrıntı: [docs/SOURCES.md](docs/SOURCES.md).
* **Enerji filtresi:** Elektrik piyasası (PTF, SMF, GÖP, GİP, dengeleme, uzlaştırma, tarifeler, kapasite
  mekanizması, depolama), yenilenebilir enerji (YEKDEM, YEKA, lisanssız üretim, YEK-G), doğal gaz, petrol/LPG,
  karbon/ETS, enerji verimliliği, nükleer. Türkçe eklere dayanıklı kök eşleşmesi kullanır.
* **Rutin karar ayrımı:** Resmî Gazete'de her gün çıkan tek şirkete özgü EPDK kararları (lisans tadili, önlisans
  süre uzatımı vb.) ayrı tutulur. Piyasa genelini etkileyen kararlar (fiyat limitleri, tarifeler, usul ve
  esaslar) öne çıkar.
* **Kural tabanlı çıkarım:** değişiklik türü, değiştirilen mevzuat ve ilk yayımı, değişen maddeler, yürürlük ve
  uyum tarihleri. Her tarihin dayanağı yazılır: `kesin` (metinde açık), `hesaplanan` (metindeki süreden),
  `belirsiz`. **Tarih tahmin edilmez.**
* **Önem seviyesi (KRİTİK / YÜKSEK / ORTA / DÜŞÜK):** KRİTİK için iki şart birlikte aranır: piyasa geneline
  etki ve aciliyet (30 gün içinde yürürlük). Gerekçesi her kayıtta yazar.
* **Güvenilirlik:** Taranamayan günler sonraki çalıştırmada telafi edilir; aynı değişiklik iki kez bildirilmez;
  tarama eksikse açıkça söylenir; robots.txt kurallarına uyulur.

---

## 1. MCP sunucusu (anahtarsız)

Gereken tek şey [uv](https://docs.astral.sh/uv/getting-started/installation/). Sunucu, verisini
`~/.enerji-mevzuat-takip/` altındaki bir SQLite dosyasında tutar.

### Claude Desktop

Ayarlar → Geliştirici → Yapılandırmayı düzenle (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "enerji-mevzuat": {
      "command": "uvx",
      "args": ["--from", "enerji-mevzuat-takip[mcp]", "enerji-mevzuat-mcp"]
    }
  }
}
```

### Claude Code

```bash
claude mcp add enerji-mevzuat -- uvx --from "enerji-mevzuat-takip[mcp]" enerji-mevzuat-mcp
```

### Diğer istemciler (Cursor, VS Code, Windsurf…)

Aynı komutu stdio sunucusu olarak ekleyin: komut `uvx`, argümanlar
`--from enerji-mevzuat-takip[mcp] enerji-mevzuat-mcp`.

**Paket henüz PyPI'de değilse** doğrudan GitHub'dan çalıştırın:

```bash
uvx --from "git+https://github.com/karahanOS/enerji-mevzuat-takip" --with "mcp>=2,<3" enerji-mevzuat-mcp
```

İsteğe bağlı ortam değişkenleri (`"env": {...}`): `HTTP_USER_AGENT` (kaynak sitelere kendinizi tanıtmak
için, ör. iletişim adresinizle), `ENERJI_TAKIP_HOME` (veri dizini). `uvx bulunamadı` hatası alırsanız `command` alanına tam yolu yazın (`which uvx` / `where uvx`).

### Araçlar

| Araç | Ne yapar |
|---|---|
| `guncelle(zorla, gecmis_gun)` | Kaynakları tarar, yeni değişiklikleri döndürür. 30 dakikada bir sınırlıdır. İlk kullanımda `gecmis_gun=7` ile son haftayı alın (en fazla 14). |
| `son_degisiklikler(gun, onem, kategori, rutin_dahil, limit)` | Son N günün değişiklikleri, önem sırasıyla |
| `ara(sorgu, gun, limit)` | Başlık ve tam metinde arama ("YEKDEM", "azami uzlaştırma fiyatı") |
| `degisiklik_detayi(degisiklik_id)` | Değiştirilen mevzuat, maddeler, yürürlük tarihleri ve dayanakları, önem gerekçesi |
| `mevzuat_metni(degisiklik_id, baslangic, uzunluk)` | Tam metin, parça parça |
| `kaynak_durumu()` | Her kaynağın erişim durumu ve son hatası |
| `yururluk_tarihlerini_cikar(metin, yayim_tarihi)` | Herhangi bir metinden yürürlük/uyum tarihlerini kural tabanlı çıkarır |

Hazır istemler (prompts): `gunluk_enerji_ozeti`, `mevzuat_analizi(degisiklik_id)`. Bu istemler, asistanın
yalnızca metne dayanmasını, hükümleri alıntıyla vermesini ve tarih tahmin etmemesini ister.

Örnek sorular: *"Enerji mevzuatını güncelle ve bu haftanın önemli değişikliklerini özetle."* ·
*"YEKDEM'le ilgili son 3 ayda ne çıktı?"* · *"Dengeleme ve Uzlaştırma Yönetmeliği değişikliği ne zaman yürürlüğe giriyor?"*

**Beceri (skill):** [`skill/enerji-mevzuat/SKILL.md`](skill/enerji-mevzuat/SKILL.md), araçların hangi sırayla
kullanılacağını ve analiz kurallarını asistana öğretir. Claude'da beceri olarak yükleyebilir veya projenizin
talimatlarına ekleyebilirsiniz.

---

## 2. Günlük e-posta (GitHub şablonu)

Bilgisayarınız kapalıyken de her sabah rapor gelir.

1. Bu depoda **Use this template → Create a new repository** deyin ve deponuzu **Private** seçin. Özel depo
   önerilir: GitHub, 60 gün etkinlik olmayan herkese açık depolarda zamanlanmış işleri durdurur; özel depoda ise
   ayda 2.000 ücretsiz dakika vardır, bu iş ayda ~100–200 dakika kullanır.
2. **Gmail uygulama şifresi alın:** Google Hesabı → Güvenlik → 2 Adımlı Doğrulama'yı açın → "Uygulama
   şifreleri" bölümünden 16 haneli şifre oluşturun.
3. **(İsteğe bağlı) ücretsiz AI anahtarı:** Gemini için <https://aistudio.google.com> → "Get API key" (kredi
   kartı istemez). Yedek olarak Groq: <https://console.groq.com/keys>. Anahtar yoksa rapor AI özeti olmadan,
   kural tabanlı bilgilerle gelir.
4. **Settings → Secrets and variables → Actions → New repository secret:**

   | Secret | Değer |
   |---|---|
   | `SMTP_USERNAME` | Gmail adresiniz |
   | `SMTP_PASSWORD` | 16 haneli uygulama şifresi |
   | `EMAIL_TO` | Raporun gideceği adres(ler), virgülle |
   | `GEMINI_API_KEY`, `GROQ_API_KEY` | (isteğe bağlı) AI anahtarları |
   | `EMAIL_CC`, `ADMIN_EMAIL` | (isteğe bağlı) |

   "Variables" sekmesine `HTTP_USER_AGENT` ekleyin, ör. `enerji-mevzuat-takip/0.1 (iletisim: adresiniz@ornek.com)`.
5. **Actions → "Enerji mevzuatı – günlük tarama" → Run workflow.** İlk denemede "E-posta gönderilsin mi?"
   kutusunu kapatabilirsiniz. Günlükte her kaynağın sonucunu görürsünüz.
6. Bundan sonra iş her gün **08:00'de** (05:00 UTC) çalışır. Veri tabanı `veri` dalında tek commit olarak
   saklanır; depo büyümez.

Secret'lar tanımlı değilse iş ağa çıkmadan "atlandı" notuyla biter. Bu yüzden şablonun kendisinde veya
kurulumu bitmemiş bir kopyada hata e-postası gelmez.

**Kişiselleştirme:** Yerelde `enerji-takip init-config` çalıştırın, oluşan `config/` klasörünü düzenleyip
deponuza ekleyin. `config/profile.yaml`, AI'nın "benim açımdan önemi" bölümünü sizin ilgi alanlarınıza göre
yazmasını sağlar. `config/` varsa varsayılanların yerine o dosyalar kullanılır.

---

## 3. Kendi bilgisayarınızda (CLI)

```bash
pip install "enerji-mevzuat-takip[mcp]"      # veya: pipx install / uv tool install
enerji-takip init-config                     # ~/.enerji-mevzuat-takip/{config/,.env} oluşturur
# .env içine Gmail ve (isteğe bağlı) AI anahtarlarını yazın
enerji-takip check-config
enerji-takip check-llm                       # her ücretsiz sağlayıcıya 1 test isteği
enerji-takip check-sources --all             # kaynaklara erişim ve ayrıştırma
enerji-takip test-email
enerji-takip run --no-send                   # rapor ~/.enerji-mevzuat-takip/data/raporlar/ altına yazılır
enerji-takip scheduler                       # her gün 08:00 (bilgisayar açık kalmalı)
```

Uygulama dizini sırasıyla şöyle belirlenir: `ENERJI_TAKIP_HOME` ortam değişkeni → içinde `config/` veya `.env`
bulunan çalışma dizini → `~/.enerji-mevzuat-takip`.

Sürekli açık bir zamanlayıcı yerine işletim sisteminin zamanlayıcısını kullanabilirsiniz:
Linux/macOS'ta `crontab` ile `0 8 * * * enerji-takip run`, Windows'ta Görev Zamanlayıcı ile her gün 08:00'de
`enerji-takip run`.

| Komut | İşlev |
|---|---|
| `init-config [--force]` | Varsayılan `sources.yaml`, `taxonomy.yaml`, `profile.yaml` ve `.env` dosyalarını uygulama dizinine kopyalar |
| `check-config` | Eksik yapılandırmayı listeler (sırları göstermez) |
| `check-llm` | Zincirdeki her ücretsiz sağlayıcıya küçük bir test isteği atar |
| `check-sources [--all]` | Kaynaklara erişilip ayrıştırılabildiğini gösterir |
| `run [--no-send] [--since YYYY-AA-GG] [--type gunluk\|tarama]` | Tek seferlik tarama ve rapor; `tarama` yalnızca tarar |
| `test-email` | Test e-postası |
| `scheduler` | Yerel sürekli zamanlayıcı (08:00, açılışta telafi, saatlik sağlık kontrolü) |
| `healthcheck` | Son günlük çalıştırma 26 saatten eskiyse çıkış kodu 1 |

**Yerel Ollama ile tamamen çevrimdışı AI:** `ollama pull <model>` ile bir model indirin, `.env` içinde
`LLM_ANALYSIS_CHAIN=ollama:<model>` ve `LLM_TRIAGE_CHAIN=ollama:<model>` yazın.

### Yapılandırma

Tüm değişkenler [.env.example](.env.example) dosyasında açıklamalı olarak durur. Öne çıkanlar:

| Değişken | Açıklama |
|---|---|
| `LLM_ANALYSIS_CHAIN` | Sıralı yedek zincir. Varsayılan: `gemini:gemini-3.8-flash,gemini:gemini-3.5-flash-lite,groq:openai/gpt-oss-120b`. Anahtarı olmayan sağlayıcı atlanır; `none` AI'yı kapatır. |
| `LLM_TRIAGE_CHAIN` | Belirsiz belgelerin ucuz ön elemesi. Varsayılan: `gemini:gemini-3.5-flash-lite,groq:openai/gpt-oss-20b`. |
| `LLM_DAILY_MAX_CALLS` | Günlük AI çağrı tavanı (varsayılan 40). |
| `DAILY_REPORT_TIME` | Yerel zamanlayıcı saati. GitHub Actions'ta saat iş akışındaki `cron` satırından ayarlanır. |
| `SEND_EMPTY_REPORTS` | Değişiklik olmayan günlerde de "değişiklik yok" e-postası gelsin mi (varsayılan evet). |
| `ENERJI_TAKIP_HOME` | Uygulama dizini (veri, rapor, `config/`). |

Model adları sağlayıcıların 23.10.2026 tarihli listelerinden alındı. Sağlayıcılar modelleri zamanla değiştirir;
`check-llm` hata verirse güncel adı zincire yazın.

---

## Maliyet — her bileşen ücretsiz

| Bileşen | Ne için | Maliyet / lisans | Dikkat edilecekler |
|---|---|---|---|
| **MCP sunucusu** | Asistan entegrasyonu | Ücretsiz; API anahtarı yok | Analizi kullandığınız asistan yapar; onun kendi kullanım koşulları geçerlidir. |
| **GitHub Actions** | Günlük çalıştırma | Özel depoda ayda 2.000 dk ücretsiz | Bu iş günde birkaç dakika sürer. |
| **Gemini API** | AI özeti (isteğe bağlı) | Ücretsiz katman, kredi kartı gerekmez | Ücretsiz katmanda Flash ve Flash-Lite modelleri var. Gönderilen içerik Google ürünlerini geliştirmek için kullanılabilir; buradaki içerik zaten kamuya açık mevzuat metni. |
| **Groq API** | Yedek AI (isteğe bağlı) | Ücretsiz plan | Dakikalık token sınırı nedeniyle Groq'a giden metin 12.000 karakterle sınırlanır. |
| **Ollama** | Yerel AI (isteğe bağlı) | Açık kaynak (MIT) | Bilgisayarınız açık olmalı. |
| **Gmail SMTP** | E-posta | Ücretsiz | 2 Adımlı Doğrulama ve uygulama şifresi gerekir. |
| **SQLite + Python kütüphaneleri** | Veri, ayrıştırma | Kamu malı / MIT / BSD / Apache / PSF | Bağımlılıkların yalnızca açık kaynak olduğunu bir test denetler. |

Kodda ücretli bir API çağrısı yoktur (bunu da bir test denetler). Sağlayıcı kotası dolarsa sıradaki ücretsiz
sağlayıcıya geçilir; hepsi doluysa rapor AI olmadan gider ve analiz ertesi gün yeniden denenir.

---

## Bilinen kısıtlamalar

1. **Kaynak siteleri değişebilir.** Ayrıştırıcılar gerçek sayfa yapısını taklit eden test verisiyle test edildi.
   Bir sitenin yapısı değişirse sistem sessizce boş dönmez, "EKSİK TARAMA" uyarısı verir. `check-sources --all`
   ile kontrol edin; sorun görürseniz issue açın.
2. **Resmî Gazete robots.txt:** Sistem her taramada robots.txt iznini kendi kimliğiyle kontrol eder; izin
   yoksa taramaz ve bunu raporlar.
3. **GitHub Actions IP'leri yurt dışındadır.** Bazı kamu siteleri yurt dışı erişimi kısıtlayabilir. Böyle bir
   kaynak Actions'ta sürekli "erişilemedi" görünürse yerelde çalıştırın.
4. **İzlenmeyen kaynaklar:** EPDK kurul kararları listesi ve mevzuat.gov.tr JavaScript gerektiriyor (EPDK
   kararları Resmî Gazete'den izleniyor). EPİAŞ doğal gaz ve çevresel piyasa duyuruları varsayılan olarak
   kapalı; `check-sources --all` ile doğrulayıp açabilirsiniz.
5. **Rutin karar ayrımı ve önem seviyesi kural tabanlıdır.** Rutin listesi her zaman bağlantılarıyla verilir.
6. **Eski–yeni metin karşılaştırması yok.** Değişen maddeler değişiklik metninden çıkarılır.

---

## Geliştirme

```bash
git clone https://github.com/karahanOS/enerji-mevzuat-takip && cd enerji-mevzuat-takip
pip install -e ".[dev]"
python -m pytest tests -q          # ağa çıkmaz; kaynaklar "TEST VERİSİ" etiketli fixture'larla taklit edilir
python -m enerji_takip.mcp_server  # MCP sunucusunu kaynak koddan çalıştırma
```

Mimari: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · Veri tabanı: [docs/DATABASE_SCHEMA.md](docs/DATABASE_SCHEMA.md) ·
Testler: [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md).

**PyPI'ye yayımlama:** pypi.org'da "Trusted Publisher" olarak bu depoyu ve `publish.yml` iş akışını tanımlayın
(ortam adı `pypi`). Sonra GitHub'da bir Release yayımlamanız yeterli; şifre gerekmez.

```
enerji-mevzuat-takip/
├── enerji_takip/
│   ├── mcp_server.py       MCP sunucusu (anahtarsız)
│   ├── varsayilan/         sources.yaml · taxonomy.yaml · profile.yaml · env.example
│   ├── sources/            resmi_gazete.py · listing.py · rss.py · registry.py
│   ├── detection/          relevance.py (enerji filtresi, rutin ayrımı) · extract.py · engine.py
│   ├── analysis/           llm.py (ücretsiz sağlayıcı zinciri) · prompts.py · schema.py · analyzer.py
│   ├── reporting/          builder.py · mailer.py · templates/
│   └── classification.py · pipeline.py · scheduler.py · models.py · db.py · config.py · __main__.py
├── skill/enerji-mevzuat/SKILL.md
├── tests/
├── docs/
└── .github/workflows/      enerji-mevzuat-takip.yml (günlük) · tests.yml · publish.yml
```

---

## English summary

**enerji-mevzuat-takip** tracks Turkish energy and electricity market regulation (Official Gazette, EPDK,
EPİAŞ, ETKB, TEİAŞ). It finds new energy-related legislation, extracts amended articles and effective dates
(never guessed: each date is marked exact, computed or unknown), and ranks items by market impact.

* **MCP server, no API key:** `uvx --from "enerji-mevzuat-takip[mcp]" enerji-mevzuat-mcp`. Your own assistant
  (Claude, Cursor…) reads the texts and writes the summary. Tools: `guncelle` (scan), `son_degisiklikler`
  (recent changes), `ara` (search), `degisiklik_detayi` (details), `mevzuat_metni` (full text), `kaynak_durumu`
  (source status), `yururluk_tarihlerini_cikar` (effective-date extraction).
* **Daily email:** click "Use this template", make the repo private, add `SMTP_USERNAME`, `SMTP_PASSWORD`,
  `EMAIL_TO` (optionally a free `GEMINI_API_KEY`) as secrets. GitHub Actions emails a Turkish report every day
  at 08:00 Istanbul time.
* Everything is free; the code calls no paid APIs. Not legal or investment advice.

## Lisans

[MIT](LICENSE)
