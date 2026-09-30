"""Notification dispatch: email and Pushover, sent directly at the moment a
backup/restore run finishes (backup_run.py, restore_run.py), plus a
NotificationEvent row for the browser feed (GET /notifications/poll on the
web service) and as an audit trail. No queue/beat step - the worker already
owns the code that knows when a run finishes, so it just sends inline.

Email sending is adapted from scripts/pymail's send() (an existing personal
tool): same SSL-then-plaintext SMTP fallback, trimmed of the CLI/argparse and
JSON-config-file parts in favor of reading from NotificationConfig.
"""
import logging
from smtplib import SMTP, SMTP_SSL
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx
from sqlalchemy.orm import Session

from app.models import NotificationConfig, NotificationEvent

logger = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def _send_email(config: NotificationConfig, subject: str, body: str) -> None:
    if not (config.smtp_host and config.smtp_from and config.smtp_to):
        logger.warning("notify: email enabled but not fully configured, skipping")
        return
    msg = MIMEMultipart()
    msg.attach(MIMEText(body, "plain"))
    msg["Subject"] = subject
    msg["From"] = config.smtp_from
    msg["To"] = config.smtp_to

    conn = None
    try:
        if config.smtp_password:
            try:
                conn = SMTP_SSL(config.smtp_host, config.smtp_port, timeout=10)
                conn.login(config.smtp_username or config.smtp_from, config.smtp_password)
            except Exception:
                conn = SMTP(config.smtp_host, config.smtp_port, timeout=10)
                conn.login(config.smtp_username or config.smtp_from, config.smtp_password)
        else:
            conn = SMTP(config.smtp_host, config.smtp_port, timeout=10)
        conn.sendmail(config.smtp_from, config.smtp_to.split(","), msg.as_string())
    except Exception:
        logger.exception("notify: failed to send email")
    finally:
        if conn:
            try:
                conn.quit()
            except Exception:
                pass


def _send_pushover(config: NotificationConfig, title: str, message: str) -> None:
    if not (config.pushover_api_token and config.pushover_user_key):
        logger.warning("notify: pushover enabled but not fully configured, skipping")
        return
    try:
        httpx.post(
            PUSHOVER_URL,
            data={
                "token": config.pushover_api_token,
                "user": config.pushover_user_key,
                "title": title,
                "message": message,
            },
            timeout=10,
        )
    except Exception:
        logger.exception("notify: failed to send pushover")


def notify(db: Session, event_type: str, level: str, title: str, message: str) -> None:
    """event_type is one of the NotificationConfig.notify_* flags' names
    without the "notify_" prefix (e.g. "backup_done", "restore_failed") -
    used to check whether this event is one the user wants to hear about at
    all, on any channel, before doing any work."""
    config = db.query(NotificationConfig).first()
    if not config:
        return
    if not getattr(config, f"notify_{event_type}", False):
        return

    db.add(NotificationEvent(level=level, title=title, message=message))
    db.commit()

    if config.email_enabled:
        _send_email(config, title, message)
    if config.pushover_enabled:
        _send_pushover(config, title, message)
    # Browser notifications don't need anything sent from here - the
    # NotificationEvent row just written is what GET /notifications/poll
    # (web) serves to the page's poller.
