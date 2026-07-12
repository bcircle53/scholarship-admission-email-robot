#!/usr/bin/env python3
"""
unsubscribe_monitor.py — Scholarship Robot Inbox Monitor
Reads IMAP inbox at scholarship@admission.lulllitcloud.com.
Classifies replies as STOP / positive / neutral and acts accordingly.
All libraries are Python built-in — no pip install required.

Phase 1: triggered via workflow_dispatch only.
Phase 2: cron added to .github/workflows/unsubscribe_monitor.yml after test passes.
"""

import os
import re
import csv
import imaplib
import email
import email.header
import smtplib
import logging
import argparse
from datetime import date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ============================================
# CONFIG
# ============================================

IMAP_HOST = "admission.lulllitcloud.com"
IMAP_PORT = 993

# ROMANCE_EMAIL defaults to the known address if secret is missing OR empty string
# (secret name kept as ROMANCE_EMAIL for template consistency — see workflow YAML notes)
IMAP_USER = os.environ.get("ROMANCE_EMAIL") or "scholarship@admission.lulllitcloud.com"
IMAP_PASS = os.environ.get("ROMANCE_EMAIL_PASS", "")

FROM_NAME  = "Scholarship Assistant"
FROM_EMAIL = "scholarship@admission.lulllitcloud.com"

TRACKING_FILE     = "email_tracking.csv"
UNSUBSCRIBED_FILE = "unsubscribed_emails.txt"
MONITOR_STATE_FILE = "monitor_processed.txt"  # Message-IDs already handled

# How many days back to scan (catches emails you've already opened)
LOOKBACK_DAYS = 3

TRACKING_FIELDS = [
    "email", "cohort_start",
    "hook_sent", "value_sent", "followup_sent", "close_sent",
]

# Brevo SMTP relay for auto-replies (same creds as sender.py)
BREVO = {
    "host": "smtp-relay.brevo.com",
    "port": 587,
    "user": os.environ.get("BREVO_USER", ""),
    "pass": os.environ.get("BREVO_PASS", ""),
}

# ============================================
# AUTOMATED-SENDER BLOCKLIST
# ============================================

# Email address patterns that indicate automated / transactional senders.
# We never reply to these — they're not real people.
AUTOMATED_ADDRESS_PATTERNS = re.compile(
    r"(noreply|no-reply|no\.reply|donotreply|do-not-reply|"
    r"mailer-daemon|postmaster|bounce|bounces|"
    r"notification|notifications|alert|alerts|"
    r"support@.*brevo|account-alerts|t\.brevo\.com|"
    r"mailjet\.com|sendinblue\.com|"
    r"^cpanel@|^webmaster@|^root@|^admin@)",
    re.IGNORECASE,
)

# Subject-line patterns that indicate an automated ticketing/confirmation system —
# catches auto-responders that don't set a proper Auto-Submitted header (many don't,
# despite RFC 3834). Found via a live run: support@unischolarz.com ("Thank You for
# Reaching Out to Us!") and help@doctutorials.com ("[##46626##] Your ticket has been
# created") both slipped past the header check and got a real auto-reply sent to them.
AUTOMATED_SUBJECT_PATTERNS = re.compile(
    r"(\[#{1,2}\d+#{1,2}\]|ticket (has been|#|created|received)|"
    r"case\s*#|thank you for (reaching out|contacting|your (email|message|inquiry))|"
    r"we('ve| have) received your|your (inquiry|request) has been received)",
    re.IGNORECASE,
)

def is_automated(msg, sender_email: str, subject: str = "") -> bool:
    """
    Return True if the message appears to be from an automated system.
    Checks sender address patterns, our own sending domain, standard email
    headers, and (as a fallback for systems that don't set proper headers)
    subject-line patterns typical of ticketing/confirmation auto-responders.
    """
    # Pattern match on the sender address itself
    if AUTOMATED_ADDRESS_PATTERNS.search(sender_email):
        return True

    # Never reply to anything sent from our own sending domain — that's always
    # a hosting-system notification (cPanel, mail server alerts, etc.), never a
    # real prospective student. Caught live: cpanel@admission.lulllitcloud.com
    # got a full marketing auto-reply before this check existed.
    own_domain = FROM_EMAIL.split("@", 1)[1].lower()
    if sender_email.endswith("@" + own_domain):
        return True

    # Standard headers that automated mailers set
    auto_submitted = msg.get("Auto-Submitted", "no")
    if auto_submitted.lower() not in ("no", ""):
        return True

    precedence = msg.get("Precedence", "")
    if precedence.lower() in ("bulk", "list", "junk"):
        return True

    if msg.get("X-Auto-Response-Suppress"):
        return True

    # Fallback: subject-line heuristics for auto-responders with no proper headers
    if subject and AUTOMATED_SUBJECT_PATTERNS.search(subject):
        return True

    return False

