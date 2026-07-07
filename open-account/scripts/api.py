"""
Authentication Center & Trading Account Opening API Wrapper
Defaults to test environment, pass production=True to switch to production environment
"""

import requests # type: ignore
import uuid
import json
import os
import re
from typing import Optional

BASE_URL_TEST = "https://test-lighthorse-trade.touzime.cn"
BASE_URL_PROD = "https://interface.lighthorse.io"

# Session file path (stored in .session file in the same directory as the script)
_SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".session")
_SESSION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sessions")


def _float_env(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _request_timeout() -> tuple[float, float]:
    connect_timeout = _float_env("DIGITAL_ONBOARDING_REAL_BANK_CONNECT_TIMEOUT", 10.0)
    read_timeout = _float_env(
        "DIGITAL_ONBOARDING_REAL_BANK_READ_TIMEOUT",
        _float_env("DIGITAL_ONBOARDING_REAL_BANK_REQUEST_TIMEOUT", 60.0),
    )
    return connect_timeout, read_timeout


def _safe_session_id(session_id: str) -> str:
    """Return a filesystem-safe session id for per-trajectory token caches."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id.strip())
    if not safe:
        raise ValueError("session_id cannot be empty")
    return safe[:160]


class TradeAPI:
    # Fixed request headers
    FIXED_HEADERS = {
        "accesskey": "",
        "fingerprint": "open-account-skill",
        "x-auth-appname": "AINVEST_BROKER",
        "x-auth-progid": "8047"
    }

    def __init__(
        self,
        environment: str = "test",
        session_id: Optional[str] = None,
        session_dir: Optional[str] = None,
        session_path: Optional[str] = None,
    ):
        self.base_url = BASE_URL_PROD if environment == "production" else BASE_URL_TEST
        self.session_id = session_id
        self._session_file = self._resolve_session_path(
            session_id=session_id,
            session_dir=session_dir,
            session_path=session_path,
        )
        self._udid = str(uuid.uuid4())
        # Load authentication info from session file
        self._load_session()

    def _resolve_session_path(
        self,
        session_id: Optional[str],
        session_dir: Optional[str],
        session_path: Optional[str],
    ) -> str:
        """Resolve local token cache path.

        Default behavior is backward compatible and uses scripts/.session.
        For parallel training, pass a unique session_id per trajectory so local
        userId/token/access_token state cannot leak across samples.
        """
        if session_path and session_id:
            raise ValueError("Pass either session_path or session_id, not both")
        if session_path:
            return os.path.abspath(session_path)
        if session_id:
            root = os.path.abspath(session_dir or _SESSION_DIR)
            return os.path.join(root, f"{_safe_session_id(session_id)}.json")
        return _SESSION_FILE

    def _session_path(self) -> str:
        """Get session file path"""
        return self._session_file

    def _load_session(self) -> None:
        """Load session info from local file"""
        session_file = self._session_path()
        if os.path.exists(session_file):
            try:
                with open(session_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.userId = data.get("userId")
                self.token = data.get("token")
                self.access_token = data.get("access_token")
                self.fingerprint = data.get("fingerprint")
            except (json.JSONDecodeError, IOError):
                self.userId = None
                self.token = None
                self.access_token = None
                self.fingerprint = None
        else:
            self.userId = None
            self.token = None
            self.access_token = None
            self.fingerprint = None

    def _save_session(self) -> None:
        """Save session info to local file"""
        session_file = self._session_path()
        os.makedirs(os.path.dirname(session_file), exist_ok=True)
        data = {
            "userId": self.userId,
            "token": self.token,
            "access_token": self.access_token,
            "fingerprint": self.fingerprint
        }
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def clear_session(self) -> None:
        """Clear session info (call when logging out)"""
        self.userId = None
        self.token = None
        self.access_token = None
        self.fingerprint = None
        session_file = self._session_path()
        if os.path.exists(session_file):
            os.remove(session_file)

    def _headers(self, extra: dict = None) -> dict:
        headers = {
            "Content-Type": "application/json",
            **self.FIXED_HEADERS,
            "fingerprint": self.fingerprint or self.FIXED_HEADERS["fingerprint"]
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(self, method: str, endpoint: str, **kwargs):
        kwargs.setdefault("timeout", _request_timeout())
        return requests.request(method, endpoint, **kwargs)

    def _normalize_area_code(self, area_code: str) -> str:
        """Normalize area code by removing '+' prefix if present"""
        if area_code and area_code.startswith("+"):
            return area_code[1:]
        return area_code

    def _cookie(self) -> str:
        """Build Cookie request header"""
        parts = []
        if self.userId:
            parts.append(f"userid={self.userId}")
        if self.token:
            parts.append(f"sessionid={self.token}")
        if self.access_token:
            parts.append(f"access_token={self.access_token}")
        return ";".join(parts) + ";" if parts else ""

    # ========== Authentication Center APIs ==========

    def send_verification_code(self, contact: str, contact_type: str = "MOBILE",
                               area_code: str = "1") -> dict:
        """
        Send verification code (SMS/Email)

        Args:
            contact: mobile number or email
            contact_type: "MOBILE" or "EMAIL"
            area_code: country code, default "1"
        """
        endpoint = f"{self.base_url}/auth/user/v3/sendMac"

        body = {
            "type": contact_type,
            "actionType": "NORMAL"
        }

        if contact_type == "MOBILE":
            body["mobile"] = contact
            body["areaCode"] = self._normalize_area_code(area_code)
        else:
            body["email"] = contact

        resp = self._request("POST", endpoint, json=body, headers=self._headers())
        return resp.json()

    def login(self, contact: str, verification_code: str,
              contact_type: str = "MOBILE", area_code: str = "1") -> dict:
        """
        Login (mobile + verification code or email + verification code)

        Args:
            contact: mobile number or email
            verification_code: user input verification code
            contact_type: "MOBILE" or "EMAIL"
            area_code: country code, default "1"
        """
        endpoint = f"{self.base_url}/auth/user/v3/login"

        headers = self._headers()

        body = {
            "loginType": "MOBILE_MAC" if contact_type == "MOBILE" else "EMAIL_MAC"
        }

        if contact_type == "MOBILE":
            body["mobile"] = contact
            body["areaCode"] = self._normalize_area_code(area_code)
            body["authCode"] = verification_code
        else:
            body["email"] = contact
            body["authCode"] = verification_code

        resp = self._request("POST", endpoint, json=body, headers=headers)
        result = resp.json()

        if result.get("data"):
            self.userId = str(result["data"].get("userId"))
            self.token = result["data"].get("token")
            self.fingerprint = result["data"].get("fingerprint") or self.fingerprint
            self._save_session()

        return result

    def get_trading_token(self) -> dict:
        """
        Get trading access token (requires login first)
        """
        if not self.userId or not self.token:
            raise ValueError("Please call login() first")

        endpoint = f"{self.base_url}/auth/v1/token"

        headers = self._headers({
            "X-Auth-Udid": self.fingerprint or self._udid,
            "Cookie": self._cookie()
        })

        params = {
            "user_id": self.userId,
            "session_id": self.token
        }

        resp = self._request("POST", endpoint, headers=headers, params=params)
        result = resp.json()

        if result.get("s") == "ok" and result.get("d"):
            self.access_token = result["d"].get("access_token")
            self._save_session()

        return result

    def get_user_info(self) -> dict:
        """
        Get user info (requires login first)

        Returns:
            dict containing user information
        """
        if not self.userId or not self.token:
            raise ValueError("Please call login() first")

        endpoint = f"{self.base_url}/auth/user/v2/getUserInfo"

        headers = self._headers({
            "Cookie": self._cookie(),
            "userId": self.userId,
            "Token": self.token
        })

        resp = self._request("POST", endpoint, headers=headers)
        return resp.json()

    def update_email(self, email: str, auth_code: str) -> dict:
        """
        Update email (requires login first)

        Args:
            email: new email address
            auth_code: verification code
        """
        if not self.userId or not self.token:
            raise ValueError("Please call login() first")

        endpoint = f"{self.base_url}/auth/user/v2/updateEmail"

        body = {
            "authCode": auth_code,
            "email": email,
            "type": "BIND",
            "originSendType": "email"
        }

        headers = self._headers({
            "Cookie": self._cookie(),
            "userId": self.userId,
            "Token": self.token
        })

        resp = self._request("POST", endpoint, json=body, headers=headers)
        return resp.json()

    def update_mobile(self, phone: str, area_code: str, auth_code: str) -> dict:
        """
        Update mobile number (requires login first)

        Args:
            phone: new mobile number
            area_code: country code
            auth_code: verification code
        """
        if not self.userId or not self.token:
            raise ValueError("Please call login() first")

        endpoint = f"{self.base_url}/auth/user/v2/updateMobile"

        body = {
            "phone": phone,
            "areaCode": self._normalize_area_code(area_code),
            "authCode": auth_code,
            "type": "BIND",
            "originSendType": "mobile"
        }

        headers = self._headers({
            "Cookie": self._cookie(),
            "userId": self.userId,
            "Token": self.token
        })

        resp = self._request("POST", endpoint, json=body, headers=headers)
        return resp.json()

    # ========== Trading Account Opening APIs ==========

    def query_progress(self) -> dict:
        """
        Query account opening progress
        """
        if not self.userId or not self.access_token:
            raise ValueError("Please complete login and get trading token first")

        endpoint = f"{self.base_url}/api/oas/v1/application/query"

        headers = self._headers({
            "Cookie": self._cookie()
        })

        resp = self._request("GET", endpoint, headers=headers)
        return resp.json()

    def collect_information(self, data: dict) -> dict:
        """
        Submit collected account opening information (batch submission)

        Args:
            data: account opening information dict, should be structured according to the account collection API documentation
                  e.g. personal information, funding source, contact information, etc.
        """
        if not self.userId or not self.access_token:
            raise ValueError("Please complete login and get trading token first")

        endpoint = f"{self.base_url}/api/oas/v1/application/collect"

        headers = self._headers({
            "Cookie": self._cookie(),
            "userId": self.userId,
            "Token": self.token
        })

        resp = self._request("POST", endpoint, json=data, headers=headers)
        return resp.json()

    def submit_application(self) -> dict:
        """
        Submit account opening application
        """
        if not self.userId or not self.access_token:
            raise ValueError("Please complete login and get trading token first")

        endpoint = f"{self.base_url}/api/oas/v1/application/submit"

        headers = self._headers({
            "Cookie": self._cookie(),
            "userId": self.userId,
            "Token": self.token
        })

        resp = self._request("POST", endpoint, headers=headers)
        return resp.json()

    def upload_file(self, file_path: str, is_need_min: bool = False, file_type: Optional[str] = None) -> dict:
        """
        Account opening file upload

        Args:
            file_path: file path (supports absolute or relative path)
            is_need_min: whether to generate thumbnail, default False
            file_type: optional bank collection field, e.g. drivers_licence_front

        Returns:
            dict containing file_id and min_file_id
        """
        if not self.userId or not self.access_token:
            raise ValueError("Please complete login and get trading token first")

        endpoint = (
            f"{self.base_url}/api/oas/v1/application/file/collect"
            if file_type
            else f"{self.base_url}/api/oas/v1/application/file/upload"
        )

        headers = self._headers({
            "Cookie": self._cookie(),
            "userId": self.userId,
            "Token": self.token
        })

        headers.pop("Content-Type", None)  # Remove Content-Type to let requests auto-set multipart/form-data

        filename = os.path.basename(file_path)

        with open(file_path, "rb") as f:
            if file_type:
                files = {
                    "file_type": (None, file_type),
                    "front": (filename, f.read(), "application/octet-stream"),
                    "is_need_min": (None, 1 if is_need_min else 0)
                }
            else:
                files = {
                    "file": (filename, f.read(), "application/octet-stream"),
                    "is_need_min": (None, 1 if is_need_min else 0)
                }

            resp = self._request(
                "POST",
                endpoint,
                headers=headers,
                files=files
            )

        return resp.json()
