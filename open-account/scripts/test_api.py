#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Trading Account Opening API Test Script

Usage:
  python test_api.py [--session-id TRAJECTORY_ID] send_code <mobile> [country_code]
  python test_api.py [--session-id TRAJECTORY_ID] login <mobile> <verification_code> [country_code]
  python test_api.py [--session-id TRAJECTORY_ID] token
  python test_api.py [--session-id TRAJECTORY_ID] query
  python test_api.py [--session-id TRAJECTORY_ID] collect <name>
  python test_api.py [--session-id TRAJECTORY_ID] submit
  python test_api.py [--session-id TRAJECTORY_ID] upload <file_path> [need_thumbnail_true/false]
  python test_api.py [--session-id TRAJECTORY_ID] all <mobile> [country_code] <verification_code> <name> <email> # Full flow
  example: python test_api.py all 2026041402 +1 123456 John john@gmail.com
  example: python test_api.py --session-id traj_0001 query
  example: python test_api.py upload test.png false
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api import TradeAPI


def parse_args(argv):
    session_id = os.environ.get("OPEN_ACCOUNT_SESSION_ID")
    args = list(argv)
    if "--session-id" in args:
        index = args.index("--session-id")
        try:
            session_id = args[index + 1]
        except IndexError:
            raise SystemExit("--session-id requires a value")
        del args[index : index + 2]
    return session_id, args


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
    contact_type = "EMAIL" if "@" in contact else "MOBILE"
    result = api.login(contact=contact, verification_code=code, contact_type=contact_type, area_code=area_code)
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
        "given_name": name,
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
    session_id, args = parse_args(sys.argv[1:])

    if len(args) < 1:
        print(__doc__)
        return

    cmd = args[0].lower()
    api = TradeAPI(environment="test", session_id=session_id)
    print(f"Environment: test, URL: {api.base_url}")
    print(f"Session file: {api._session_path()}")

    if cmd == "send_code":
        contact = args[1] if len(args) > 1 else input("Mobile/Email: ").strip()
        if "@" not in contact:
            area = args[2] if len(args) > 2 else input("Country Code(+86): ").strip() or "+86"
            test_send_code(api, contact, area)
        else:
            test_send_code(api, contact)

    elif cmd == "login":
        contact = args[1] if len(args) > 1 else input("Mobile/Email: ").strip()
        code = args[2] if len(args) > 2 else input("Verification Code: ").strip()
        area = args[3] if len(args) > 3 else input("Country Code(+86): ").strip() or "+86"
        test_login(api, contact, code, area)

    elif cmd == "token":
        test_token(api)

    elif cmd == "query":
        test_query(api)

    elif cmd == "collect":
        name = args[1] if len(args) > 1 else input("Name: ").strip()
        test_collect(api, name)

    elif cmd == "submit":
        test_submit(api)

    elif cmd == "upload":
        file_path = args[1] if len(args) > 1 else input("File Path: ").strip()
        is_need_min = args[2].lower() == "true" if len(args) > 2 else False
        test_upload_file(api, file_path, is_need_min)

    elif cmd == "update_email":
        email = args[1] if len(args) > 1 else input("Email: ").strip()
        code = args[2] if len(args) > 2 else input("Verification Code: ").strip()
        test_update_email(api, email, code)

    elif cmd == "all":
        contact = args[1] if len(args) > 1 else input("Mobile: ").strip()
        area = args[2] if len(args) > 2 else input("Country Code(+86): ").strip() or "+86"
        code = args[3] if len(args) > 3 else input("Verification Code: ").strip()
        name = args[4] if len(args) > 4 else input("Name: ").strip()
        email = args[5] if len(args) > 5 else input("Email: ").strip()

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
