#!/usr/bin/env python3
"""Email bridge — IMAP polling → daemon HTTP API → SMTP reply.

Polls an IMAP mailbox for unread emails, sends them to the daemon,
and replies via SMTP.

Run:  python3 channels/email.py
Env:  LUCYD_URL                 (default: http://127.0.0.1:8100)
      LUCYD_EMAIL_IMAP_HOST     (required)
      LUCYD_EMAIL_SMTP_HOST     (required)
      LUCYD_EMAIL_USER          (required)
      LUCYD_EMAIL_PASSWORD      (required)
      LUCYD_EMAIL_FOLDER        (default: INBOX)
      LUCYD_EMAIL_POLL_INTERVAL (default: 60)
      LUCYD_EMAIL_FROM          (default: same as LUCYD_EMAIL_USER)
"""

from __future__ import annotations

import asyncio
import email
import email.utils
import imaplib
import logging
import os
import smtplib
import sys
from email.mime.text import MIMEText

import httpx

log = logging.getLogger("bridge.email")

URL = os.environ.get("LUCYD_URL", "http://127.0.0.1:8100")
IMAP_HOST = os.environ.get("LUCYD_EMAIL_IMAP_HOST", "")
SMTP_HOST = os.environ.get("LUCYD_EMAIL_SMTP_HOST", "")
USER = os.environ.get("LUCYD_EMAIL_USER", "")
PASSWORD = os.environ.get("LUCYD_EMAIL_PASSWORD", "")
FOLDER = os.environ.get("LUCYD_EMAIL_FOLDER", "INBOX")
POLL_INTERVAL = int(os.environ.get("LUCYD_EMAIL_POLL_INTERVAL", "60"))
FROM_ADDR = os.environ.get("LUCYD_EMAIL_FROM", "") or USER


def fetch_unread() -> list[dict]:
    """Fetch unread emails via IMAP. Returns list of {uid, from, subject, body}."""
    messages = []
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST)
        imap.login(USER, PASSWORD)
        imap.select(FOLDER)
        _, data = imap.search(None, "UNSEEN")
        uids = data[0].split()

        for uid in uids:
            _, msg_data = imap.fetch(uid, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            # Extract text body
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode("utf-8", errors="replace")
                        break
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="replace")

            from_addr = email.utils.parseaddr(msg.get("From", ""))[1]
            subject = msg.get("Subject", "(no subject)")

            messages.append({
                "uid": uid,
                "from": from_addr,
                "subject": subject,
                "body": body.strip(),
            })

        imap.close()
        imap.logout()
    except Exception as e:
        log.error("IMAP error: %s", e, exc_info=True)

    return messages


def send_reply(to: str, subject: str, body: str) -> None:
    """Send email reply via SMTP."""
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = FROM_ADDR
        msg["To"] = to
        msg["Subject"] = subject

        with smtplib.SMTP_SSL(SMTP_HOST) as smtp:
            smtp.login(USER, PASSWORD)
            smtp.send_message(msg)

        log.info("Reply sent to %s", to)
    except Exception as e:
        log.error("SMTP error: %s", e, exc_info=True)


def mark_read(uid: bytes) -> None:
    """Mark email as read via IMAP."""
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST)
        imap.login(USER, PASSWORD)
        imap.select(FOLDER)
        imap.store(uid, "+FLAGS", "\\Seen")
        imap.close()
        imap.logout()
    except Exception as e:
        log.warning("Failed to mark as read: %s", e, exc_info=True)


async def poll_loop():
    """Main loop: fetch unread → POST to daemon → reply via SMTP."""
    log.info("Email bridge started: %s@%s → %s", USER, IMAP_HOST, URL)

    async with httpx.AsyncClient(timeout=300) as client:
        while True:
            emails = await asyncio.get_event_loop().run_in_executor(None, fetch_unread)

            for msg in emails:
                if not msg["body"]:
                    continue

                log.info("Processing email from %s: %s", msg["from"], msg["subject"])

                try:
                    resp = await client.post(f"{URL}/api/v1/chat", json={
                        "message": msg["body"],
                        "sender": msg["from"],
                    })
                    data = resp.json()
                    reply = data.get("reply", "")

                    if reply:
                        subject = msg["subject"]
                        if not subject.lower().startswith("re:"):
                            subject = f"Re: {subject}"
                        await asyncio.get_event_loop().run_in_executor(
                            None, send_reply, msg["from"], subject, reply,
                        )

                    mark_read(msg["uid"])

                except Exception as e:
                    log.error("Failed to process email from %s: %s", msg["from"], e, exc_info=True)

            await asyncio.sleep(POLL_INTERVAL)


async def main():
    if not all([IMAP_HOST, SMTP_HOST, USER, PASSWORD]):
        sys.exit("Required env vars: LUCYD_EMAIL_IMAP_HOST, LUCYD_EMAIL_SMTP_HOST, "
                 "LUCYD_EMAIL_USER, LUCYD_EMAIL_PASSWORD")
    await poll_loop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(main())
