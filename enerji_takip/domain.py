"""Alan (domain) profili: enerji dışındaki takip alanları için metin ve kurallar.

Varsayılan değerler enerji piyasası takibidir. ``profile.yaml`` içinde bir ``alan:`` bölümü varsa
AI istemi, ön eleme istemi, önem sınıflandırmasının etki etiketleri ve rapor başlıkları oradan okunur.
Böylece aynı kod tabanı farklı bir alanda (ör. akaryakıt/LPG taşımacılığı, İSG) yalnızca
``config/`` klasörüyle çalıştırılabilir.

Örnek (profile.yaml)::

    alan:
      rapor_basligi: Akaryakıt ve Gaz Taşımacılığı Mevzuatı
      konu: akaryakıt ve gaz taşımacılığı
      e_posta_etiketi: MEVZUAT TAKİP
      uzmanlik: >-
        Türkiye'de karayoluyla akaryakıt ve LPG taşımacılığı, tehlikeli madde (ADR), iş sağlığı ve
        güvenliği ve çevre mevzuatı
      etki_basligi: Şirkete etkisi
      etki_duzeyleri:
        genel: "tüm tehlikeli madde taşımacılarını / işverenleri doğrudan bağlar"
        ...
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_IMPACT_DEFINITIONS = {
    "genel": "tüm elektrik/gaz piyasasını, fiyat oluşumunu veya tüm katılımcıları etkiler",
    "sektorel": "bir alt piyasa veya katılımcı grubu (ör. YEKDEM katılımcıları, dağıtım şirketleri)",
    "sinirli": "tek şirket, tesis veya proje",
    "yok": "piyasa etkisi yok",
}
DEFAULT_IMPACT_LABELS = {"genel": "Piyasa geneli", "sektorel": "Sektörel", "sinirli": "Sınırlı (tek şirket/proje)",
                         "belirsiz": "Belirsiz", "yok": "Piyasa etkisi yok"}
DEFAULT_IMPORTANT_ASPECTS = ["fiyat_tarife_degisikligi", "piyasa_kurali_degisikligi", "destek_tesvik_degisikligi",
                             "lisans_rejimi_degisikligi"]
DEFAULT_SEVERITY = (
    "KRITIK = piyasa genelini kısa sürede doğrudan etkileyen değişiklik (ör. fiyat limitleri, "
    "dengeleme/uzlaştırma kuralları, YEKDEM bedeli yöntemi, tarife metodolojisi) ve yürürlüğü yakın; YUKSEK = önemli "
    "yapısal değişiklik (yeni yönetmelik, lisans rejimi, destek mekanizması); ORTA = belirli bir alt alanı etkiler; "
    "DUSUK = bilgilendirme veya tek şirkete özgü karar."
)
DEFAULT_EXPERTISE = ("Türkiye enerji piyasaları (elektrik, doğal gaz, yenilenebilir enerji, petrol/LPG, karbon "
                     "piyasaları)")
DEFAULT_TRIAGE_TOPICS = ("Türkiye elektrik, doğal gaz, yenilenebilir enerji, petrol/LPG, nükleer, enerji verimliliği "
                         "veya karbon/iklim piyasalarıyla")


@dataclass
class Domain:
    rapor_basligi: str = "Enerji Mevzuatı"
    ust_baslik: str = "Enerji Mevzuat Takibi"
    konu: str = "enerji sektörü"
    konu_kisa: str = "enerjiyle"
    e_posta_etiketi: str = "ENERJİ MEVZUAT"
    uzmanlik: str = DEFAULT_EXPERTISE
    on_eleme_konulari: str = DEFAULT_TRIAGE_TOPICS
    on_eleme_adi: str = "enerji mevzuatı"
    tahmin_ornegi: str = "Fiyat veya piyasa sonucu"
    etki_basligi: str = "Piyasaya etkisi"
    ilgi_basligi: str = "Benim açımdan önemi"
    etki_ozne: str = "piyasa"
    etkilenen_unsurlar_basligi: str = "Etkilenen piyasa unsurları"
    etki_duzeyleri: dict = field(default_factory=lambda: dict(DEFAULT_IMPACT_DEFINITIONS))
    etki_etiketleri: dict = field(default_factory=lambda: dict(DEFAULT_IMPACT_LABELS))
    onemli_nitelikler: list = field(default_factory=lambda: list(DEFAULT_IMPORTANT_ASPECTS))
    onemli_nitelik_gerekcesi: str = "fiyat/tarife, piyasa kuralı, destek veya lisans rejimi değişikliği"
    yukumluluk_ozne: str = "piyasa katılımcıları"
    onem_kriterleri: str = DEFAULT_SEVERITY
    sorumluluk_reddi: str = ""  # boşsa varsayılan metin (alan profilinde "yatırım tavsiyesi" ibaresi çıkarılır)
    is_default: bool = True

    @classmethod
    def from_profile(cls, profile: dict | None) -> "Domain":
        data = dict((profile or {}).get("alan") or {})
        if not data:
            return cls()
        d = cls(is_default=False)
        for key, value in data.items():
            if not hasattr(d, key) or key == "is_default" or value in (None, ""):
                continue
            if isinstance(getattr(d, key), dict):
                merged = dict(getattr(d, key))
                merged.update(value or {})
                setattr(d, key, merged)
            else:
                setattr(d, key, value)
        if "etki_etiketleri" not in data and "etki_duzeyleri" in data:
            d.etki_etiketleri = {"genel": "Genel", "sektorel": "Sektörel", "sinirli": "Sınırlı",
                                 "belirsiz": "Belirsiz", "yok": "Etkisi yok"}
        return d

    def impact_label(self, level: str) -> str:
        return self.etki_etiketleri.get(level, level)


DEFAULT_DOMAIN = Domain()
