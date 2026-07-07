import asyncio
import json
import logging
import re
import time
import uuid
from typing import Optional

try:
    from pypinyin import lazy_pinyin, Style
    _HAS_PYPINYIN = True
except ImportError:
    _HAS_PYPINYIN = False

from onboarding_api import get_session

logger = logging.getLogger(__name__)

# Enum normalization maps — common case variations → canonical values
_ENUM_MAPS = {
    "account_type": {"CASH": "CASH", "MARGIN": "MARGIN", "cash": "CASH", "margin": "MARGIN", "Cash": "CASH", "Margin": "MARGIN"},
    "gender": {"MALE": "MALE", "FEMALE": "FEMALE", "OTHER": "OTHER", "male": "MALE", "female": "FEMALE", "other": "OTHER", "Male": "MALE", "Female": "FEMALE"},
    "marital_status": {"MARRIED": "MARRIED", "SINGLE": "SINGLE", "DIVORCED": "DIVORCED", "WIDOWED": "WIDOWED", "married": "MARRIED", "single": "SINGLE", "divorced": "DIVORCED", "widowed": "WIDOWED"},
    "employment_status": {"EMPLOYED": "EMPLOYED", "SELF_EMPLOYED": "SELF_EMPLOYED", "UNEMPLOYED": "UNEMPLOYED", "RETIRED": "RETIRED", "STUDENT": "STUDENT", "employed": "EMPLOYED", "self_employed": "SELF_EMPLOYED", "unemployed": "UNEMPLOYED", "retired": "RETIRED", "student": "STUDENT"},
    "funding_source": {"SAVINGS": "Savings", "INHERITANCE": "Inheritance", "PENSION": "Pension", "RENTAL INCOME": "Rental Income", "SOCIAL SECURITY": "Social Security", "OTHER": "Other", "Savings": "Savings", "Inheritance": "Inheritance", "Pension": "Pension", "Rental Income": "Rental Income", "Social Security": "Social Security", "Other": "Other", "savings": "Savings", "inheritance": "Inheritance", "other": "Other"},
    "investment_experience": {"EXTENSIVE": "EXTENSIVE", "GOOD": "GOOD", "LIMITED": "LIMITED", "NONE": "NONE", "extensive": "EXTENSIVE", "good": "GOOD", "limited": "LIMITED", "none": "NONE"},
    "risk_tolerance": {"HIGH": "HIGH", "MEDIUM": "MEDIUM", "LOW": "LOW", "high": "HIGH", "medium": "MEDIUM", "low": "LOW"},
    "is_control_person": {"true": True, "false": False, "True": True, "False": False, "1": True, "0": False},
    "is_affiliated_exchangeorfinra": {"true": True, "false": False, "True": True, "False": False, "1": True, "0": False},
    "is_politically_exposed": {"true": True, "false": False, "True": True, "False": False, "1": True, "0": False},
    "is_commodity": {"true": True, "false": False, "True": True, "False": False, "1": True, "0": False},
    "is_trade_authorization": {"true": True, "false": False, "True": True, "False": False, "1": True, "0": False},
    "is_identify": {"true": True, "false": False, "True": True, "False": False, "1": True, "0": False},
    "agreements_accepted": {"true": True, "false": False, "True": True, "False": False, "1": True, "0": False},
    "permanent_resident": {"true": True, "false": False, "True": True, "False": False, "1": True, "0": False},
}


def _normalize_field_value(field: str, value) -> tuple:
    """Normalize a single field value. Returns (field, normalized_value)."""
    if field in _ENUM_MAPS and isinstance(value, str):
        normalized = _ENUM_MAPS[field].get(value, value)
        if normalized != value:
            logger.info(f"Enum normalized: {field} '{value}' → '{normalized}'")
        return field, normalized
    return field, value


def _normalize_data(data: dict) -> dict:
    """Normalize all enum field values in collect_information data."""
    normalized = {}
    for field, value in data.items():
        _, normalized_value = _normalize_field_value(field, value)
        normalized[field] = normalized_value
    return normalized


# Backend file_type remapping.  The model and the frontend still use the
# business-meaningful name ('permanent_resident_card') so prompts, widgets and
# logs stay readable, but the backend stores green cards under the same
# 'id_card' slot it uses for Chinese national ID cards.  Apply this remap at
# every point where file_type is forwarded to the broker API.
_BACKEND_FILE_TYPE_REMAP = {
    "permanent_resident_card": "id_card",
}


def _remap_file_type_for_backend(file_type):
    if isinstance(file_type, str):
        mapped = _BACKEND_FILE_TYPE_REMAP.get(file_type)
        if mapped and mapped != file_type:
            logger.info(f"file_type remap (for backend): '{file_type}' → '{mapped}'")
            return mapped
    return file_type


