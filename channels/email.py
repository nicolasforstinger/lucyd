#!/usr/bin/env python3
"""Email bridge — IMAP polling → daemon HTTP API → SMTP reply.

Polls an IMAP mailbox for unread emails, sends them to the daemon,
and replies via SMTP.

Run:  python3 channels/email.py
Config: LUCYD_EMAIL_CONFIG env var, or email.toml in working dir.
        Falls back to env vars for backward compatibility.
"""

from __future__ import annotations

import asyncio
import base64
import email
import email.utils
import imaplib
import logging
import os
import smtplib
import sys
from email import encoders
from email.message import Message
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.parser import BytesParser
from typing import Any

import httpx

from pathlib import Path

log = logging.getLogger("bridge.email")

# ─── Config (defaults — overridden by load_config) ──────────────

URL = "http://127.0.0.1:8100"
IMAP_HOST = ""
SMTP_HOST = ""
USER = ""
PASSWORD = ""
FOLDER = "INBOX"
POLL_INTERVAL = 60
FROM_ADDR = ""
IMAP_PORT = 0
SMTP_PORT = 0
SECURITY = "ssl"  # "ssl" or "starttls"
ALLOWED_SENDERS: list[str] = []  # empty = allow all


def load_config() -> None:
    """Load bridge config from email.toml, lucyd.toml [email] section, or env vars.

    Search order:
    1. LUCYD_EMAIL_CONFIG env var (explicit path)
    2. email.toml / /config/email.toml (standalone bridge config)
    3. LUCYD_CONFIG env var → lucyd.toml [email] section
    4. Environment variables (backward compat)
    """
    global URL, IMAP_HOST, SMTP_HOST, USER, PASSWORD, FOLDER, POLL_INTERVAL, FROM_ADDR, \
        IMAP_PORT, SMTP_PORT, SECURITY, ALLOWED_SENDERS

    config_path = os.environ.get("LUCYD_EMAIL_CONFIG", "")
    if not config_path:
        for p in ["email.toml", "/config/email.toml"]:
            if Path(p).exists():
                config_path = p
                break
    # Fall back to lucyd.toml if it has an [email] section
    if not config_path:
        lucyd_config = os.environ.get("LUCYD_CONFIG", "")
        if lucyd_config and Path(lucyd_config).exists():
            try:
                import tomllib
                with Path(lucyd_config).open("rb") as f:
                    data = tomllib.load(f)
                if "email" in data:
                    config_path = lucyd_config
            except Exception:
                pass

    if config_path and Path(config_path).exists():
        try:
            import tomllib
            with Path(config_path).open("rb") as f:
                data = tomllib.load(f)

            daemon = data.get("daemon", {})
            URL = daemon.get("url", URL)
            daemon_token_env = daemon.get("token_env", "")
            if daemon_token_env:
                token = os.environ.get(daemon_token_env, "")
                if token:
                    os.environ.setdefault("LUCYD_HTTP_TOKEN", token)

            em = data.get("email", {})
            IMAP_HOST = em.get("imap_host", IMAP_HOST)
            SMTP_HOST = em.get("smtp_host", SMTP_HOST)
            user_env = em.get("user_env", "LUCYD_EMAIL_USER")
            password_env = em.get("password_env", "LUCYD_EMAIL_PASSWORD")
            USER = os.environ.get(user_env, "")
            PASSWORD = os.environ.get(password_env, "")
            FOLDER = em.get("folder", FOLDER)
            POLL_INTERVAL = em.get("poll_interval", POLL_INTERVAL)
            FROM_ADDR = em.get("from_address", "") or USER
            IMAP_PORT = em.get("imap_port", IMAP_PORT)
            SMTP_PORT = em.get("smtp_port", SMTP_PORT)
            SECURITY = em.get("security", SECURITY)
            ALLOWED_SENDERS = [
                s.lower() for s in em.get("allowed_senders", ALLOWED_SENDERS)
            ]

            log.info("Loaded config from %s", config_path)
            return

        except Exception as e:
            log.warning("Failed to load config %s: %s", config_path, e, exc_info=True)

    # Fallback: env vars only (backward compat)
    URL = os.environ.get("LUCYD_URL", URL)
    IMAP_HOST = os.environ.get("LUCYD_EMAIL_IMAP_HOST", "")
    SMTP_HOST = os.environ.get("LUCYD_EMAIL_SMTP_HOST", "")
    USER = os.environ.get("LUCYD_EMAIL_USER", "")
    PASSWORD = os.environ.get("LUCYD_EMAIL_PASSWORD", "")
    FOLDER = os.environ.get("LUCYD_EMAIL_FOLDER", FOLDER)
    POLL_INTERVAL = int(os.environ.get("LUCYD_EMAIL_POLL_INTERVAL", str(POLL_INTERVAL)))
    FROM_ADDR = os.environ.get("LUCYD_EMAIL_FROM", "") or USER
    IMAP_PORT = int(os.environ.get("LUCYD_EMAIL_IMAP_PORT", str(IMAP_PORT)))
    SMTP_PORT = int(os.environ.get("LUCYD_EMAIL_SMTP_PORT", str(SMTP_PORT)))
    SECURITY = os.environ.get("LUCYD_EMAIL_SECURITY", SECURITY)
    raw_senders = os.environ.get("LUCYD_EMAIL_ALLOWED_SENDERS", "")
    if raw_senders:
        ALLOWED_SENDERS = [s.strip().lower() for s in raw_senders.split(",") if s.strip()]


