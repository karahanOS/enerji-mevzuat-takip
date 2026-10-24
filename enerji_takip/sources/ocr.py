"""Yazı tipi kodlaması bozuk PDF'ler için isteğe bağlı OCR (pdftoppm + tesseract).

EPDK kurul kararı PDF'leri (QuarkXPress) metin katmanı içermez; yalnızca görüntü olarak okunabilir.
İki program da PATH'te yoksa sessizce atlanır ve belge "okunamadı" olarak işaretlenir.
Debian/Ubuntu: apt-get install poppler-utils tesseract-ocr tesseract-ocr-tur
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..text_utils import normalize_space

log = logging.getLogger(__name__)


def ocr_available() -> bool:
    return bool(shutil.which("pdftoppm") and shutil.which("tesseract"))


TUR_URL = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/tur.traineddata"


def _installed_langs(extra: list[str] | None = None) -> set[str]:
    try:
        out = subprocess.run(["tesseract", "--list-langs", *(extra or [])], capture_output=True, text=True, timeout=20)
        return set(out.stdout.split())
    except Exception:
        return set()


def _fetch_tur(dest: Path) -> bool:
    """Sistemde Türkçe paket yoksa tur.traineddata'yı (~2 MB) uygulama dizinine indirir."""
    try:
        import httpx

        dest.mkdir(parents=True, exist_ok=True)
        r = httpx.get(TUR_URL, timeout=60, follow_redirects=True)
        r.raise_for_status()
        if len(r.content) < 100_000:  # hata sayfası vb.
            return False
        (dest / "tur.traineddata").write_bytes(r.content)
        return True
    except Exception as exc:
        log.warning("Türkçe OCR paketi indirilemedi: %s", exc)
        return False


def _tesseract_setup() -> tuple[str, list[str]]:
    """(dil, ek bağımsız değişkenler). Önce sistem 'tur' paketi, sonra uygulama dizinindeki, yoksa indirir, olmazsa 'eng'."""
    langs = _installed_langs()
    if "tur" in langs:
        return ("tur+eng" if "eng" in langs else "tur"), []
    from ..config import app_home

    tessdir = app_home() / "tessdata"
    if not (tessdir / "tur.traineddata").is_file() and not _fetch_tur(tessdir):
        return "eng", []
    return "tur", ["--tessdata-dir", str(tessdir)]


def ocr_pdf(content: bytes, *, max_pages: int = 8, timeout: int = 120, dpi: int = 200) -> str:
    """PDF'in ilk `max_pages` sayfasını OCR ile okur. Başarısızlıkta boş metin döner."""
    if not ocr_available():
        log.info("OCR atlandı: pdftoppm/tesseract kurulu değil")
        return ""
    lang, extra = _tesseract_setup()
    try:
        with tempfile.TemporaryDirectory(prefix="enerji-ocr-") as tmp:
            tmp_path = Path(tmp)
            pdf = tmp_path / "in.pdf"
            pdf.write_bytes(content)
            subprocess.run(["pdftoppm", "-r", str(dpi), "-l", str(max_pages), "-png", str(pdf), str(tmp_path / "p")],
                           check=True, capture_output=True, timeout=timeout)
            parts = []
            for img in sorted(tmp_path.glob("p-*.png")):
                res = subprocess.run(["tesseract", str(img), "stdout", "-l", lang, *extra],
                                     capture_output=True, text=True, timeout=timeout)
                if res.returncode == 0:
                    parts.append(res.stdout)
            return normalize_space("\n".join(parts))
    except Exception as exc:
        log.warning("OCR başarısız: %s", exc)
        return ""