# ============================================
# KEYWORD CLASSIFIERS
# ============================================

# STOP takes priority — checked first
STOP_KEYWORDS = [
    "stop",
    "unsubscribe",
    "remove me",
    "opt out",
    "opt-out",
    "take me off",
    "don't email",
    "do not email",
    "no more",
    "delete me",
]

POSITIVE_KEYWORDS = [
    "yes",
    "interested",
    "tell me more",
    "sign me up",
    "sounds good",
    "i'm in",
    "im in",
    "count me in",
    "love it",
    "yes please",
    "more info",
    "want to know more",
    "i want",
    "let me in",
    "how do i",
    "sign up",
    "subscribe",
    "where do i",
    "give me",
    "i'd like",
    "id like",
    # Scholarship/study-abroad niche additions — drawn from the Marketing Blueprint's
    # own documented Reply Handling Protocol (real phrases students ask)
    "which countries",
    "what countries",
    "how much",
    "how do i apply",
]

# ============================================
# AUTO-REPLY TEMPLATES
# ============================================

POSITIVE_SUBJECT = "Your Study Abroad Roadmap Is Ready"
POSITIVE_BODY = """\
Hi there, thanks for saying yes — let's get you a roadmap.

We help African students secure tuition-free admission and fully-funded scholarships abroad — Germany, France, UK, Turkey, and more.

Here's what happens next:
1. Fill out our 3-minute Pre-Qualification Form: https://docs.google.com/forms/d/e/1FAIpQLScCZFLY1scNHkzUx6X7NwMj3BGbc1YxbEi0kScherxe5oMnsA/viewform
2. We'll review your profile and match you to the right package (Tier 2 or Tier 3).
3. A member of our team will WhatsApp you within 24 hours to schedule a free 15-minute call.

Our fee ranges from $800 (Admission + Funding) to $1,500 (Full Scholarships) — and comes with a 70% refund if we don't secure an offer after two honest attempts. You risk far less than the opportunity is worth.

Fill out the form here: https://docs.google.com/forms/d/e/1FAIpQLScCZFLY1scNHkzUx6X7NwMj3BGbc1YxbEi0kScherxe5oMnsA/viewform

Talk soon,
Scholarship Assistant"""

NEUTRAL_SUBJECT = "Re: Scholarship Assistant"
NEUTRAL_BODY = """\
Hi there,

Thanks for getting back to us.

We're Scholarship Assistant — we help African final-year students and recent graduates secure tuition-free admission and fully-funded scholarships abroad. You received our email because you were identified as someone exploring study-abroad opportunities.

If you'd like to see if you qualify, fill out our free 3-minute Pre-Qualification Form here: https://docs.google.com/forms/d/e/1FAIpQLScCZFLY1scNHkzUx6X7NwMj3BGbc1YxbEi0kScherxe5oMnsA/viewform

If you'd rather not hear from us again, simply reply with STOP and we'll remove you immediately.

Scholarship Assistant"""

# ============================================
# LOGGING
# ============================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("monitor")

# ============================================
# CLASSIFICATION
# ============================================

def strip_quoted_text(body: str) -> str:
    """
    Remove quoted reply chains from an email body so we only classify
    the fresh text the person actually typed.
    Strips:
      - Lines starting with '>' (standard quoting)
      - Everything from 'On ... wrote:' onwards (Gmail/Outlook style)
      - Everything from '-----Original Message-----' onwards
    """
    # Cut at common quote-header patterns
    cut_patterns = [
        r"\bOn .{10,100} wrote:\s*$",          # Gmail: "On Thu, Jul 10 ... wrote:"
        r"-{3,}\s*Original Message\s*-{3,}",   # Outlook
        r"_{3,}",                               # Outlook underscore divider
        r"From:\s+\S+@\S+",                    # Forwarded message headers
    ]
    lines = body.splitlines()
    clean_lines = []
    for line in lines:
        stripped = line.strip()
        # Stop at quoted lines
        if stripped.startswith(">"):
            break
        # Stop at quote-header patterns
        if any(re.search(p, stripped, re.IGNORECASE) for p in cut_patterns):
            break
        clean_lines.append(line)
    return "\n".join(clean_lines).strip()

