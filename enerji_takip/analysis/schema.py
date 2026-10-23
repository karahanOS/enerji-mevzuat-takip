"""Yapay zekâ çıktısının zorunlu JSON şeması (pydantic ile doğrulanır)."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..text_utils import tr_upper

ASPECTS = [
    "yeni_yukumluluk",
    "yukumluluk_degisikligi",
    "yeni_uygulama_tarihi",
    "yaptirim",
    "gecis_suresi_istisna",
    "yururlukten_kaldirma",
    "fiyat_tarife_degisikligi",
    "piyasa_kurali_degisikligi",
    "destek_tesvik_degisikligi",
    "lisans_rejimi_degisikligi",
    "tanim_degisikligi",
    "usul_degisikligi",
    "diger",
]

_ASCII = str.maketrans("ÇĞİÖŞÜÂÎÛ", "CGIOSUAIU")


def norm_enum(value) -> str:
    return tr_upper(str(value)).translate(_ASCII).strip().replace(" ", "_").replace("-", "_")


EvetHayir = Literal["evet", "hayir", "belirsiz"]


def _text(value):
    """Modeller 'madde' gibi alanlara null ya da sayı (ör. 5) verebilir; metne çevirir."""
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return ", ".join(str(x) for x in value if x is not None)
    return value


def _norm_ehb(value):
    if isinstance(value, bool):
        return "evet" if value else "hayir"
    v = norm_enum(value).lower()
    return {"var": "evet", "yok": "hayir"}.get(v, v)


class Provision(BaseModel):
    hukum: str = Field(description="Mevzuatta AÇIKÇA yer alan hüküm, sade dille")
    madde: str = Field(default="", description="İlgili madde/fıkra")
    alinti: str = Field(default="", description="Metinden BİREBİR kısa alıntı (en fazla ~30 kelime)")

    @field_validator("madde", "alinti", mode="before")
    @classmethod
    def _t(cls, v):
        return _text(v)


class Flagged(BaseModel):
    var_mi: EvetHayir
    aciklama: str = ""
    madde: str = ""

    @field_validator("aciklama", "madde", mode="before")
    @classmethod
    def _t(cls, v):
        return _text(v)

    @field_validator("var_mi", mode="before")
    @classmethod
    def _n(cls, v):
        return _norm_ehb(v)


class DateItem(BaseModel):
    tur: Literal["yururluk", "uyum_son_tarihi", "gecis_suresi_sonu", "basvuru_suresi", "diger"]
    hukum: str = "Tüm hükümler"
    tarih: str | None = None
    metindeki_ifade: str = ""
    kesinlik: Literal["kesin", "metinden_hesaplanan", "belirsiz"]

    @field_validator("tarih", mode="before")
    @classmethod
    def _date(cls, v):
        if v in (None, "", "null", "belirsiz"):
            return None
        v = str(v).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("tarih YYYY-AA-GG biçiminde olmalı ya da null")
        return v

    @field_validator("tur", "kesinlik", mode="before")
    @classmethod
    def _lower(cls, v):
        return norm_enum(v).lower()


class MarketImpact(BaseModel):
    duzey: Literal["genel", "sektorel", "sinirli", "belirsiz", "yok"] = Field(
        description="genel: tüm piyasayı/fiyatları etkiler; sektorel: bir alt piyasa veya katılımcı grubu; "
                    "sinirli: tek şirket/proje; yok: piyasa etkisi yok")
    aciklama: str

    @field_validator("duzey", mode="before")
    @classmethod
    def _lower(cls, v):
        return norm_enum(v).lower()


class AnalysisResult(BaseModel):
    ilgili_mi: EvetHayir
    ilgililik_gerekcesi: str
    ozet: str = Field(description="3-5 cümlelik Türkçe özet")
    ne_degisti: str
    onceki_duzenleme: str
    yeni_duzenleme: str
    acik_hukumler: list[Provision] = Field(default_factory=list)
    cikarimlar: list[str] = Field(default_factory=list)
    piyasa_etkisi: MarketImpact
    kisisel_ilgi: str = Field(description="Kullanıcı profili açısından neden önemli (ör. analiz/modellerine etkisi)")
    ilgili_alanlar: list[str] = Field(default_factory=list)
    etkilenen_piyasa_unsurlari: list[str] = Field(default_factory=list, description="ör. PTF, SMF, YEKDEM, dağıtım bedeli")
    kimler_etkilenir: list[str] = Field(default_factory=list, description="ör. üreticiler, tedarikçiler, tüketiciler")
    yeni_yukumluluk: Flagged
    yukumluluk_degisikligi: Flagged
    tarihler: list[DateItem] = Field(default_factory=list)
    yaptirim: Flagged
    gecis_suresi_istisna: Flagged
    degisiklik_nitelikleri: list[str] = Field(default_factory=list)
    takip_onerileri: list[str] = Field(default_factory=list)
    onem_onerisi: Literal["KRITIK", "YUKSEK", "ORTA", "DUSUK"]
    onem_gerekcesi: str
    guven: Literal["yuksek", "orta", "dusuk"]
    metin_eksik_mi: bool

    @field_validator("ilgili_mi", mode="before")
    @classmethod
    def _ilgili(cls, v):
        return _norm_ehb(v)

    @field_validator("onem_onerisi", mode="before")
    @classmethod
    def _level(cls, v):
        return norm_enum(v)

    @field_validator("guven", mode="before")
    @classmethod
    def _guven(cls, v):
        return norm_enum(v).lower()

    @field_validator("degisiklik_nitelikleri", mode="after")
    @classmethod
    def _aspects(cls, v: list[str]) -> list[str]:
        out = []
        for item in v:
            key = norm_enum(item).lower()
            out.append(key if key in ASPECTS else "diger")
        return list(dict.fromkeys(out))

    @field_validator("ozet")
    @classmethod
    def _ozet(cls, v: str) -> str:
        if len(v.strip()) < 40:
            raise ValueError("özet çok kısa (3-5 cümle beklenir)")
        return v.strip()


class TriageResult(BaseModel):
    ilgili_mi: EvetHayir
    gerekce: str

    @field_validator("ilgili_mi", mode="before")
    @classmethod
    def _ilgili(cls, v):
        return _norm_ehb(v)


def map_to_known(items: list[str], known: list[str]) -> list[str]:
    """AI'nın verdiği alan adlarını profildeki adlara eşler ("YEKDEM" → "Yenilenebilir enerji ve YEKDEM")."""
    index = {norm_enum(k): k for k in known}
    out: list[str] = []
    for item in items:
        key = norm_enum(item)
        match = index.get(key) or next(
            (k for nk, k in index.items() if key and (key in nk or nk in key)), None)
        if match is None:
            # Kelime bazında kesişim (ör. "Doğal gaz" → "Doğal gaz piyasası")
            words = set(key.split("_"))
            match = next((k for nk, k in index.items() if words & set(nk.split("_")) - {"VE", "PIYASASI"}), None)
        if match and match not in out:
            out.append(match)
    return out
