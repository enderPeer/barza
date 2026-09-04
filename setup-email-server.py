#!/usr/bin/env python3
"""Setup script for barza email server."""
import json
import getpass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CONFIG_PATH = DATA_DIR / "email_config.json"

print("=" * 60)
print("Barza Email Server Setup")
print("=" * 60)
print()

print("This will configure the email server for sending/receiving emails.")
print("You'll need an email provider that supports SMTP/IMAP (e.g., Gmail with app password).")
print()

email = input("Email address: ").strip()
app_password = getpass.getpass("App password (or regular password): ")

config = {
    "email": email,
    "app_password": app_password,
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "imap_server": "imap.gmail.com",
    "imap_port": 993
}

with open(CONFIG_PATH, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2)

print()
print("=" * 60)
print("Configuration saved!")
print("=" * 60)
print(f"Config location: {CONFIG_PATH}")
print()
print("To start the email server:")
print("  powershell -ExecutionPolicy Bypass -File .\\run-email-server.bat")
print()
print("IMPORTANT: If using Gmail, you must:")
print("  1. Enable 2FA on your Google account")
print("  2. Create an App Password (not your regular password)")
print("  3. Allow IMAP/SMTP access in Gmail settings")
print()