def classify(body: str) -> str:
    """
    Returns: "stop", "positive", or "neutral".
    Uses word-boundary regex to avoid false matches (e.g. "stopping" != "stop").
    STOP is checked first — takes priority over positive.
    Only classifies the fresh reply text — quoted original emails are stripped first.
    """
    body_lower = strip_quoted_text(body).lower()

    for kw in STOP_KEYWORDS:
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, body_lower):
            return "stop"

    for kw in POSITIVE_KEYWORDS:
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, body_lower):
            return "positive"

    return "neutral"

# ============================================
# EMAIL PARSING HELPERS
# ============================================

def extract_body(msg) -> str:
    """Return plain-text body from an email.message.Message object."""
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ct   = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in disp:
                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    parts.append(payload.decode(charset, errors="replace"))
                except Exception:
                    pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            parts.append(payload.decode(charset, errors="replace"))
        except Exception:
            pass
    return "\n".join(parts)

def decode_header_value(val: str) -> str:
    """Decode an RFC2047-encoded header value to a plain string."""
    if not val:
        return ""
    parts = email.header.decode_header(val)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)

def extract_sender_email(from_header: str) -> str:
    """
    Pull bare email address from a From header like 'Name <addr@domain.com>'.
    Falls back to the raw header if no angle-bracket address found.
    """
    match = re.search(r"<([^>]+)>", from_header)
    if match:
        return match.group(1).strip().lower()
    return from_header.strip().lower()

# ============================================
# TRACKING — email_tracking.csv
# ============================================