class OnboardingToolHandler:
    """
    处理开户相关的 tool call
    直接调用 API，无需 Claude agent
    """

    def __init__(self):
        self.sessions = {}

    def create_session(self, session_id: str = None, branch: str = "DOMESTIC") -> str:
        if session_id is None:
            session_id = str(uuid.uuid4())
        # Normalize branch — accept lowercase or unknown values gracefully
        branch_norm = (branch or "DOMESTIC").upper()
        if branch_norm not in ("DOMESTIC", "FOREIGNER"):
            logger.warning(f"Unknown branch '{branch}', falling back to DOMESTIC")
            branch_norm = "DOMESTIC"
        self.sessions[session_id] = {
            "step": "initial",
            "branch": branch_norm,
            "data": {},
            "history": []
        }
        return session_id

    def get_session(self, session_id: str):
        if session_id not in self.sessions:
            self.create_session(session_id)
        return self.sessions[session_id]

    def get_session_branch(self, session_id: str) -> str:
        """Return the branch this session was created with (DOMESTIC / FOREIGNER).
        Defaults to DOMESTIC if the session does not exist or has no branch set."""
        return self.get_session(session_id).get("branch", "DOMESTIC")

    async def send_verification_code(self, contact: str, contact_type: str, area_code: str, session_id: str) -> dict:
        """发送验证码"""
        session = get_session(session_id)
        result = session.send_verification_code(contact, contact_type, area_code)
        return result

    async def login(self, contact: str, verification_code: str, contact_type: str, area_code: str, session_id: str) -> dict:
        """登录"""
        session = get_session(session_id)
        result = session.login(contact, verification_code, contact_type, area_code)

        if result.get("s") == "ok":
            self.get_session(session_id)["step"] = "logged_in"
            result["userId"] = session.userId
            result["token"] = session.token

        return result

    async def login_and_get_token(self, contact: str, verification_code: str, contact_type: str, area_code: str, session_id: str) -> dict:
        """登录并获取交易 token（合并版）"""
        logger.info(f"login_and_get_token called: contact={contact}, contact_type={contact_type}")

        session = get_session(session_id)

        # 1. Login
        login_result = session.login(contact, verification_code, contact_type, area_code)
        logger.info(f"Login result: {login_result}")
        # API returns {"i18nMsg": "success", "data": {...}} or {"s": "ok", ...}
        is_ok = login_result.get("s") == "ok" or login_result.get("i18nMsg") == "success"
        if not is_ok:
            return login_result

        # 2. Get trading token
        token_result = session.get_trading_token()
        logger.info(f"Token result: {token_result}")
        token_ok = token_result.get("s") == "ok" or token_result.get("i18nMsg") == "success"
        if not token_ok:
            return token_result

        self.get_session(session_id)["step"] = "token_obtained"

        # 3. Query progress to get account opening status
        progress_result = session.query_progress()
        logger.info(f"Progress result: {progress_result}")

        progress_ok = progress_result.get("s") == "ok" or progress_result.get("i18nMsg") == "success"
        if progress_ok:
            data = progress_result.get("d", {})
            status = data.get("status", "UNKNOWN")
            missing_fields = data.get("missing_fields", [])
            collected_fields = data.get("collected_fields", [])

            logger.info(f"Progress status={status}, missing={missing_fields}, collected={collected_fields}")

            self.get_session(session_id)["step"] = f"progress_{status}"
            self.get_session(session_id)["progress_data"] = data

            return {
                "s": "ok",
                "d": {
                    "message": "Login, token and progress obtained successfully",
                    "userId": session.userId,
                    "token": session.token,
                    "access_token": session.access_token,
                    "status": status,
                    "missing_fields": missing_fields,
                    "collected_fields": collected_fields,
                    "full_progress": data
                }
            }

        logger.warning(f"Progress query failed, returning basic result")
        return {
            "s": "ok",
            "d": {
                "message": "Login and trading token obtained successfully",
                "userId": session.userId
            }
        }

    async def get_trading_token(self, session_id: str) -> dict:
        """获取交易 token"""
        session = get_session(session_id)
        result = session.get_trading_token()

        if result.get("s") == "ok":
            self.get_session(session_id)["step"] = "token_obtained"

        return result

    async def get_user_info(self, session_id: str) -> dict:
        """获取用户信息"""
        session = get_session(session_id)
        return session.get_user_info()

    async def update_email(self, email: str, auth_code: str, session_id: str) -> dict:
        """更新邮箱"""
        session = get_session(session_id)
        return session.update_email(email, auth_code)

    async def update_mobile(self, phone: str, area_code: str, auth_code: str, session_id: str) -> dict:
        """更新手机号"""
        session = get_session(session_id)
        return session.update_mobile(phone, area_code, auth_code)

    async def query_progress(self, session_id: str) -> dict:
        """查询开户进度"""
        session = get_session(session_id)
        result = session.query_progress()

        if result.get("s") == "ok":
            data = result.get("d", {})
            status = data.get("status", "UNKNOWN")
            self.get_session(session_id)["step"] = f"progress_{status}"

        return result

    async def collect_information(self, data: dict, session_id: str) -> dict:
        """提交开户信息"""
        session = get_session(session_id)
        # Normalize enum case variations (e.g. "cash" → "CASH", "true" → True)
        normalized_data = _normalize_data(data)
        logger.info(f"collect_information: raw={json.dumps(data, ensure_ascii=False)[:300]} normalized={json.dumps(normalized_data, ensure_ascii=False)[:300]}")
        result = session.collect_information(normalized_data)

        if result.get("s") == "ok":
            self.get_session(session_id)["step"] = "information_collected"

        return result

    # ── New individual submit_* handlers (map to collect_information) ──

    async def submit_account_type(self, account_type: str,  session_id: str) -> dict:
        return await self.collect_information({"account_type": account_type, }, session_id)

    async def submit_personal_identity(self, session_id: str, **kwargs) -> dict:
        """Branch-aware identity submit.

        Domestic must carry SSN (no tax_id); Foreigner must carry tax_id (no SSN).
        """
        branch = self.get_session_branch(session_id)
        ssn = kwargs.get("social_security_number")
        tax_id = kwargs.get("tax_id")

        # For Chinese nationals: if no explicit tax_id but an ID card was OCR'd,
        # use the id_number as the tax_id (身份证号即税号).
        if not tax_id and branch == "FOREIGNER":
            sess = self.get_session(session_id)
            docs = sess.get("extracted_docs") or {}
            id_card = docs.get("ID_CARD") or {}
            id_fields = id_card.get("fields") or {}
            if id_fields.get("document_number"):
                tax_id = id_fields["document_number"]
                kwargs["tax_id"] = tax_id
                kwargs["tax_id_country"] = "CHN"
                logger.info(f"Using ID card number as tax_id: {tax_id}")

        if branch == "DOMESTIC":
            if not ssn:
                return {"s": "error", "errmsg": "social_security_number is required on DOMESTIC branch — re-present present_ssn_input."}
            kwargs.pop("tax_id", None)
            kwargs.pop("tax_id_country", None)
        else:  # FOREIGNER
            if not tax_id:
                return {"s": "error", "errmsg": "tax_id is required on FOREIGNER branch — re-present present_tax_id_input."}
            kwargs.pop("social_security_number", None)

        # Map to broker API field names
        payload = dict(kwargs)
        if payload.get("tax_id"):
            tax_id_country = payload.pop("tax_id_country", None) or ""
            payload["weight_form"] = {"tax_id": payload.pop("tax_id")}
            if tax_id_country:
                payload["weight_form"]["tax_id_country"] = tax_id_country
        return await self.collect_information(payload, session_id)

    async def submit_residency_status(self, session_id: str, **kwargs) -> dict:
        """Branch-aware residency. Foreigner coerces permanent_resident=False and
        skips the US-only rejection check entirely. Caches citizenship + PR on the
        session so submit_documents can compute the sub-branch later."""
        branch = self.get_session_branch(session_id)
        citizenship = (kwargs.get("citizenship_country") or "").upper()
        if branch == "FOREIGNER":
            kwargs["permanent_resident"] = False
        # Cache for downstream document matrix calculation
        sess = self.get_session(session_id)
        if citizenship:
            sess["citizenship_country"] = citizenship
        sess["permanent_resident"] = bool(kwargs.get("permanent_resident"))
        return await self.collect_information(kwargs, session_id)

    async def submit_home_address(self, session_id: str, **kwargs) -> dict:
        """Branch-aware address. Caller is expected to pass address_branch=US|INTERNATIONAL;
        if missing we infer from session branch so the broker still receives it."""
        if "address_branch" not in kwargs:
            kwargs["address_branch"] = "US" if self.get_session_branch(session_id) == "DOMESTIC" else "INTERNATIONAL"
        return await self.collect_information(kwargs, session_id)

    async def submit_employment(self, session_id: str, **kwargs) -> dict:
        return await self.collect_information(kwargs, session_id)

    async def submit_financial_profile(self, session_id: str, **kwargs) -> dict:
        return await self.collect_information(kwargs, session_id)

    async def submit_investment_profile(self, session_id: str, **kwargs) -> dict:
        return await self.collect_information(kwargs, session_id)

    async def submit_disclosures(self, session_id: str, **kwargs) -> dict:
        return await self.collect_information(kwargs, session_id)

    async def submit_documents(self, session_id: str, **kwargs) -> dict:
        """Forward uploaded-document metadata to the backend.

        This is a pure metadata pass-through: it takes the `documents` list the
        model assembled (file_type, filename, passport_number, expiration_date,
        id_number, issuing_country, ...) and POSTs it to
        `/api/oas/v1/application/collect` for the backend to validate.

        No citizenship / sub-branch precheck is done here on purpose — the
        backend accepts any combination of document fields regardless of
        nationality, and a session that reconnects mid-flow will have lost any
        cached citizenship. Keeping a local matrix in lock-step with the
        backend's rules just adds a stale gate.
        """
        sess = self.get_session(session_id)
        documents = kwargs.get("documents") or []

        # Remap file_type to the backend's storage names BEFORE forwarding
        # (e.g. permanent_resident_card → id_card). The original list is left
        # untouched in the session for any local bookkeeping.
        documents_for_backend = []
        for d in documents:
            if isinstance(d, dict) and "file_type" in d:
                d_copy = dict(d)
                d_copy["file_type"] = _remap_file_type_for_backend(d_copy["file_type"])
                documents_for_backend.append(d_copy)
            else:
                documents_for_backend.append(d)

        sess["documents"] = documents

        # Build collect_information payload — extract known fields to top level
        payload = {"documents": documents_for_backend}
        for d in documents:
            if not isinstance(d, dict):
                continue
            ft = d.get("file_type") or d.get("document_type") or ""
            if ft == "passport":
                if d.get("passport_number"):
                    payload["passport_no"] = d["passport_number"]
                if d.get("expiration_date"):
                    payload["passport_expire_date"] = d["expiration_date"]

        return await self.collect_information(payload, session_id)

    async def get_agreement_file_list(self, account_type: str, session_id: str) -> dict:
        """获取协议文件列表（供前端展示）"""
        session = get_session(session_id)
        return session.get_agreement_file_list(account_type)

    async def submit_agreements(self, agreements_accepted: bool, session_id: str) -> dict:
        return await self.collect_information({"agreements_accepted": agreements_accepted}, session_id)

    async def submit_application(self, session_id: str) -> dict:
        """提交开户申请"""
        session = get_session(session_id)
        result = session.submit_application()

        if result.get("s") == "ok":
            self.get_session(session_id)["step"] = "submitted"

        return result

    def upload_file(self, file_data: str, filename: str, is_need_min: bool, file_type: str = None, session_id: str = None) -> dict:
        """上传文件"""
        session = get_session(session_id)
        # Map business-level file_type to whatever slot the backend actually
        # stores (e.g. green card is stored under 'id_card').
        file_type = _remap_file_type_for_backend(file_type)
        return session.upload_file(file_data, filename, is_need_min, file_type)

    def _to_pinyin(self, text: str) -> str:
        """Convert Chinese characters to pinyin. Non-Chinese text passes through."""
        if not text or not _HAS_PYPINYIN:
            return text
        # Only convert if the string contains CJK characters
        if not re.search(r'[一-鿿]', text):
            return text
        parts = []
        for char in text:
            if re.match(r'[一-鿿]', char):
                py = lazy_pinyin(char, style=Style.NORMAL)
                # Capitalize first letter of each syllable
                parts.append(py[0].capitalize() if py else char)
            else:
                parts.append(char)
        return ''.join(parts)

    async def extract_document_info(self, document_type: str, extracted_fields: dict = None, document_image: str = None, session_id: str = None) -> dict:
        """从证件中提取信息并存储，用于后续自动填表

        存储结构: session["extracted_docs"] 是按 document_type 归类的 dict，
        每类只保留最近一次 OCR 结果（同类型重传时直接覆盖）。
        """
        if extracted_fields:
            # Convert Chinese names to pinyin for uniform downstream handling
            for key in ("given_name", "family_name", "full_name", "first_name", "last_name", "name", "address", "address_state", "address_city", "address_street1", "address_street2"):
                val = extracted_fields.get(key)
                if val and isinstance(val, str):
                    converted = self._to_pinyin(val)
                    if converted != val:
                        extracted_fields[key] = converted
                        logger.info(f"pinyin: {key} {val!r} → {converted!r}")

        if session_id and extracted_fields:
            session_data = self.get_session(session_id)
            docs = session_data.get("extracted_docs")
            if not isinstance(docs, dict):
                docs = {}
            docs[document_type] = {
                "fields": extracted_fields,
                "ts": int(time.time()),
            }
            session_data["extracted_docs"] = docs

        return {
            "s": "ok",
            "d": {
                "message": f"Document {document_type} extracted successfully",
                "extracted": extracted_fields
            }
        }

    async def close_session(self, session_id: str):
        """关闭 session"""
        if session_id in self.sessions:
            del self.sessions[session_id]
        from onboarding_api import close_session as api_close
        api_close(session_id)


_handler: Optional[OnboardingToolHandler] = None


def get_handler() -> OnboardingToolHandler:
    global _handler
    if _handler is None:
        _handler = OnboardingToolHandler()
    return _handler