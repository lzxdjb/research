#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Trading Account Opening API Test Script

Usage:
  python test_api.py send_code <mobile> [country_code]
  python test_api.py login <mobile> <verification_code> [country_code]
  python test_api.py token
  python test_api.py query
  python test_api.py collect <name>
  python test_api.py submit
  python test_api.py upload <file_path> [need_thumbnail_true/false]
  python test_api.py all <mobile> [country_code] <verification_code> <name> <email> # Full flow
  example: python test_api.py all 2026041402 +1 123456 John john@gmail.com
  example: python test_api.py upload test.png false
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api import TradeAPI


def test_send_code(api, contact, area_code="86"):
    print("\n=== Send Verification Code ===")
    # Detect contact type
    if "@" in contact:
        contact_type = "EMAIL"
        result = api.send_verification_code(contact=contact, contact_type=contact_type)
    else:
        contact_type = "MOBILE"
        result = api.send_verification_code(contact=contact, contact_type=contact_type, area_code=area_code)
    print(result)
    return result.get("i18nMsg") == "success"


def test_login(api, contact, code, area_code="86"):
    print("\n=== Login ===")
    result = api.login(contact=contact, verification_code=code, contact_type="MOBILE", area_code=area_code)
    print(result)
    if result.get("s") == "ok":
        print(f"userId={api.userId}")
    return result.get("s") == "ok"

def test_get_user_info(api):
    print("\n=== Get User Info ===")
    result = api.get_user_info()
    print(result)
    return result.get("data")

def test_update_email(api, email, code):
    print("\n=== Update Email ===")
    result = api.update_email(email=email, auth_code=code)
    print(result)
    return result is not None


def test_token(api):
    print("\n=== Get Trading Token ===")
    result = api.get_trading_token()
    print(result)
    if api.access_token:
        print(f"access_token={api.access_token[:20]}...")
    return api.access_token is not None


def test_query(api):
    print("\n=== Query Account Opening Progress ===")
    result = api.query_progress()
    print(result)
    return True


def test_collect(api, name):
    print("\n=== Submit Collected Account Opening Info ===")
    data = {
        "give_name": name,
        "family_name": name
    }
    result = api.collect_information(data)
    print(result)
    return result.get("s") == "ok"


def test_submit(api):
    print("\n=== Submit Account Opening Application ===")
    result = api.submit_application()
    print(result)
    return result.get("s") == "ok"


def test_upload_file(api, file_path, is_need_min=False):
    print("\n=== Account Opening File Upload ===")
    result = api.upload_file(file_path=file_path, is_need_min=is_need_min)
    print(result)
    if result.get("s") == "ok" and result.get("d"):
        print(f"fileId={result['d'].get('fileId')}")
        print(f"minFileId={result['d'].get('minFileId')}")
    return result.get("s") == "ok"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1].lower()
    api = TradeAPI(environment="test")
    print(f"Environment: test, URL: {api.base_url}")

    if cmd == "send_code":
        contact = sys.argv[2] if len(sys.argv) > 2 else input("Mobile/Email: ").strip()
        if "@" not in contact:
            area = sys.argv[3] if len(sys.argv) > 3 else input("Country Code(+86): ").strip() or "+86"
            test_send_code(api, contact, area)
        else:
            test_send_code(api, contact)

    elif cmd == "login":
        contact = sys.argv[2] if len(sys.argv) > 2 else input("Mobile: ").strip()
        code = sys.argv[3] if len(sys.argv) > 3 else input("Verification Code: ").strip()
        area = sys.argv[4] if len(sys.argv) > 4 else input("Country Code(+86): ").strip() or "+86"
        test_login(api, contact, code, area)

    elif cmd == "token":
        test_token(api)

    elif cmd == "query":
        test_query(api)

    elif cmd == "collect":
        name = sys.argv[2] if len(sys.argv) > 2 else input("Name: ").strip()
        test_collect(api, name)

    elif cmd == "submit":
        test_submit(api)

    elif cmd == "upload":
        file_path = sys.argv[2] if len(sys.argv) > 2 else input("File Path: ").strip()
        is_need_min = sys.argv[3].lower() == "true" if len(sys.argv) > 3 else False
        test_upload_file(api, file_path, is_need_min)

    elif cmd == "update_email":
        email = sys.argv[2] if len(sys.argv) > 2 else input("Email: ").strip()
        code = sys.argv[3] if len(sys.argv) > 3 else input("Verification Code: ").strip()
        test_update_email(api, email, code)

    elif cmd == "all":
        contact = sys.argv[2] if len(sys.argv) > 2 else input("Mobile: ").strip()
        area = sys.argv[3] if len(sys.argv) > 3 else input("Country Code(+86): ").strip() or "+86"
        code = sys.argv[4] if len(sys.argv) > 4 else input("Verification Code: ").strip()
        name = sys.argv[5] if len(sys.argv) > 5 else input("Name: ").strip()
        email = sys.argv[6] if len(sys.argv) > 6 else input("Email: ").strip()

        test_send_code(api, contact, area)
        test_login(api, contact, code, area)
        user_info = test_get_user_info(api)
        if user_info and user_info.get("emailVerify") == 0:
            test_send_code(api, email)
            test_update_email(api, email, code)
        test_token(api)
        test_collect(api, name)
        test_query(api)
        test_submit(api)

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