def load_tracking() -> dict:
    tracking = {}
    if not os.path.exists(TRACKING_FILE):
        return tracking
    with open(TRACKING_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tracking[row["email"].lower()] = row
    return tracking

def save_tracking(tracking: dict):
    with open(TRACKING_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRACKING_FIELDS)
        writer.writeheader()
        for row in tracking.values():
            writer.writerow(row)

def mark_complete(tracking: dict, sender_email: str):
    """
    Set all 4 drip-stage flags to "1" for this address.
    If not already in tracking, enroll them fully complete —
    they're already a convert, no need to send the drip sequence.
    """
    if sender_email not in tracking:
        tracking[sender_email] = {
            "email":         sender_email,
            "cohort_start":  str(date.today()),
            "hook_sent":     "1",
            "value_sent":    "1",
            "followup_sent": "1",
            "close_sent":    "1",
        }
    else:
        row = tracking[sender_email]
        row["hook_sent"]     = "1"
        row["value_sent"]    = "1"
        row["followup_sent"] = "1"
        row["close_sent"]    = "1"

# ============================================
# PROCESSED IDs — monitor_processed.txt
# ============================================

def load_processed_ids() -> set:
    """Load Message-IDs that have already been handled."""
    if not os.path.exists(MONITOR_STATE_FILE):
        return set()
    with open(MONITOR_STATE_FILE, "r") as f:
        return {line.strip() for line in f if line.strip()}

def save_processed_id(message_id: str):
    """Append one Message-ID to the processed log."""
    with open(MONITOR_STATE_FILE, "a") as f:
        f.write(message_id.strip() + "\n")

# ============================================
# UNSUBSCRIBED — unsubscribed_emails.txt
# ============================================

def append_unsub(email_addr: str):
    """Append one email address to the permanent opt-out list."""
    with open(UNSUBSCRIBED_FILE, "a") as f:
        f.write(email_addr.strip() + "\n")

# ============================================
# SMTP AUTO-REPLY (Brevo)
# ============================================

def send_reply(to_email: str, subject: str, body: str, dry_run: bool = False) -> bool:
    """
    Send an auto-reply via Brevo SMTP.
    Returns True on success, False on any failure.
    Never touches bounced_emails.txt — reply failures are transient.
    """
    if dry_run:
        log.info(f"[DRY-RUN] Would send '{subject}' -> {to_email}")
        return True

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"]      = to_email
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(BREVO["host"], BREVO["port"], timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(BREVO["user"], BREVO["pass"])
            server.sendmail(FROM_EMAIL, [to_email], msg.as_string())
        log.info(f"REPLIED -> {to_email} | {subject}")
        return True

    except smtplib.SMTPAuthenticationError as e:
        log.error(f"AUTH FAILURE [Brevo] code={e.smtp_code} — check BREVO_USER / BREVO_PASS")
        return False

    except Exception as e:
        log.error(f"Reply send failed -> {to_email}: {e}")
        return False

# ============================================
# MAIN PIPELINE
# ============================================

def run(dry_run: bool = False):
    log.info("=== Scholarship Robot Monitor starting ===")

    if not IMAP_PASS:
        log.error("ROMANCE_EMAIL_PASS not set — exiting.")
        return

    # -- Connect to IMAP --------------------------------------------------
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        log.info(f"Attempting IMAP login as: {IMAP_USER}")
        mail.login(IMAP_USER, IMAP_PASS)
        log.info(f"IMAP connected: {IMAP_USER} @ {IMAP_HOST}:{IMAP_PORT}")
    except Exception as e:
        log.error(f"IMAP connection failed: {e}")
        return

    try:
        mail.select("INBOX")

        # Search ALL messages in the last LOOKBACK_DAYS days (read OR unread).
        # This ensures replies you've manually opened are still processed.
        since_str = (date.today() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        status, data = mail.search(None, f"SINCE {since_str}")
        if status != "OK":
            log.warning("IMAP search returned non-OK status — exiting.")
            return

        msg_ids = data[0].split()
        log.info(f"Messages in last {LOOKBACK_DAYS} days: {len(msg_ids)}")

        if not msg_ids:
            log.info("No messages in lookback window — nothing to process.")
            return

        # Load already-processed Message-IDs to prevent double-handling
        processed_ids  = load_processed_ids()
        tracking       = load_tracking()
        tracking_dirty = False

        processed      = 0
        skipped        = 0
        stop_count     = 0
        positive_count = 0
        neutral_count  = 0

        for msg_id in msg_ids:
            # Fetch full RFC822 message
            status, msg_data = mail.fetch(msg_id, "(RFC822)")
            if status != "OK":
                log.warning(f"Failed to fetch message id={msg_id} — skipping.")
                continue

            raw_email    = msg_data[0][1]
            msg          = email.message_from_bytes(raw_email)

            # Use Message-ID header as unique dedup key
            message_id   = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip()
            from_header  = decode_header_value(msg.get("From", ""))
            sender_email = extract_sender_email(from_header)
            subject      = decode_header_value(msg.get("Subject", "(no subject)"))
            body         = extract_body(msg)

            # Skip if already processed in a previous run
            if message_id and message_id in processed_ids:
                skipped += 1
                continue

            log.info(f"Processing: from={sender_email} | subject={subject[:60]}")

            # Skip our own outbound messages landing in inbox
            if sender_email == FROM_EMAIL.lower():
                log.info("Skipping — message is from our own address.")
                if message_id and not dry_run:
                    save_processed_id(message_id)
                continue

            # Skip automated / transactional senders (Brevo alerts, mailer-daemons, etc.)
            if is_automated(msg, sender_email, subject):
                log.info(f"Skipping — automated sender: {sender_email}")
                if message_id and not dry_run:
                    save_processed_id(message_id)
                continue

            # Classify the reply
            label = classify(body)
            log.info(f"-> Classified: {label.upper()}")

            # -- Act on classification ------------------------------------
            if label == "stop":
                if not dry_run:
                    append_unsub(sender_email)
                log.info(f"STOP: {sender_email} appended to {UNSUBSCRIBED_FILE}")
                stop_count += 1

            elif label == "positive":
                if not dry_run:
                    mark_complete(tracking, sender_email)
                    tracking_dirty = True
                send_reply(sender_email, POSITIVE_SUBJECT, POSITIVE_BODY, dry_run=dry_run)
                log.info(f"POSITIVE: {sender_email} — drip stopped, positive reply queued")
                positive_count += 1

            else:  # neutral
                send_reply(sender_email, NEUTRAL_SUBJECT, NEUTRAL_BODY, dry_run=dry_run)
                log.info(f"NEUTRAL: {sender_email} — holding reply queued")
                neutral_count += 1

            # Record as processed so future runs skip it
            if message_id and not dry_run:
                save_processed_id(message_id)

            processed += 1

        # -- Batch save tracking once at end (only if changed) ------------
        if tracking_dirty:
            save_tracking(tracking)
            log.info("email_tracking.csv saved.")

        log.info(
            f"=== Run complete: processed={processed} | skipped(already handled)={skipped} | "
            f"stop={stop_count} | positive={positive_count} | neutral={neutral_count} ==="
        )

    except Exception as e:
        log.error(f"Unexpected monitor error: {e}")

    finally:
        try:
            mail.logout()
            log.info("IMAP disconnected.")
        except Exception:
            pass

# ============================================
# ENTRY POINT
# ============================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scholarship Robot Inbox Monitor")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate — no SMTP replies, no file writes, no IMAP flag changes",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run)
