"""
Onboarding API Handler
Wraps the lighthorse onboarding API for tool calls
"""

import base64
import json
import logging
import os
import requests
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL_TEST = "https://test-lighthorse-trade.touzime.cn"
BASE_URL_PROD = "https://interface.lighthorse.io"

# Session file path
SESSION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".onboarding_sessions")
os.makedirs(SESSION_DIR, exist_ok=True)


class OnboardingSession:
    """Manages a single user's onboarding session"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.session_file = os.path.join(SESSION_DIR, f"{session_id}.json")
        self.userId = None
        self.token = None
        self.access_token = None
        self._udid = None
        self._load()
        if not self._udid:
            self._udid = str(uuid.uuid4())

    @property
    def base_url(self) -> str:
        return BASE_URL_PROD if os.getenv("ONBOARDING_ENV") == "production" else BASE_URL_TEST

    def _headers(self, extra: dict = None) -> dict:
        headers = {
            "Content-Type": "application/json",
            "accesskey": os.getenv("VERIFY_CODE_RISK_ACCESSKEY", ""),
            "fingerprint": self._udid,
            "x-auth-appname": "AINVEST_BROKER",
            "x-auth-progid": "8047"
        }
        if extra:
            headers.update(extra)
        return headers

    def _cookie(self) -> str:
        parts = []
        if self.userId:
            parts.append(f"userid={self.userId}")
        if self.token:
            parts.append(f"sessionid={self.token}")
        if self.access_token:
            parts.append(f"access_token={self.access_token}")
        return ";".join(parts) + ";"

    def _load(self):
        if os.path.exists(self.session_file):
            try:
                with open(self.session_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.userId = data.get("userId")
                self.token = data.get("token")
                self.access_token = data.get("access_token")
                self._udid = data.get("udid", self._udid)
            except (json.JSONDecodeError, IOError):
                pass

    def _save(self):
        data = {
            "userId": self.userId,
            "token": self.token,
            "access_token": self.access_token,
            "udid": self._udid
        }
        with open(self.session_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def set_from_cookies(self, cookies: dict):
        """Set credentials from frontend cookies (userid, sessionid, access_token)"""
        self.userId = cookies.get("userid") or cookies.get("userId") or self.userId
        self.token = cookies.get("sessionid") or cookies.get("session_id") or self.token
        self.access_token = cookies.get("access_token") or self.access_token
        if self.userId or self.token or self.access_token:
            logger.info(f"set_from_cookies: userId={self.userId}, token={'***' if self.token else None}, access_token={'***' if self.access_token else None}")
            self._save()

    def _is_auth_error(self, resp: requests.Response) -> bool:
        """Check if response indicates authentication error (401)"""
        if resp.status_code == 401:
            return True
        try:
            result = resp.json()
            if result.get("s") == "error" and "401" in str(result.get("code", "")):
                return True
        except json.JSONDecodeError:
            pass
        return False

    def _clear_auth(self):
        """Clear authentication credentials after 401"""
        logger.warning("_clear_auth: clearing credentials")
        self.userId = None
        self.token = None
        self.access_token = None
        self._save()

    def send_verification_code(self, contact: str, contact_type: str = "MOBILE", area_code: str = "1") -> dict:
        """Send verification code (SMS/Email)"""
        endpoint = f"{self.base_url}/auth/user/v3/sendMac"
        body = {"type": contact_type, "actionType": "NORMAL"}
        if contact_type == "MOBILE":
            body["mobile"] = contact
            body["areaCode"] = area_code
        else:
            body["email"] = contact
        resp = requests.post(endpoint, json=body, headers=self._headers())
        return resp.json()

    def login(self, contact: str, verification_code: str, contact_type: str = "MOBILE", area_code: str = "1") -> dict:
        """Login with verification code"""
        logger.info(f"login: contact={contact}, contact_type={contact_type}, area_code={area_code}")
        endpoint = f"{self.base_url}/auth/user/v3/login"
        body = {"loginType": "MOBILE_MAC" if contact_type == "MOBILE" else "EMAIL_MAC"}
        if contact_type == "MOBILE":
            body["mobile"] = contact
            body["areaCode"] = area_code
            body["authCode"] = verification_code
        else:
            body["email"] = contact
            body["authCode"] = verification_code
        extra_headers = {}
        if self.userId:
            extra_headers["userId"] = self.userId
        resp = requests.post(endpoint, json=body, headers=self._headers(extra_headers))
        result = resp.json()
        logger.info(f"login result: {result}")
        if result.get("data"):
            self.userId = str(result["data"].get("userId"))
            self.token = result["data"].get("token")
            self._save()
            logger.info(f"login saved: userId={self.userId}, token={self.token[:10] if self.token else None}...")
            # Return credentials to frontend
            result["userId"] = self.userId
            result["token"] = self.token
        return result

    def get_trading_token(self) -> dict:
        """Get trading access token"""
        if not self.userId or not self.token:
            logger.error("get_trading_token: userId or token is None")
            return {"s": "error", "errmsg": "Please login first"}
        logger.info(f"get_trading_token: userId={self.userId}, token={self.token[:10]}...")
        endpoint = f"{self.base_url}/auth/v1/token"
        headers = self._headers({"X-Auth-Udid": self._udid, "Cookie": self._cookie()})
        logger.info(f"get_trading_token: headers={headers}")
        params = {"user_id": self.userId, "session_id": self.token}
        resp = requests.post(endpoint, headers=headers, params=params)
        if self._is_auth_error(resp):
            logger.warning("get_trading_token: 401 received, clearing credentials")
            self._clear_auth()
            return {"s": "error", "errmsg": "Authentication expired, please login again", "auth_expired": True}
        result = resp.json()
        logger.info(f"get_trading_token result: {result}")
        if result.get("s") == "ok" and result.get("d"):
            self.access_token = result["d"].get("access_token")
            self._save()
            # Return access_token to frontend
            result["access_token"] = self.access_token
        return result

    def get_user_info(self) -> dict:
        """Get user info"""
        if not self.userId or not self.token:
            return {"s": "error", "errmsg": "Please login first"}
        endpoint = f"{self.base_url}/auth/user/v2/getUserInfo"
        headers = self._headers({"Cookie": self._cookie(), "userId": self.userId, "Token": self.token})
        resp = requests.post(endpoint, headers=headers)
        if self._is_auth_error(resp):
            logger.warning("get_user_info: 401 received, clearing credentials")
            self._clear_auth()
            return {"s": "error", "errmsg": "Authentication expired, please login again", "auth_expired": True}
        return resp.json()

    def update_email(self, email: str, auth_code: str) -> dict:
        """Update email"""
        if not self.userId or not self.token:
            return {"s": "error", "errmsg": "Please login first"}
        endpoint = f"{self.base_url}/api/oas/v1/application/email/update"
        body = {"auth_code": auth_code, "email": email}
        headers = self._headers({"Cookie": self._cookie(), "userId": self.userId, "Token": self.token})
        resp = requests.post(endpoint, json=body, headers=headers)
        if self._is_auth_error(resp):
            logger.warning("update_email: 401 received, clearing credentials")
            self._clear_auth()
            return {"s": "error", "errmsg": "Authentication expired, please login again", "auth_expired": True}
        logger.info(f"update_email: return={resp.json()}")
        return resp.json()

    def update_mobile(self, phone: str, area_code: str, auth_code: str) -> dict:
        """Update mobile"""
        if not self.userId or not self.token:
            return {"s": "error", "errmsg": "Please login first"}
        endpoint = f"{self.base_url}/api/oas/v1/application/phone/update"
        body = {"phone": phone, "areaCode": area_code, "auth_code": auth_code}
        headers = self._headers({"Cookie": self._cookie(), "userId": self.userId, "Token": self.token})
        resp = requests.post(endpoint, json=body, headers=headers)
        if self._is_auth_error(resp):
            logger.warning("update_mobile: 401 received, clearing credentials")
            self._clear_auth()
            return {"s": "error", "errmsg": "Authentication expired, please login again", "auth_expired": True}
        return resp.json()

    def query_progress(self) -> dict:
        """Query account opening progress"""
        logger.info(f"query_progress: userId={self.userId}, access_token={self.access_token[:10] if self.access_token else None}...")
        if not self.userId or not self.access_token:
            logger.error("query_progress: missing userId or access_token")
            return {"s": "error", "errmsg": "Please login and get trading token first"}
        endpoint = f"{self.base_url}/api/oas/v1/application/query"
        headers = self._headers({"Cookie": self._cookie()})
        logger.info(f"query_progress: headers={headers}")
        resp = requests.get(endpoint, headers=headers)
        if self._is_auth_error(resp):
            logger.warning("query_progress: 401 received, clearing credentials")
            self._clear_auth()
            return {"s": "error", "errmsg": "Authentication expired, please login again", "auth_expired": True}
        result = resp.json()
        logger.info(f"query_progress result: {result}")
        return resp.json()

    def collect_information(self, data: dict) -> dict:
        """Submit collected account opening information"""
        if not self.userId or not self.access_token:
            return {"s": "error", "errmsg": "Please login and get trading token first"}
        endpoint = f"{self.base_url}/api/oas/v1/application/collect"
        headers = self._headers({"Cookie": self._cookie(), "userId": self.userId, "Token": self.token, "fingerprint": self._udid})
        data["application_source"] = {"source":"oao_agent","packageName":"web","appVersion":"1.0.0"}
        resp = requests.post(endpoint, json=data, headers=headers)
        if self._is_auth_error(resp):
            logger.warning("collect_information: 401 received, clearing credentials")
            self._clear_auth()
            return {"s": "error", "errmsg": "Authentication expired, please login again", "auth_expired": True}
        return resp.json()

    def submit_application(self) -> dict:
        """Submit account opening application"""
        if not self.userId or not self.access_token:
            return {"s": "error", "errmsg": "Please login and get trading token first"}
        endpoint = f"{self.base_url}/api/oas/v1/application/submit"
        headers = self._headers({"Cookie": self._cookie(), "userId": self.userId, "Token": self.token})
        resp = requests.post(endpoint, headers=headers)
        if self._is_auth_error(resp):
            logger.warning("submit_application: 401 received, clearing credentials")
            self._clear_auth()
            return {"s": "error", "errmsg": "Authentication expired, please login again", "auth_expired": True}
        return resp.json()

    def get_agreement_file_list(self, account_type: str, open_crypto: int = 0) -> dict:
        """Get list of agreements and their URLs for the given account type"""
        if not self.userId or not self.access_token:
            return {"s": "error", "errmsg": "Please login and get trading token first"}
        endpoint = f"{self.base_url}/api/oas/v1/application/agreement/file/list"
        headers = self._headers({"Cookie": self._cookie(), "userId": self.userId, "Token": self.token})
        params = {"account_type": account_type, "open_crypto": open_crypto}
        resp = requests.get(endpoint, headers=headers, params=params)
        if self._is_auth_error(resp):
            logger.warning("get_agreement_file_list: 401 received, clearing credentials")
            self._clear_auth()
            return {"s": "error", "errmsg": "Authentication expired, please login again", "auth_expired": True}
        logger.info(f"get_agreement_file_list: account_type={account_type}, result={resp.json()}")
        return resp.json()

    def upload_file(self, file_data: str, filename: str, is_need_min: bool = False, file_type: str = None) -> dict:
        """Upload document file via collect endpoint (base64 encoded)"""
        if not self.userId or not self.access_token:
            return {"s": "error", "errmsg": "Please login and get trading token first"}
        logger.info(f"upload_file: filename={filename}, file_data length={len(file_data)}, file_type={file_type}")
        endpoint = f"{self.base_url}/api/oas/v1/application/file/collect"
        headers = self._headers({"Cookie": self._cookie(), "userId": self.userId, "Token": self.token})
        headers.pop("Content-Type", None)
        binary_data = base64.b64decode(file_data)
        files = {
            "file": (filename, binary_data, "application/octet-stream"),
            "is_need_min": (None, "1" if is_need_min else "0")
        }
        if file_type:
            files["file_type"] = (None, file_type)
        resp = requests.post(endpoint, files=files, headers=headers)
        if self._is_auth_error(resp):
            logger.warning("upload_file: 401 received, clearing credentials")
            self._clear_auth()
            return {"s": "error", "errmsg": "Authentication expired, please login again", "auth_expired": True}
        logger.info(f"upload_file response: {resp.json()}")
        return resp.json()

    def close(self):
        """Clean up session file"""
        if os.path.exists(self.session_file):
            os.remove(self.session_file)


_sessions = {}


def get_session(session_id: str, cookies: dict = None) -> OnboardingSession:
    if session_id not in _sessions:
        _sessions[session_id] = OnboardingSession(session_id)
    if cookies:
        _sessions[session_id].set_from_cookies(cookies)
    return _sessions[session_id]


def close_session(session_id: str):
    if session_id in _sessions:
        _sessions[session_id].close()
        del _sessions[session_id]