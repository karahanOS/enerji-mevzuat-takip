# Kaynaklar ve Entegrasyonlar

Kontrol tarihi: **23.10.2026**. Adresler ve bağlantı kalıpları, sitelerin o tarihteki sayfalarından bir web okuma
aracıyla doğrulandı. Sizin bağlantınızdan (veya GitHub Actions'tan) sonuç farklı olabilir; kurulumdan sonra
`enerji-takip check-sources --all` çalıştırın (MCP sunucusunda: `kaynak_durumu` aracı).

## İzlenen kaynaklar

| Kaynak | Adres | Yöntem | Durum |
|---|---|---|---|
| **Resmî Gazete** (birincil) | `eskiler/YYYY/AA/YYYYAAGG.htm` (+ `…M1.htm`), belgeler `…-N.htm/pdf` | HTML fihrist + HTML/PDF metin (RSS/API yok) | **Etkin.** EPDK kurul kararları, enerji yönetmelik ve tebliğleri burada yayımlanır. Çalışma anında robots.txt kontrol edilir. |
| EPDK – Duyurular | `epdk.gov.tr/Detay/Icerik/4-0-1/duyurular` → `/Detay/Icerik/4-NNNNN/…` | HTML liste | **Etkin.** İstatistik ve sektör raporu duyuruları hariç. |
| EPİAŞ – Elektrik Piyasası Duyuruları | `epias.com.tr/spot-elektrik-piyasasi/elektrik-piyasasi-duyurulari` → `/tum-duyurular/…` | HTML liste | **Etkin.** Aylık uzlaştırma bildirimi, planlı bakım, teminat takvimi gibi rutin duyurular hariç. |
| ETKB – Duyurular | `enerji.gov.tr/medya-duyurular-liste` → `duyuru-detay?id=NNNNN` | HTML liste | **Etkin.** Tek tek santral imar planı duyuruları hariç; YEKA ve lisanssız üretim duyuruları dahil. |
| ETKB – Mevzuat RSS | `enerji.gov.tr/RSS/15?t=1` | RSS | **Kapalı.** GitHub Actions'tan alınan akışta kayıtların bağlantısı yoktu (yalnızca başlık, açıklama, tarih); içerik zamana göre değişiyor. ETKB duyuruları HTML listesinden izleniyor. Düzelirse `enabled: true` yapın. |
| TEİAŞ – Duyurular | `teias.gov.tr/duyurular` → `/duyurular/<slug>` | HTML liste | **Etkin.** Kapasite mekanizması, yan hizmetler, lisanssız kapasite tahsisleri. Personel duyuruları hariç. |
| EPİAŞ – Doğal Gaz Duyuruları | `epias.com.tr/spot-dogal-gaz-piyasasi/dogal-gaz-piyasasi-duyurulari` | HTML liste | **Kapalı.** Adres doğrulandı, liste yapısı doğrulanmadı. `check-sources --all` ✔ gösterirse `enabled: true` yapın. |
| EPİAŞ – Çevresel Piyasalar | `epias.com.tr/cevresel-piyasalar/cevresel-piyasa-duyurulari` | HTML liste | **Kapalı.** Aynı nedenle. |
| EPDK – Elektrik Kurul Kararları listesi | `…/3-0-39-3/son-kurul-kararlari/elektrik` | — | **Kapalı.** Tablo JavaScript ile dolduruluyor. Kararlar Resmî Gazete'den izleniyor. |
| Mevzuat Bilgi Sistemi | `mevzuat.gov.tr` | — | **Kapalı.** JavaScript gerekiyor, belgelenmiş API yok. |

Kaynak listesi `enerji_takip/varsayilan/sources.yaml` içindedir; değiştirmek için `enerji-takip init-config`
ile `config/sources.yaml` oluşturun. Kapalı kaynaklar raporun "Kaynak durumu" tablosunda "İzlenmiyor" olarak görünür. Böylece hangi kaynağın
kontrol **edilmediği** her gün görülür.

**Tekilleştirme:** EPİAŞ veya TEİAŞ duyurusu, Resmî Gazete'de son 10 günde tespit edilmiş bir EPDK kararının
numarasına başlıkta veya metnin başında atıf yapıyorsa ikinci kez bildirilmez.

## Entegrasyonlar (hepsi ücretsiz)

| Entegrasyon | Uç nokta | Yapılandırma |
|---|---|---|
| Gemini API (OpenAI uyumlu katman) | `POST https://generativelanguage.googleapis.com/v1beta/openai/chat/completions` | `GEMINI_API_KEY`, zincirde `gemini:<model>` |
| Groq API (OpenAI uyumlu) | `POST https://api.groq.com/openai/v1/chat/completions` | `GROQ_API_KEY`, zincirde `groq:<model>` |
| Ollama (yerel) | `POST http://localhost:11434/v1/chat/completions` | `OLLAMA_BASE_URL`, zincirde `ollama:<model>` |
| Gmail SMTP | `smtp.gmail.com:587` STARTTLS | `SMTP_USERNAME`, `SMTP_PASSWORD` (uygulama şifresi), `EMAIL_TO` |
| GitHub Actions | `.github/workflows/enerji-mevzuat-takip.yml` | Depo secret'ları; veri `veri` dalında |
| Kurum siteleri | HTTPS GET; robots.txt; istekler arası 1,5 sn | `HTTP_USER_AGENT` |
| MCP istemcileri | stdio (`enerji-mevzuat-mcp`) | Anahtar gerekmez; veri `~/.enerji-mevzuat-takip` |
