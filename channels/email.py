#!/usr/bin/env python3
"""Email bridge — IMAP polling → daemon HTTP API → SMTP reply.

Polls an IMAP mailbox for unread emails, sends them to the daemon,
and replies via SMTP.

Run:  python3 channels/email.py
Config: [email] section in lucyd.toml (path from LUCYD_CONFIG env var).
"""

from __future__ import annotations

import asyncio
import base64
import email
import email.utils
import imaplib
import logging
import mimetypes
import os
import smtplib
import sys
from email import encoders
from email.message import Message
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import httpx
from aiohttp import web

# Put the parent dir (/app in container) on sys.path so `channels` imports
# resolve — the bridge is launched via `python -P channels/email.py`, which
# excludes the script dir and cwd from sys.path.
_app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _app_dir not in sys.path:
    sys.path.insert(0, _app_dir)

from channels.bridge_outbound_server import build_outbound_app  # noqa: E402

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
USER_ADDRESS = ""  # proactive-outbound destination ([email] user_address)


def load_config() -> None:
    """Load bridge config from [email] section in lucyd.toml."""
    global URL, IMAP_HOST, SMTP_HOST, USER, PASSWORD, FOLDER, POLL_INTERVAL, FROM_ADDR, \
        IMAP_PORT, SMTP_PORT, SECURITY, ALLOWED_SENDERS, USER_ADDRESS

    import tomllib

    config_path = os.environ.get("LUCYD_CONFIG", "")
    if not config_path:
        sys.exit("LUCYD_CONFIG environment variable is not set.")

    path = Path(config_path)
    if not path.exists():
        sys.exit(f"Config file not found: {config_path}")

    with path.open("rb") as f:
        data = tomllib.load(f)

    em = data.get("email")
    if not em:
        sys.exit(f"No [email] section in {config_path}")

    IMAP_HOST = em.get("imap_host", "")
    SMTP_HOST = em.get("smtp_host", "")
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
        s.lower() for s in em.get("allowed_senders", [])
    ]
    # Proactive-outbound destination. Distinct from allowed_senders (an inbound
    # filter): this is the single address the daemon's /send pushes to. Defaults
    # to the first allowed sender when unset.
    USER_ADDRESS = em.get("user_address", "") or (
        ALLOWED_SENDERS[0] if ALLOWED_SENDERS else ""
    )

    log.info("Loaded email config from %s", config_path)


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
                except (imaplib.IMAP4.error, OSError) as e:
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
            except (imaplib.IMAP4.error, OSError) as exc:
                log.debug("IMAP cleanup failed: %s", exc)
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


# ─── Outbound (proactive /send listener) ─────────────────────────

_SUBJECT_MAX = 60


def _subject_for(text: str) -> str:
    """Synthesize an email subject from the message text.

    Telegram has no subject; email needs one. Use the first line (clipped),
    falling back to a generic line for attachment-only sends.
    """
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first[:_SUBJECT_MAX] if first else "Message from your assistant"


async def send_text(recipient: str, text: str) -> None:
    """Outbound adapter for the /send listener — SMTP a proactive message."""
    await asyncio.get_event_loop().run_in_executor(
        None, send_reply, recipient, _subject_for(text), text, None,
    )


async def send_attachment(recipient: str, path: str, *, caption: str = "") -> None:
    """Outbound adapter for the /send listener — SMTP an attachment.

    The shared outbound server decodes the attachment to a tempfile and passes
    its path; re-encode it into the dict shape send_reply expects.
    """
    p = Path(path)
    ctype, _enc = mimetypes.guess_type(p.name)
    att = {
        "content_type": ctype or "application/octet-stream",
        "data": base64.b64encode(p.read_bytes()).decode("ascii"),
        "filename": p.name,
    }
    subject = _subject_for(caption) if caption else f"Attachment: {p.name}"
    await asyncio.get_event_loop().run_in_executor(
        None, send_reply, recipient, subject, caption, [att],
    )


async def _start_outbound_server() -> web.AppRunner:
    """Bind the outbound /send server on the conventional email port.

    Port and attachment cap come from bridge_client.BRIDGE_LIMITS (the single
    source of truth for the bridge contract). The recipient is [email]
    user_address, resolved by load_config().
    """
    token = os.environ.get("LUCYD_HTTP_TOKEN", "")
    if not token:
        log.error("LUCYD_HTTP_TOKEN not set; outbound server refusing to start")
        raise RuntimeError("LUCYD_HTTP_TOKEN required for outbound server")
    if not USER_ADDRESS:
        log.error("No [email] user_address (or allowed_senders) configured; "
                  "outbound server refusing to start")
        raise RuntimeError("No email user_address configured for proactive outbound")
    from bridge_client import BRIDGE_LIMITS
    limits = BRIDGE_LIMITS["email"]
    app = build_outbound_app(
        token=token,
        recipient=USER_ADDRESS,
        send_text=send_text,
        send_attachment=send_attachment,
        max_attachment_bytes=limits["max_attachment_bytes"],
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", limits["port"])
    await site.start()
    log.info("Email outbound server listening on 127.0.0.1:%d (to=%s)",
             limits["port"], USER_ADDRESS)
    return runner


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
                    log.info("Dropped message from non-allowlisted sender: %s", msg["from"])
                    processed_uids.append(msg["uid"])
                    continue

                log.info("Processing email from %s: %s", msg["from"], msg["subject"])

                try:
                    request_body: dict[str, Any] = {
                        "message": msg["body"],
                    }
                    if msg.get("attachments"):
                        request_body["attachments"] = msg["attachments"]

                    resp = await client.post(
                        f"{URL}/api/v1/inbound/email", json=request_body,
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
                            await _notify_delivery_failure(msg["from"], str(smtp_err))
                            continue  # don't mark UID processed — retry next poll

                    processed_uids.append(msg["uid"])

                except Exception as e:
                    log.error("Failed to process email from %s: %s", msg["from"], e, exc_info=True)

            await asyncio.sleep(POLL_INTERVAL)


async def _notify_delivery_failure(recipient: str, detail: str) -> None:
    """POST delivery failure to daemon's /system/event (talker=system, sender=error)."""
    message = f"Email delivery to {recipient} failed: {detail}"
    token = os.environ.get("LUCYD_HTTP_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
            await client.post(
                f"{URL}/api/v1/system/event",
                json={"message": message, "sender": "error"},
            )
    except Exception as e:
        log.error("Failed to notify daemon of delivery failure: %s", e)


async def main() -> None:
    load_config()
    if not all([IMAP_HOST, SMTP_HOST, USER, PASSWORD]):
        sys.exit("Email bridge requires imap_host, smtp_host, user, and password "
                 "in [email] section of lucyd.toml.")
    runner = await _start_outbound_server()
    try:
        await poll_loop()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(main())
