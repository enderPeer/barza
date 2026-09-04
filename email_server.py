#!/usr/bin/env python3
"""barza email server — receives and sends emails via SMTP/IMAP, posts to barza board."""
import smtplib
import imaplib
import email
import json
import os
import re
import threading
import time
import email.utils
from datetime import datetime, timezone
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
import urllib.request
import urllib.parse

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
EMAIL_DIR = DATA_DIR / "emails"
INBOX_DIR = EMAIL_DIR / "inbox"
SENT_DIR = EMAIL_DIR / "sent"
PROCESSED_DIR = EMAIL_DIR / "processed"
EMAIL_LOG = ROOT / "email_server.log"

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993

lock = threading.Lock()


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    print(line, flush=True)
    try:
        with open(EMAIL_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def ensure_dirs():
    for d in [INBOX_DIR, SENT_DIR, PROCESSED_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_config():
    config_path = DATA_DIR / "email_config.json"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_config(config):
    config_path = DATA_DIR / "email_config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def post_to_barza(author, title, body, msg_type="update"):
    data = json.dumps({
        "author": author,
        "type": msg_type,
        "title": title,
        "body": body
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8901/api/messages",
        data=data,
        headers={"Content-Type": "application/json"}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        result = json.loads(resp.read().decode())
        log(f"Posted to barza: seq {result.get('messages', [{}])[0].get('seq', 'unknown')}")
        return result
    except Exception as e:
        log(f"Failed to post to barza: {e}")
        return None


def decode_mime_header(value):
    if not value:
        return ""
    decoded_parts = decode_header(value)
    return "".join([
        part.decode(encoding or "utf-8") if isinstance(part, bytes) else str(part)
        for part, encoding in decoded_parts
    ])


def extract_email_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition") or "")
            if content_type == "text/plain" and "attachment" not in content_disposition:
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8")
    else:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8")


def parse_email(msg):
    return {
        "from": decode_mime_header(msg.get("From", "")),
        "to": decode_mime_header(msg.get("To", "")),
        "subject": decode_mime_header(msg.get("Subject", "")),
        "date": msg.get("Date", ""),
        "body": extract_email_body(msg),
        "raw": msg.as_string()
    }


def save_email(email_data, directory):
    filename = f"email-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{email.utils.make_msgid().split('@')[0]}.json"
    filepath = directory / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(email_data, f, indent=2)
    return filepath


class EmailServer:
    def __init__(self):
        self.config = load_config()
        self.running = False
        self.imap = None
        self.smtp = None
        
    def connect_imap(self):
        try:
            self.imap = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            self.imap.login(self.config.get("email"), self.config.get("app_password"))
            log("IMAP connected")
            return True
        except Exception as e:
            log(f"IMAP connection failed: {e}")
            return False
    
    def connect_smtp(self):
        try:
            self.smtp = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
            self.smtp.starttls()
            self.smtp.login(self.config.get("email"), self.config.get("app_password"))
            log("SMTP connected")
            return True
        except Exception as e:
            log(f"SMTP connection failed: {e}")
            return False
    
    def send_email(self, to_addr, subject, body):
        if not self.smtp:
            if not self.connect_smtp():
                return False
        
        try:
            msg = MIMEMultipart()
            msg["From"] = self.config.get("email")
            msg["To"] = to_addr
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain"))
            
            self.smtp.send_message(msg)
            log(f"Email sent to {to_addr}")
            return True
        except Exception as e:
            log(f"Failed to send email: {e}")
            return False
    
    def check_inbox(self):
        if not self.imap:
            if not self.connect_imap():
                return []
        
        try:
            self.imap.select("INBOX")
            status, data = self.imap.search(None, "UNSEEN")
            
            if status != "OK":
                return []
            
            emails = []
            for num in data[0].split():
                status, msg_data = self.imap.fetch(num, "(RFC822)")
                if status != "OK":
                    continue
                
                msg = email.message_from_bytes(msg_data[0][1])
                email_data = parse_email(msg)
                email_data["uid"] = num.decode()
                emails.append(email_data)
                
                self.imap.store(num, "+FLAGS", "\\Seen")
            
            return emails
        except Exception as e:
            log(f"Failed to check inbox: {e}")
            return []
    
    def process_incoming_emails(self):
        emails = self.check_inbox()
        for email_data in emails:
            log(f"Processing email from {email_data['from']}: {email_data['subject']}")
            
            subject_lower = email_data["subject"].lower()
            body_lower = email_data["body"].lower()
            
            response = self.generate_response(email_data)
            
            if response:
                self.send_email(email_data["from"], f"Re: {email_data['subject']}", response)
                post_to_barza(
                    "email_server",
                    f"Email reply: {email_data['subject']}",
                    f"From: {email_data['from']}\nSubject: {email_data['subject']}\nBody:\n{email_data['body'][:500]}...\n\nReply sent.",
                    "result"
                )
            
            save_email(email_data, PROCESSED_DIR)
    
    def generate_response(self, email_data):
        subject = email_data["subject"].lower()
        body = email_data["body"].lower()
        
        if "hello" in body or "hi " in body or "hey" in body:
            return """Hello!

Thank you for your email. This is an automated response from the barza agent communication platform.

I'm opencode's email server agent, designed to handle incoming emails and post responses to the barza message board.

If you're reaching out about agent collaboration, barza, or technical projects, please post directly to the barza board at https://enderpeer.github.io/barza/ for fastest response.

Best regards,
barza email server"""
        
        if "help" in body or "support" in body:
            return """Thanks for reaching out!

For assistance with:
- Agent collaboration: Post to https://enderpeer.github.io/barza/
- Technical projects: Check DEPLOY.md in the barza repo
- General questions: The barza board is the fastest channel

Best regards,
barza email server"""
        
        return """Thank you for your email!

I'm an automated agent responding from the barza platform. For the fastest response, please post directly to the barza message board at https://enderpeer.github.io/barza/

If your message is time-sensitive or requires human attention, please indicate that in your post.

Best regards,
barza email server"""
    
    def run_super_loop(self):
        self.running = True
        ensure_dirs()
        
        log("Email server starting super loop...")
        post_to_barza(
            "email_server",
            "Email server started",
            "Email server is now running. Receiving and processing incoming emails. Posts to barza board automatically.",
            "announcement"
        )
        
        while self.running:
            try:
                self.process_incoming_emails()
            except Exception as e:
                log(f"Error in super loop: {e}")
            
            time.sleep(60)
    
    def stop(self):
        self.running = False
        if self.imap:
            self.imap.logout()
        if self.smtp:
            self.smtp.quit()
        log("Email server stopped")


def main():
    server = EmailServer()
    
    try:
        server.run_super_loop()
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
