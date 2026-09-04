#!/usr/bin/env python3
"""Quick test for email server functionality."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from email_server import EmailServer, post_to_barza

def test():
    print("Testing email server...")
    
    server = EmailServer()
    
    print("1. Testing barza connection...")
    result = post_to_barza(
        "email_test",
        "Email server test",
        "Testing email server connectivity...",
        "alert"
    )
    
    if result:
        print(f"   ✓ Barza connection OK (seq {result['messages'][0]['seq']})")
    else:
        print("   ✗ Barza connection failed")
        return False
    
    print("2. Testing email config...")
    config = server.config
    if config.get("email") and config.get("app_password"):
        print(f"   ✓ Config loaded for {config['email']}")
    else:
        print("   ✗ No email config found. Run setup-email-server.py first.")
        return False
    
    print("3. Testing SMTP connection...")
    if server.connect_smtp():
        print("   ✓ SMTP connected")
        server.smtp.quit()
    else:
        print("   ✗ SMTP connection failed")
        return False
    
    print("4. Testing IMAP connection...")
    if server.connect_imap():
        print("   ✓ IMAP connected")
        server.imap.logout()
    else:
        print("   ✗ IMAP connection failed")
        return False
    
    print()
    print("=" * 60)
    print("All tests passed! Email server is ready.")
    print("=" * 60)
    print()
    print("Start the server with:")
    print("  powershell -ExecutionPolicy Bypass -File .\\run-email-server.bat")
    print()
    
    return True

if __name__ == "__main__":
    test()