def _imap_connect() -> imaplib.IMAP4:
    """Open and authenticate an IMAP connection."""
    imap_args = (IMAP_HOST, IMAP_PORT) if IMAP_PORT else (IMAP_HOST,)
    if SECURITY == "starttls":
        imap: imaplib.IMAP4 = imaplib.IMAP4(*imap_args)
        imap.starttls()
    else:
        imap = imaplib.IMAP4_SSL(*imap_args)
    imap.login(USER, PASSWORD)
    imap.select(FOLDER)
    return imap


def _extract_attachments(msg: Message) -> list[dict[str, str]]:
    """Extract non-text MIME parts as base64-encoded attachment dicts.

    Returns list of {"content_type", "data", "filename"} matching the
    daemon's HTTP attachment format (same as the Telegram bridge).
    """
    attachments: list[dict[str, str]] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        content_type = part.get_content_type()
        disposition = str(part.get("Content-Disposition", ""))
        # Skip text body parts (extracted separately), but keep text files
        # that are explicitly attached
        if content_type in ("text/plain", "text/html") and "attachment" not in disposition:
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes) or not payload:
            continue
        filename = part.get_filename() or "attachment"
        attachments.append({
            "content_type": content_type,
            "data": base64.b64encode(payload).decode(),
            "filename": filename,
        })
    return attachments


def fetch_and_mark(processed_uids: list[bytes]) -> list[dict[str, Any]]:
    """Fetch unread emails and mark previously processed ones as read.

    Uses a single IMAP connection for both operations.
    Returns list of {uid, from, subject, body, attachments}.
    """
    messages = []
    try:
        imap = _imap_connect()
        try:
            # Mark previous batch as read
            for uid in processed_uids:
                try:
                    imap.store(uid.decode(), "+FLAGS", "\\Seen")
                except Exception as e:
                    log.warning("Failed to mark %s as read: %s", uid, e)

            # Fetch new unread
            _, data = imap.search(None, "UNSEEN")
            uids = data[0].split()

            for uid in uids:
                _, msg_data = imap.fetch(uid, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                assert isinstance(raw, bytes)
                msg = BytesParser().parsebytes(raw)

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            payload = part.get_payload(decode=True)
                            if isinstance(payload, bytes):
                                body = payload.decode("utf-8", errors="replace")
                            break
                else:
                    payload = msg.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        body = payload.decode("utf-8", errors="replace")

                from_addr = email.utils.parseaddr(msg.get("From", ""))[1]
                subject = msg.get("Subject", "(no subject)")
                attachments = _extract_attachments(msg) if msg.is_multipart() else []

                messages.append({
                    "uid": uid,
                    "from": from_addr,
                    "subject": subject,
                    "body": body.strip(),
                    "attachments": attachments,
                })
        finally:
            try:
                imap.close()
                imap.logout()
            except Exception:
                pass
    except Exception as e:
        log.error("IMAP error: %s", e, exc_info=True)

    return messages


def send_reply(
    to: str,
    subject: str,
    body: str,
    attachments: list[dict[str, str]] | None = None,
) -> None:
    """Send email reply via SMTP, optionally with file attachments.

    Raises on SMTP failure so the caller can handle it (e.g. skip marking
    the email as processed, notify the daemon).
    """
    try:
        if attachments:
            msg: MIMEMultipart | MIMEText = MIMEMultipart("mixed")
            msg.attach(MIMEText(body, "plain", "utf-8"))
            for att in attachments:
                maintype, _, subtype = att.get("content_type", "application/octet-stream").partition("/")
                part = MIMEBase(maintype, subtype or "octet-stream")
                part.set_payload(base64.b64decode(att.get("data", "")))
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition", "attachment",
                    filename=att.get("filename", "attachment"),
                )
                msg.attach(part)
        else:
            msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = FROM_ADDR
        msg["To"] = to
        msg["Subject"] = subject

        smtp_args = (SMTP_HOST, SMTP_PORT) if SMTP_PORT else (SMTP_HOST,)
        if SECURITY == "starttls":
            with smtplib.SMTP(*smtp_args) as smtp:
                smtp.starttls()
                smtp.login(USER, PASSWORD)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP_SSL(*smtp_args) as smtp:
                smtp.login(USER, PASSWORD)
                smtp.send_message(msg)

        log.info("Reply sent to %s", to)
    except Exception as e:
        log.error("SMTP error: %s", e, exc_info=True)
        raise


