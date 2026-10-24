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


def _languages() -> str:
    """Kurulu dil paketine göre: Türkçe varsa 'tur', yoksa 'eng'."""
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=20)
        langs = set(out.stdout.split())
    except Exception:
        return "eng"
    return "tur+eng" if "tur" in langs else "eng"


def ocr_pdf(content: bytes, *, max_pages: int = 8, timeout: int = 120, dpi: int = 200) -> str:
    """PDF'in ilk `max_pages` sayfasını OCR ile okur. Başarısızlıkta boş metin döner."""
    if not ocr_available():
        log.info("OCR atlandı: pdftoppm/tesseract kurulu değil")
        return ""
    lang = _languages()
    try:
        with tempfile.TemporaryDirectory(prefix="enerji-ocr-") as tmp:
            tmp_path = Path(tmp)
            pdf = tmp_path / "in.pdf"
            pdf.write_bytes(content)
            subprocess.run(["pdftoppm", "-r", str(dpi), "-l", str(max_pages), "-png", str(pdf), str(tmp_path / "p")],
                           check=True, capture_output=True, timeout=timeout)
            parts = []
            for img in sorted(tmp_path.glob("p-*.png")):
                res = subprocess.run(["tesseract", str(img), "stdout", "-l", lang],
                                     capture_output=True, text=True, timeout=timeout)
                if res.returncode == 0:
                    parts.append(res.stdout)
            return normalize_space("\n".join(parts))
    except Exception as exc:
        log.warning("OCR başarısız: %s", exc)
        return ""
