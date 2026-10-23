"""E-posta gönderim servisi (SMTP; STARTTLS / SSL / düz). Kimlik bilgileri yalnızca ortam değişkenlerinden."""
from __future__ import annotations

import logging
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Callable

from ..config import Settings

log = logging.getLogger(__name__)


class EmailConfigError(Exception):
    pass


@dataclass
class OutgoingEmail:
    subject: str
    text: str
    html: str | None
    to: list[str]
    cc: list[str]


def build_message(settings: Settings, mail: OutgoingEmail) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = mail.subject
    msg["From"] = settings.email_from
    msg["To"] = ", ".join(mail.to)
    if mail.cc:
        msg["Cc"] = ", ".join(mail.cc)
    msg["Date"] = formatdate(localtime=True)
    domain = (settings.email_from or "localhost").split("@")[-1].strip(">")
    msg["Message-ID"] = make_msgid(domain=domain)
    msg.set_content(mail.text, charset="utf-8")
    if mail.html:
        msg.add_alternative(mail.html, subtype="html", charset="utf-8")
    return msg


def send_once(settings: Settings, mail: OutgoingEmail) -> None:
    problems = settings.email_config_problems()
    if problems:
        raise EmailConfigError("; ".join(problems))
    if not mail.to:
        raise EmailConfigError("Alıcı (EMAIL_TO) tanımlı değil")
    msg = build_message(settings, mail)
    recipients = list(dict.fromkeys(mail.to + mail.cc))
    timeout = 30
    if settings.smtp_security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout,
                                                context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
    try:
        server.ehlo()
        if settings.smtp_security == "starttls":
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password or "")
        refused = server.send_message(msg, to_addrs=recipients)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def send_with_retry(
    settings: Settings,
    mail: OutgoingEmail,
    on_attempt: Callable[[int, str, str | None], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Üstel bekleme ile yeniden dener. Her deneme on_attempt(deneme, durum, hata) ile kaydedilir."""
    attempts = max(1, settings.email_max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            send_once(settings, mail)
        except EmailConfigError as exc:
            if on_attempt:
                on_attempt(attempt, "hata", f"Yapılandırma hatası: {exc}")
            log.error("E-posta yapılandırma hatası: %s", exc)
            return False  # yapılandırma hatası yeniden denemeyle düzelmez
        except (smtplib.SMTPException, OSError) as exc:
            if on_attempt:
                on_attempt(attempt, "hata", f"{type(exc).__name__}: {exc}")
            log.warning("E-posta gönderilemedi (%s/%s): %s", attempt, attempts, exc)
            if attempt < attempts:
                sleep(settings.email_retry_base_seconds * (2 ** (attempt - 1)))
            continue
        if on_attempt:
            on_attempt(attempt, "basarili", None)
        return True
    return False