async def poll_loop() -> None:
    """Main loop: fetch unread → POST to daemon → reply via SMTP."""
    log.info("Email bridge started: %s@%s → %s", USER, IMAP_HOST, URL)

    token = os.environ.get("LUCYD_HTTP_TOKEN", "")
    auth_headers = {"Authorization": f"Bearer {token}"} if token else {}
    processed_uids: list[bytes] = []

    async with httpx.AsyncClient(timeout=300, headers=auth_headers) as client:
        while True:
            emails = await asyncio.get_event_loop().run_in_executor(
                None, fetch_and_mark, processed_uids,
            )
            processed_uids = []

            for msg in emails:
                if not msg["body"] and not msg.get("attachments"):
                    processed_uids.append(msg["uid"])
                    continue

                if ALLOWED_SENDERS and msg["from"].lower() not in ALLOWED_SENDERS:
                    log.info("Ignoring email from unauthorized sender %s", msg["from"])
                    processed_uids.append(msg["uid"])
                    continue

                log.info("Processing email from %s: %s", msg["from"], msg["subject"])

                try:
                    request_body: dict[str, Any] = {
                        "message": msg["body"],
                        "sender": msg["from"],
                        "channel_id": "email",
                    }
                    if msg.get("attachments"):
                        request_body["attachments"] = msg["attachments"]

                    resp = await client.post(
                        f"{URL}/api/v1/chat", json=request_body,
                    )
                    data = resp.json()
                    reply = data.get("reply", "")
                    outbound_atts: list[dict[str, str]] = data.get("attachments", [])

                    if reply or outbound_atts:
                        subject = msg["subject"]
                        if not subject.lower().startswith("re:"):
                            subject = f"Re: {subject}"
                        try:
                            await asyncio.get_event_loop().run_in_executor(
                                None, send_reply, msg["from"], subject,
                                reply, outbound_atts or None,
                            )
                        except Exception as smtp_err:
                            await _notify_delivery_failure(
                                client, msg["from"], smtp_err,
                            )
                            continue  # don't mark UID processed — retry next poll

                    processed_uids.append(msg["uid"])

                except Exception as e:
                    log.error("Failed to process email from %s: %s", msg["from"], e, exc_info=True)

            await asyncio.sleep(POLL_INTERVAL)


async def _notify_delivery_failure(
    client: httpx.AsyncClient, recipient: str, error: Exception,
) -> None:
    """POST delivery failure to daemon's /notify endpoint."""
    message = f"Email delivery to {recipient} failed: {error}"
    try:
        await client.post(
            f"{URL}/api/v1/notify",
            json={"message": message, "source": "email"},
            timeout=10,
        )
    except Exception as e:
        log.error("Failed to notify daemon of delivery failure: %s", e)


async def main() -> None:
    load_config()
    if not all([IMAP_HOST, SMTP_HOST, USER, PASSWORD]):
        sys.exit("Email bridge requires imap_host, smtp_host, user, and password. "
                 "Set via email.toml or environment variables.")
    await poll_loop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(main())
