import asyncio
import base64
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import time
import uuid
from liveavatar_channel_sdk import AvatarAgent, AvatarAgentConfig, AgentListener, AudioFrame
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from gemini_live import GeminiLive
from onboarding_api import get_session
from tool_handlers import get_handler
from tool_schemas import get_all_tools

# Load environment variables
load_dotenv()

# Configure logging - add file handler to existing loggers
LOG_DIR = os.getenv("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
log_file = os.path.join(LOG_DIR, "app.log")

file_handler = logging.FileHandler(log_file, encoding="utf-8")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

# Add file handler to root logger (doesn't remove existing handlers)
logging.getLogger().addHandler(file_handler)
logging.getLogger().setLevel(logging.INFO)

# Module-specific levels
logging.getLogger("gemini_live").setLevel(logging.INFO)
logging.getLogger(__name__).setLevel(logging.INFO)
logger = logging.getLogger(__name__)
logger.info("Logging initialized, log file: %s", log_file)

# Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("MODEL", "gemini-3.1-flash-live-preview")

LIVEAVATAR_API_KEY = os.getenv("LIVEAVATAR_API_KEY", "")
LIVEAVATAR_AVATAR_ID = os.getenv("LIVEAVATAR_AVATAR_ID", "")
LIVEAVATAR_BASE_URL = os.getenv("LIVEAVATAR_BASE_URL", "https://facemarket.ai/vih/dispatcher")

class _AvatarListener(AgentListener):
    """Listener：记录所有平台事件，便于排查问题。"""

    def __init__(self, scene_ready_event: asyncio.Event, on_closed_cb=None):
        self._scene_ready = scene_ready_event
        self._on_closed_cb = on_closed_cb
        self._frame_count = 0

    async def on_session_init(self, session_id: str, user_id: str) -> None:
        logger.info("[avatar] session.init  session_id=%s  user_id=%s", session_id, user_id)
        asyncio.create_task(self._delayed_ready(5.0))

    async def _delayed_ready(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if not self._scene_ready.is_set():
            logger.info("[avatar] scene_ready (%.0fs timer after session.init)", delay)
            self._scene_ready.set()

    async def on_session_state(self, state) -> None:
        logger.info("[avatar] session.state  state=%s", state)
        if not self._scene_ready.is_set():
            logger.info("[avatar] scene_ready via session.state")
            self._scene_ready.set()

    async def on_session_closing(self, reason) -> None:
        logger.warning("[avatar] session.closing  reason=%s", reason)

    async def on_text_input(self, text: str, request_id: str) -> None:
        logger.info("[avatar] input.text  request_id=%s  text=%r", request_id, text[:200])

    async def on_audio_frame(self, frame) -> None:
        self._frame_count += 1
        if self._frame_count <= 3 or self._frame_count % 100 == 0:
            logger.info("[avatar] recv audio frame #%d  seq=%d  samples=%d  len=%d",
                        self._frame_count, frame.seq, frame.samples, len(frame.payload))

    async def on_idle_trigger(self, reason: str, idle_time_ms: int) -> None:
        logger.info("[avatar] idle_trigger  reason=%s  idle_ms=%d", reason, idle_time_ms)

    async def on_error(self, code: str, message: str) -> None:
        logger.error("[avatar] error  code=%s  msg=%s", code, message)

    async def on_closed(self, code: int, reason: str) -> None:
        logger.warning("[avatar] WS closed  code=%d  reason=%r  frame_count=%d",
                       code, reason, self._frame_count)
        if self._on_closed_cb:
            self._on_closed_cb()


async def _start_avatar_agent():
    scene_ready = asyncio.Event()
    listener = _AvatarListener(scene_ready)
    config = AvatarAgentConfig(
        api_key=LIVEAVATAR_API_KEY,
        avatar_id=LIVEAVATAR_AVATAR_ID,
        base_url=LIVEAVATAR_BASE_URL,
        developer_tts=True,
        developer_asr=False,
    )
    agent = AvatarAgent(config, listener)
    listener.agent = agent
    result = await agent.start()
    return agent, result, scene_ready


# Initialize FastAPI
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class CacheControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # Static asset cache strategy:
        #   - Requests carrying ?v=<hash> (rewritten by the index.html injector
        #     below) are content-addressed — safe to cache aggressively. The
        #     hash changes the URL whenever the file's bytes change, so the
        #     browser will refetch automatically on the next page load.
        #   - Requests without ?v= are direct hits (e.g. someone typing the
        #     URL, or an asset we forgot to fingerprint). Use no-cache so the
        #     browser still revalidates each time and never serves stale code.
        if request.url.path.startswith(("/static/", "/onboarding/static/")):
            if "v=" in (request.url.query or ""):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache"
        return response

app.add_middleware(CacheControlMiddleware)

# ─── Static-asset fingerprinting (cache busting) ─────────────────────────────
# At startup, compute an md5 of every JS / CSS file under frontend/ and inject
# a short prefix into the <script src="static/foo.js"> URLs in index.html as
# ?v=<hash>. Combined with the long-cache header above, this gives us:
#   - aggressive browser caching when the file hasn't changed (URL unchanged)
#   - automatic cache busting when the file's bytes change (URL changes)
# Computed once at process start — restart the service to pick up new bundles.
_ASSET_VERSIONS: dict[str, str] = {}
_INDEX_HTML_REWRITTEN: str = ""

def _build_asset_versions() -> None:
    frontend = Path("frontend")
    for path in list(frontend.glob("*.js")) + list(frontend.glob("*.css")):
        h = hashlib.md5(path.read_bytes()).hexdigest()
        _ASSET_VERSIONS[path.name] = h[:10]
    logger.info("Asset versions computed: %d files", len(_ASSET_VERSIONS))

def _rewrite_index_html() -> str:
    html = Path("frontend/index.html").read_text(encoding="utf-8")
    # Match  src="static/foo.js"  or  href="static/foo.css"  (no existing query
    # string). Tolerant of single/double quotes.
    pattern = re.compile(r'(src|href)=(["\'])(static/([^"\'?]+\.(?:js|css)))\2')

    def inject(m: re.Match) -> str:
        attr, quote, url, fname = m.group(1), m.group(2), m.group(3), m.group(4)
        version = _ASSET_VERSIONS.get(Path(fname).name)
        if not version:
            return m.group(0)
        return f'{attr}={quote}{url}?v={version}{quote}'

    return pattern.sub(inject, html)

_build_asset_versions()
_INDEX_HTML_REWRITTEN = _rewrite_index_html()
_LANDING_HTML = Path("frontend/landing.html").read_text(encoding="utf-8")

# The index.html itself MUST NOT be cached — its job is to deliver the
# current asset version numbers. If the browser pinned an old copy, users
# would keep loading stale JS bundles even after the service is restarted.
_INDEX_HEADERS = {"Cache-Control": "no-cache"}

# `/` is the marketing landing by default. The agent onboarding flow is gated
# behind ?agentMode=true so casual visitors don't drop into the WebSocket UI
# unprompted. Both /onboarding and /onboarding/ honour the same gate.
def _serve(agent_mode: str | None) -> HTMLResponse:
    if agent_mode == "true":
        return HTMLResponse(_INDEX_HTML_REWRITTEN, headers=_INDEX_HEADERS)
    return HTMLResponse(_LANDING_HTML, headers=_INDEX_HEADERS)

@app.get("/")
async def root(agentMode: str | None = None):
    return _serve(agentMode)

@app.get("/onboarding")
async def onboarding_root(agentMode: str | None = None):
    return _serve(agentMode)

@app.get("/onboarding/")
async def onboarding_root_trailing(agentMode: str | None = None):
    return _serve(agentMode)

app.mount("/static", StaticFiles(directory="frontend"), name="static")
app.mount("/onboarding/static", StaticFiles(directory="frontend"), name="static_onboarding")

handler = get_handler()


@app.websocket("/onboarding/ws")
async def websocket_endpoint_onboarding(websocket: WebSocket):
    await websocket_endpoint(websocket)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket connection accepted")

    # Branch is selected by the user on the homepage and passed in the WebSocket
    # query string (?branch=DOMESTIC or ?branch=FOREIGNER). Default to DOMESTIC
    # for backwards compatibility with older frontends that don't send it.
    raw_branch = websocket.query_params.get("branch", "DOMESTIC")
    branch = (raw_branch or "DOMESTIC").upper()
    if branch not in ("DOMESTIC", "FOREIGNER"):
        logger.warning(f"Unknown branch query param '{raw_branch}', falling back to DOMESTIC")
        branch = "DOMESTIC"

    session_id = handler.create_session(branch=branch)
    logger.info(f"Created session: {session_id} (branch={branch})")

    # --- LiveAvatar avatar startup ---
    avatar_state = {"ready": False, "agent": None, "scene_ready": None}

    async def _start_avatar_bg():
        try:
            avatar_agent, avatar_result, scene_ready_event = await _start_avatar_agent()
            avatar_state["ready"] = True
            avatar_state["agent"] = avatar_agent
            avatar_state["scene_ready"] = scene_ready_event
            avatar_agent._listener._on_closed_cb = lambda: avatar_state.update({"ready": False})
            await websocket.send_json({
                "type": "avatar_session",
                "user_token": avatar_result.user_token,
                "sfu_url": avatar_result.sfu_url,
            })
            logger.info("AvatarAgent started, sfu_url=%s", avatar_result.sfu_url)
        except Exception as e:
            logger.warning("AvatarAgent start failed (falling back to PCM): %s", e)

    asyncio.create_task(_start_avatar_bg())

    audio_input_queue = asyncio.Queue()
    video_input_queue = asyncio.Queue()
    text_input_queue = asyncio.Queue()
    classify_result_queue = asyncio.Queue()

    async def _inject_session_init(payload: dict):
        """Inject a structured session_init signal into the LLM input queue.

        The LLM is taught (in gemini_system_prompt_common.md → 'Session Init Signal')
        to recognize a JSON message of shape {"type": "session_init", "init_type": ...}
        as a system bootstrap message — NOT user speech — and branch behavior on
        `init_type`. Keeping this strictly structured (instead of free-form prose)
        means adding new bootstrap states later only requires defining a new
        `init_type` value, not rewriting prompt text.
        """
        envelope = {"type": "session_init", **payload, "branch": branch}
        text = json.dumps(envelope, ensure_ascii=False)
        logger.info(f"session_init injected: {text}")
        await text_input_queue.put(text)

    async def apply_cookies(cookies):
        """Apply cookies to the onboarding session and query current state.

        Emits exactly one `session_init` signal to the LLM, with `init_type` one of:
          - returning_logged_in  : cookies valid + query_progress ok → resume
          - auth_expired         : cookies present but query_progress rejected → re-login
          - returning_needs_login: only userId in cookies (no token) → recognized but not authed
          - new_user             : no cookies at all → fresh start
        """
        if cookies and (cookies.get("userid") or cookies.get("sessionid") or cookies.get("access_token")):
            from onboarding_api import get_session as api_get_session
            api_session = api_get_session(session_id, cookies)
            logger.info(f"Applied cookies: userid={cookies.get('userid')}, sessionid={'***' if cookies.get('sessionid') else None}, access_token={'***' if cookies.get('access_token') else None}")
            if api_session.token and api_session.access_token:
                progress_result = await asyncio.to_thread(api_session.query_progress)
                logger.info(f"Init cookies progress result: {progress_result}")
                if progress_result.get("s") == "ok":
                    data = progress_result.get("d", {})
                    status = data.get("status", "UNKNOWN")
                    missing_fields = data.get("missing_fields", [])
                    collected_fields = data.get("collected_fields", [])
                    sections = data.get("sections", [])
                    handler.get_session(session_id)["step"] = f"progress_{status}"
                    handler.get_session(session_id)["progress_data"] = data
                    completion_pct = data.get("completion_percentage", 0)
                    await websocket.send_json({
                        "type": "session_state",
                        "s": "ok",
                        "status": status,
                        "completion_percentage": completion_pct,
                        "missing_fields": missing_fields,
                        "collected_fields": collected_fields,
                        "userId": api_session.userId,
                        "access_token": api_session.access_token,
                    })
                    await _inject_session_init({
                        "init_type": "returning_logged_in",
                        "user_id": api_session.userId,
                        "status": status,
                        "percentage": completion_pct,
                        "missing_fields": missing_fields,
                        "collected_fields": collected_fields,
                        "sections": sections,
                    })
                    await asyncio.sleep(0.2)
                else:
                    await asyncio.to_thread(api_session._clear_auth)
                    await websocket.send_json({
                        "type": "session_state",
                        "s": "error",
                        "errmsg": "Session invalid or expired, please login",
                    })
                    await _inject_session_init({
                        "init_type": "auth_expired",
                        "errmsg": progress_result.get("errmsg") or "session invalid or expired",
                    })
                    await asyncio.sleep(0.2)
            else:
                userId = api_session.userId
                await websocket.send_json({
                    "type": "session_state",
                    "s": "ok",
                    "status": "NOT_APPLIED",
                    "completion_percentage": 0,
                    "missing_fields": [],
                    "collected_fields": [],
                    "userId": userId,
                })
                if userId:
                    await _inject_session_init({
                        "init_type": "returning_needs_login",
                        "user_id": userId,
                    })
                else:
                    await _inject_session_init({"init_type": "new_user"})
                await asyncio.sleep(0.2)
        else:
            # No cookies — fresh new user
            await websocket.send_json({
                "type": "session_state",
                "s": "ok",
                "status": "NOT_APPLIED",
                "completion_percentage": 0,
                "missing_fields": [],
                "collected_fields": [],
            })
            await _inject_session_init({"init_type": "new_user"})
            await asyncio.sleep(0.2)

    # Developer TTS: 把 Gemini PCM 拆成固定帧推给 avatar
    user_text_buf = []
    # is_prompt=True → promptStart/promptFinish (proactive, no requestId)
    # is_prompt=False → response.audio.start/finish (response to user input)
    av_turn = {"audio_started": False, "request_id": None, "response_id": None, "is_prompt": False}
    _had_user_input = False  # False = still in greeting phase; True = user has spoken

    # Developer TTS: Gemini 24kHz PCM → 960 samples/frame (40ms)
    _FRAME_SAMPLES = 960
    _FRAME_BYTES = _FRAME_SAMPLES * 2
    _audio_seq = 0
    _audio_ts = 0  # ms
    _audio_seq_sent = 0

    # Queue-based sender: raw PCM bytes, or None sentinel = end-of-turn
    _pcm_queue: asyncio.Queue = asyncio.Queue()
    _sender_task_ref: list = [None]

    def _audio_frame(n_samples: int, chunk: bytes) -> AudioFrame:
        return AudioFrame(
            channel=0,          # mono
            key=1 if _audio_seq == 0 else 0,
            seq=_audio_seq,
            timestamp=_audio_ts,
            sample_rate=1,      # 24kHz
            samples=n_samples,
            codec=0,            # PCM
            payload=chunk,
        )

    async def audio_output_callback(data, mime_type: str = ""):
        nonlocal _audio_seq, _audio_ts
        if not avatar_state["ready"]:
            await websocket.send_bytes(data)
            return
        agent = avatar_state["agent"]
        if not av_turn["audio_started"]:
            av_turn["audio_started"] = True
            _audio_seq = 0
            _audio_ts = 0
            if not _had_user_input:
                # Proactive / greeting — no requestId required
                av_turn["is_prompt"] = True
                av_turn["request_id"] = None
                av_turn["response_id"] = None
                try:
                    await agent.send_prompt_audio_start()
                    logger.info("[avatar] prompt_audio_start (greeting/proactive)")
                except Exception as e:
                    logger.warning("Avatar prompt_audio_start failed: %s", e)
            else:
                # Response to user input
                av_turn["is_prompt"] = False
                rid = f"req_{uuid.uuid4().hex[:8]}"
                rsid = f"res_{uuid.uuid4().hex[:8]}"
                av_turn["request_id"] = rid
                av_turn["response_id"] = rsid
                try:
                    await agent.send_response_audio_start(rid, rsid)
                    logger.info("[avatar] response.audio.start  rid=%s  rsid=%s", rid, rsid)
                except Exception as e:
                    logger.warning("Avatar audio_start failed: %s", e)
        await _pcm_queue.put(data)  # Enqueue — sender task handles pacing

    async def audio_interrupt_callback():
        pass

    async def handle_classify_document_type(file_type: str):
        await classify_result_queue.put(file_type)
        logger.info(f"classify_document_type tool called: file_type={file_type}")
        return json.dumps({"status": "ok", "file_type": file_type})

    # Create async handlers bound to session_id
    async def handle_send_verification_code(contact: str, contact_type: str, area_code: str = "1"):
        return await handler.send_verification_code(contact, contact_type, area_code, session_id)

    async def handle_login_and_get_token(contact: str, verification_code: str, contact_type: str, area_code: str = "1"):
        return await handler.login_and_get_token(contact, verification_code, contact_type, area_code, session_id)

    async def handle_get_user_info():
        return await handler.get_user_info(session_id)

    async def handle_update_email(email: str, auth_code: str):
        return await handler.update_email(email, auth_code, session_id)

    async def handle_update_mobile(phone: str, area_code: str, auth_code: str):
        return await handler.update_mobile(phone, area_code, auth_code, session_id)

    async def handle_query_progress():
        return await handler.query_progress(session_id)

    async def handle_collect_information(data: dict):
        return await handler.collect_information(data, session_id)

    async def handle_submit_application():
        return await handler.submit_application(session_id)

    async def handle_submit_account_type(account_type: str):
        return await handler.submit_account_type(account_type, session_id)

    async def handle_submit_personal_identity(**kwargs):
        return await handler.submit_personal_identity(session_id=session_id, **kwargs)

    async def handle_submit_residency_status(**kwargs):
        return await handler.submit_residency_status(session_id=session_id, **kwargs)

    async def handle_submit_home_address(**kwargs):
        return await handler.submit_home_address(session_id=session_id, **kwargs)

    async def handle_submit_employment(**kwargs):
        return await handler.submit_employment(session_id=session_id, **kwargs)

    async def handle_submit_financial_profile(**kwargs):
        return await handler.submit_financial_profile(session_id=session_id, **kwargs)

    async def handle_submit_investment_profile(**kwargs):
        return await handler.submit_investment_profile(session_id=session_id, **kwargs)

    async def handle_submit_disclosures(**kwargs):
        return await handler.submit_disclosures(session_id=session_id, **kwargs)

    async def handle_submit_documents(**kwargs):
        return await handler.submit_documents(session_id=session_id, **kwargs)

    async def handle_submit_agreements(agreements_accepted: bool):
        return await handler.submit_agreements(agreements_accepted, session_id)

    async def handle_upload_file(file_data: str, filename: str, is_need_min: bool = False, file_type: str = None):
        return await handler.upload_file(file_data, filename, is_need_min, file_type, session_id)

    async def handle_extract_document_info(document_type: str, extracted_fields: dict = None, document_image: str = None, **_ignored):
        return await handler.extract_document_info(document_type, extracted_fields, document_image, session_id)

    async def handle_present_agreements(question: str, account_type: str):
        return json.dumps({"status": "displayed", "type": "agreements"})

    async def handle_present_options(question: str, options: list, type: str, layout: str = None, field_key: str = None):
        return json.dumps({"status": "displayed", "type": "options", "field_key": field_key})

    async def handle_present_country_select(question: str, field_key: str, default_country: str = None):
        return json.dumps({"status": "displayed", "type": "country_select", "field_key": field_key})

    async def handle_present_date_input(question: str, format: str):
        return json.dumps({"status": "displayed", "type": "date_input"})

    async def handle_present_disclosure(questions: list):
        return json.dumps({"status": "displayed", "type": "disclosure"})

    async def handle_present_drivers_license_review(question: str, fields: dict):
        return json.dumps({"status": "displayed", "type": "drivers_license_review"})

    async def handle_present_personal_info_input(question: str, prefill: dict = None, address_prefill: dict = None):
        return json.dumps({"status": "displayed", "type": "personal_info"})

    async def handle_present_phone_input(question: str):
        return json.dumps({"status": "displayed", "type": "phone_input"})

    async def handle_present_email_input(question: str):
        return json.dumps({"status": "displayed", "type": "email_input"})

    async def handle_present_us_address_input(question: str, prefill: dict = None):
        # Legacy alias — same widget, defaults to mode='US'. Kept for backwards compatibility.
        return json.dumps({"status": "displayed", "type": "us_address_input"})

    async def handle_present_address_input(question: str, mode: str = "US", prefill: dict = None):
        return json.dumps({"status": "displayed", "type": "address_input", "mode": mode})

    async def handle_present_ssn_input(question: str):
        return json.dumps({"status": "displayed", "type": "ssn_input"})

    async def handle_present_tax_id_input(question: str, default_country: str = None):
        return json.dumps({"status": "displayed", "type": "tax_id_input"})

    async def handle_present_passport_input(question: str, fields: dict = None):
        return json.dumps({"status": "displayed", "type": "passport_input"})

    async def handle_present_visa_input(question: str):
        return json.dumps({"status": "displayed", "type": "visa_input"})

    async def handle_present_green_card_input(question: str, fields: dict = None):
        return json.dumps({"status": "displayed", "type": "green_card_input"})

    async def handle_present_id_card_input(question: str, fields: dict = None):
        return json.dumps({"status": "displayed", "type": "id_card_input"})

    async def handle_present_address_proof_upload(question: str, fields: dict = None):
        return json.dumps({"status": "displayed", "type": "address_proof_upload"})

    async def handle_present_financial_range_input(question: str, currency: str = "USD", buckets: list = None):
        return json.dumps({"status": "displayed", "type": "financial_range_input"})

    async def handle_present_investment_profile_input(question: str):
        return json.dumps({"status": "displayed", "type": "investment_profile_input"})

    async def handle_present_employment_input(question: str, prefill: dict = None):
        return json.dumps({"status": "displayed", "type": "employment_input"})

    async def handle_present_progress_indicator(percentage: int, sections: list, status: str, branch: str = None):
        return json.dumps({"status": "displayed", "type": "progress_indicator", "percentage": percentage, "status": status, "branch": branch})

    async def handle_capture_document(doc_type: str, purpose: str = None, **_ignored):
        logger.info(f"Tool call: capture_document | doc_type={doc_type}, purpose={purpose}")
        return "CAPTURE_REQUESTED"

    tool_mapping = {
        "send_verification_code": handle_send_verification_code,
        "login_and_get_token": handle_login_and_get_token,
        "get_user_info": handle_get_user_info,
        "update_email": handle_update_email,
        "update_mobile": handle_update_mobile,
        "query_progress": handle_query_progress,
        "collect_information": handle_collect_information,
        "submit_application": handle_submit_application,
        "submit_account_type": handle_submit_account_type,
        "submit_personal_identity": handle_submit_personal_identity,
        "submit_residency_status": handle_submit_residency_status,
        "submit_home_address": handle_submit_home_address,
        "submit_employment": handle_submit_employment,
        "submit_financial_profile": handle_submit_financial_profile,
        "submit_investment_profile": handle_submit_investment_profile,
        "submit_disclosures": handle_submit_disclosures,
        "submit_documents": handle_submit_documents,
        "submit_agreements": handle_submit_agreements,
        "upload_file": handle_upload_file,
        "extract_document_info": handle_extract_document_info,
        "present_agreements": handle_present_agreements,
        "present_options": handle_present_options,
        "present_country_select": handle_present_country_select,
        "present_date_input": handle_present_date_input,
        "present_disclosure": handle_present_disclosure,
        "present_drivers_license_review": handle_present_drivers_license_review,
        "present_personal_info_input": handle_present_personal_info_input,
        "present_phone_input": handle_present_phone_input,
        "present_email_input": handle_present_email_input,
        "present_us_address_input": handle_present_us_address_input,
        "present_address_input": handle_present_address_input,
        "present_ssn_input": handle_present_ssn_input,
        "present_tax_id_input": handle_present_tax_id_input,
        "present_passport_input": handle_present_passport_input,
        "present_visa_input": handle_present_visa_input,
        "present_green_card_input": handle_present_green_card_input,
        "present_id_card_input": handle_present_id_card_input,
        "present_address_proof_upload": handle_present_address_proof_upload,
        "present_financial_range_input": handle_present_financial_range_input,
        "present_investment_profile_input": handle_present_investment_profile_input,
        "present_employment_input": handle_present_employment_input,
        "present_progress_indicator": handle_present_progress_indicator,
        "capture_document": handle_capture_document,
        "classify_document_type": handle_classify_document_type,
    }

    gemini_client = GeminiLive(
        api_key=GEMINI_API_KEY,
        model=MODEL,
        input_sample_rate=16000,
        tools=get_all_tools(),
        tool_mapping=tool_mapping,
        branch=branch,
    )

    async def receive_from_client():
        try:
            while True:
                message = await websocket.receive()

                if message.get("bytes"):
                    await audio_input_queue.put(message["bytes"])
                elif message.get("text"):
                    text = message["text"]
                    try:
                        payload = json.loads(text)
                        if isinstance(payload, dict) and payload.get("type") == "init":
                            await apply_cookies(payload["cookies"])
                            continue
                        elif isinstance(payload, dict) and payload.get("type") == "image":
                            image_data = base64.b64decode(payload["data"])
                            await video_input_queue.put(image_data)
                            continue
                        elif isinstance(payload, dict) and payload.get("type") == "classify_file":
                            file_data = payload.get("data", "")
                            filename = payload.get("filename", "upload.jpg")
                            while not classify_result_queue.empty():
                                classify_result_queue.get_nowait()
                            image_bytes = base64.b64decode(file_data)
                            await video_input_queue.put(image_bytes)
                            classify_prompt = (
                                f"The user just uploaded a document file: '{filename}'. "
                                "Examine the image and call classify_document_type with the appropriate type. "
                                "Then briefly acknowledge what you see and tell the user "
                                "'File is being uploaded and processed, please wait...' — "
                                "do NOT call extract_document_info or present any widget yet. "
                                "The frontend will confirm when the upload is complete."
                            )
                            await text_input_queue.put(classify_prompt)
                            try:
                                file_type = await asyncio.wait_for(classify_result_queue.get(), timeout=20.0)
                            except asyncio.TimeoutError:
                                logger.warning(f"classify_document_type not called within timeout for {filename}, using fallback")
                                file_type = "id_card"
                            logger.info(f"classify_file: filename={filename}, file_type={file_type}")
                            await websocket.send_json({"type": "classify_file_result", "file_type": file_type})
                            continue
                        elif isinstance(payload, dict) and payload.get("type") == "upload_file":
                            # Fire-and-forget: spawn a background task so the WS receive
                            # loop stays responsive.  Result is pushed back asynchronously.
                            file_data = payload.get("data", "")
                            filename = payload.get("filename", "upload.jpg")
                            is_need_min = payload.get("is_need_min", False)
                            file_type = payload.get("file_type")
                            async def _do_upload():
                                try:
                                    result = await asyncio.to_thread(
                                        handler.upload_file, file_data, filename, is_need_min, file_type, session_id
                                    )
                                    await websocket.send_json({"type": "upload_file_result", "result": result})
                                except Exception as _e:
                                    logger.error(f"Upload background task failed: {_e}")
                                    try:
                                        await websocket.send_json({"type": "upload_file_result", "result": {"s": "error", "errmsg": str(_e)}})
                                    except Exception:
                                        pass
                            asyncio.create_task(_do_upload())
                            continue
                    except json.JSONDecodeError:
                        pass

                    await text_input_queue.put(text)
        except WebSocketDisconnect:
            logger.info("WebSocket disconnected")
        except Exception as e:
            logger.error(f"Error receiving from client: {e}")

    receive_task = asyncio.create_task(receive_from_client())

    async def run_session():
        def _av_reset():
            nonlocal _audio_seq, _audio_ts, _audio_seq_sent
            av_turn["audio_started"] = False
            av_turn["request_id"] = None
            av_turn["response_id"] = None
            av_turn["is_prompt"] = False
            user_text_buf.clear()
            _audio_seq = 0
            _audio_ts = 0
            _audio_seq_sent = 0
            # Drain stale PCM so the sender task doesn't replay old audio
            while not _pcm_queue.empty():
                try:
                    _pcm_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

        async def _avatar_frame_sender():
            """Background task: reads PCM from queue, packs into 40ms frames, sends at real-time rate.
            None sentinel = end-of-turn → flush remainder + send audio_finish.

            Uses a monotonic clock to enforce ≥40ms between frames regardless of whether
            the next frame comes from the same queue item or the next one.
            """
            nonlocal _audio_seq, _audio_ts, _audio_seq_sent
            buf = bytearray()
            next_frame_at = 0.0  # asyncio monotonic time when next frame may be sent
            loop = asyncio.get_event_loop()

            async def _send_frame(chunk: bytes) -> None:
                """Send one frame, respecting the rate limit."""
                nonlocal next_frame_at, _audio_seq, _audio_ts, _audio_seq_sent
                now = loop.time()
                if next_frame_at > now:
                    await asyncio.sleep(next_frame_at - now)
                n_samples = len(chunk) // 2
                agent = avatar_state.get("agent")
                if not agent or not avatar_state["ready"]:
                    return
                try:
                    await agent.send_audio_frame(_audio_frame(n_samples, chunk))
                    _audio_seq_sent += 1
                    if _audio_seq_sent <= 3 or _audio_seq_sent % 50 == 0:
                        logger.info("[avatar] audio frame #%d  seq=%d  samples=%d",
                                    _audio_seq_sent, _audio_seq, n_samples)
                except Exception as e:
                    logger.warning("[avatar] send_audio_frame #%d failed: %s",
                                   _audio_seq_sent + 1, e)
                _audio_seq += 1
                _audio_ts = (_audio_ts + 40) & 0xFFFFF
                next_frame_at = loop.time() + 0.04

            try:
                while True:
                    item = await _pcm_queue.get()
                    if item is None:
                        # End-of-turn: flush partial frame then send finish
                        agent = avatar_state.get("agent")
                        if agent and avatar_state["ready"] and av_turn["audio_started"]:
                            if buf:
                                chunk = bytes(buf)
                                buf.clear()
                                await _send_frame(chunk)
                            try:
                                if av_turn["is_prompt"]:
                                    await agent.send_prompt_audio_finish()
                                    logger.info("[avatar] prompt_audio_finish  frames=%d", _audio_seq_sent)
                                else:
                                    await agent.send_response_audio_finish(
                                        av_turn["request_id"], av_turn["response_id"]
                                    )
                                    logger.info("[avatar] response_audio_finish  rid=%s  frames=%d",
                                                av_turn["request_id"], _audio_seq_sent)
                            except Exception as e:
                                logger.warning("[avatar] audio_finish failed: %s", e)
                        _av_reset()
                        buf.clear()
                        next_frame_at = 0.0
                    else:
                        buf.extend(item)
                        while len(buf) >= _FRAME_BYTES:
                            chunk = bytes(buf[:_FRAME_BYTES])
                            del buf[:_FRAME_BYTES]
                            await _send_frame(chunk)
            except asyncio.CancelledError:
                pass

        # Start background frame sender (runs for the duration of the session)
        _sender_task_ref[0] = asyncio.create_task(_avatar_frame_sender())

        try:
            # 等 avatar scene 就绪再启动 Gemini，避免音频积压后瞬间喷发
            scene_ready = avatar_state.get("scene_ready")
            if scene_ready:
                await asyncio.wait_for(scene_ready.wait(), timeout=30.0)
                logger.info("[avatar] scene ready, starting Gemini session")

            async for event in gemini_client.start_session(
                audio_input_queue=audio_input_queue,
                video_input_queue=video_input_queue,
                text_input_queue=text_input_queue,
                audio_output_callback=audio_output_callback,
                audio_interrupt_callback=audio_interrupt_callback,
            ):
                if event:
                    logger.info(f"run_session event: {json.dumps(event, ensure_ascii=False)[:200]}")
                    try:
                        if event.get("type") == "user" and event.get("text"):
                            nonlocal _had_user_input
                            _had_user_input = True
                            user_text_buf.append(event["text"])

                        elif event.get("type") == "turn_complete":
                            if avatar_state["ready"] and av_turn["audio_started"]:
                                # Sentinel tells sender task to flush remainder + send audio_finish
                                await _pcm_queue.put(None)
                            else:
                                _av_reset()

                        elif event.get("type") == "interrupted":
                            if avatar_state["ready"]:
                                # Drain queue before interrupt so stale frames aren't sent
                                while not _pcm_queue.empty():
                                    try:
                                        _pcm_queue.get_nowait()
                                    except asyncio.QueueEmpty:
                                        break
                                try:
                                    await avatar_state["agent"].send_interrupt()
                                    logger.info("[avatar] interrupt sent")
                                except Exception as e:
                                    logger.warning("Avatar interrupt failed: %s", e)
                            _av_reset()

                        if event.get("type") == "tool_call" and event.get("name") == "present_options":
                            args = event.get("args", {})
                            widget_type = args.get("type", "single")
                            if args.get("layout") == "checklist":
                                widget_type = "checklist"
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": widget_type,
                                "question": args.get("question", ""),
                                "options": args.get("options", []),
                                "field_key": args.get("field_key"),
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_country_select":
                            args = event.get("args", {})
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "country_select",
                                "question": args.get("question", ""),
                                "field_key": args.get("field_key") or "",
                                "default_country": args.get("default_country") or "",
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_date_input":
                            args = event.get("args", {})
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "date",
                                "question": args.get("question", ""),
                                "format": args.get("format", "date"),
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_disclosure":
                            args = event.get("args", {})
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "disclosure",
                                "questions": args.get("questions", []),
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_drivers_license_review":
                            args = event.get("args", {})
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "drivers_license",
                                "question": args.get("question", ""),
                                "fields": args.get("fields", {}),
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_personal_info_input":
                            args = event.get("args", {})
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "personal_info",
                                "question": args.get("question", ""),
                                "prefill": args.get("prefill") or {},
                                "address_prefill": args.get("address_prefill") or {},
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_phone_input":
                            args = event.get("args", {})
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "phone",
                                "question": args.get("question", ""),
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_email_input":
                            args = event.get("args", {})
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "email",
                                "question": args.get("question", ""),
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_us_address_input":
                            args = event.get("args", {})
                            # Legacy alias — emit as the new address widget with mode='US'.
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "address",
                                "mode": "US",
                                "question": args.get("question", ""),
                                "prefill": args.get("prefill") or {},
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_address_input":
                            args = event.get("args", {})
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "address",
                                "mode": (args.get("mode") or "US").upper(),
                                "question": args.get("question", ""),
                                "prefill": args.get("prefill") or {},
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_tax_id_input":
                            args = event.get("args", {})
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "tax_id",
                                "question": args.get("question", ""),
                                "default_country": args.get("default_country") or "",
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_passport_input":
                            args = event.get("args", {})
                            fields = dict(args.get("fields") or {})
                            sess = handler.get_session(session_id)
                            docs = (sess.get("extracted_docs") or {}) if sess else {}
                            passport_ocr = (docs.get("PASSPORT") or {}).get("fields") or {}
                            if passport_ocr.get("document_number") and not fields.get("passport_number"):
                                fields["passport_number"] = passport_ocr["document_number"]
                            if passport_ocr.get("expiry_date") and not fields.get("expiration_date"):
                                fields["expiration_date"] = passport_ocr["expiry_date"]
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "passport",
                                "question": args.get("question", ""),
                                "fields": fields,
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_visa_input":
                            args = event.get("args", {})
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "visa",
                                "question": args.get("question", ""),
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_green_card_input":
                            args = event.get("args", {})
                            fields = dict(args.get("fields") or {})
                            sess = handler.get_session(session_id)
                            docs = (sess.get("extracted_docs") or {}) if sess else {}
                            gc_ocr = (docs.get("PERMANENT_RESIDENT_CARD") or {}).get("fields") or {}
                            if gc_ocr.get("document_number") and not fields.get("card_number"):
                                fields["card_number"] = gc_ocr["document_number"]
                            if gc_ocr.get("expiry_date") and not fields.get("expiration_date"):
                                fields["expiration_date"] = gc_ocr["expiry_date"]
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "green_card",
                                "question": args.get("question", ""),
                                "fields": fields,
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_id_card_input":
                            args = event.get("args", {})
                            fields = dict(args.get("fields") or {})
                            sess = handler.get_session(session_id)
                            docs = (sess.get("extracted_docs") or {}) if sess else {}
                            id_ocr = (docs.get("ID_CARD") or {}).get("fields") or {}
                            for k in ("document_number", "given_name", "family_name", "full_name",
                                      "date_of_birth", "gender", "address",
                                      "address_state", "address_city", "address_street1", "address_street2"):
                                if id_ocr.get(k) and not fields.get(k):
                                    fields[k] = id_ocr[k]
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "id_card",
                                "question": args.get("question", ""),
                                "fields": fields,
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_address_proof_upload":
                            args = event.get("args", {})
                            # Merge model-provided fields with any stored OCR results
                            fields = dict(args.get("fields") or {})
                            sess = handler.get_session(session_id)
                            docs = (sess.get("extracted_docs") or {}) if sess else {}
                            for doc_type, entry in docs.items():
                                if isinstance(entry, dict):
                                    ocr = entry.get("fields") or {}
                                    if ocr.get("full_name") and not fields.get("name_on_doc"):
                                        fields["name_on_doc"] = ocr["full_name"]
                                    if ocr.get("address") and not fields.get("address_on_doc"):
                                        fields["address_on_doc"] = ocr["address"]
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "address_proof_upload",
                                "question": args.get("question", ""),
                                "fields": fields,
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_financial_range_input":
                            args = event.get("args", {})
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "financial_range",
                                "question": args.get("question", ""),
                                "field_key": args.get("field_key") or "",
                                "currency": args.get("currency") or "USD",
                                "buckets": args.get("buckets") or [],
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_investment_profile_input":
                            args = event.get("args", {})
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "investment_profile",
                                "question": args.get("question", ""),
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_ssn_input":
                            args = event.get("args", {})
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "ssn",
                                "question": args.get("question", ""),
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_employment_input":
                            args = event.get("args", {})
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "employment",
                                "question": args.get("question", ""),
                                "prefill": args.get("prefill") or {},
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_agreements":
                            args = event.get("args", {})
                            account_type = args.get("account_type", "CASH")
                            api_session = get_session(session_id)
                            agreements_result = api_session.get_agreement_file_list(account_type)
                            agreements = agreements_result.get("d", []) if agreements_result.get("s") == "ok" else []
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "agreement",
                                "question": args.get("question", ""),
                                "agreements": agreements,
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "present_progress_indicator":
                            args = event.get("args", {})
                            await websocket.send_json({
                                "type": "widget",
                                "widget_type": "progress_indicator",
                                "percentage": args.get("percentage", 0),
                                "sections": args.get("sections", []),
                                "status": args.get("status", "NOT_STARTED"),
                                # Fall back to the session's branch so the frontend
                                # always has a definitive value even if the LLM omits it.
                                "branch": args.get("branch") or branch,
                            })
                        elif event.get("type") == "tool_call" and event.get("name") == "capture_document":
                            args = event.get("args", {})
                            logger.info(f"Tool call: capture_document | args: {json.dumps(args, ensure_ascii=False)}")
                            await websocket.send_json({
                                "type": "capture_document",
                                "doc_type": args.get("doc_type", "document"),
                                "purpose": args.get("purpose", "ocr"),
                            })
                        else:
                            await websocket.send_json(event)
                            if event.get("type") == "tool_call" and event.get("name") == "query_progress":
                                try:
                                    result_str = event.get("result", "")
                                    result = json.loads(result_str) if isinstance(result_str, str) else result_str
                                    if result.get("s") == "ok":
                                        data = result.get("d", {})
                                        pct = data.get("completion_percentage", 0)
                                        status = data.get("status", "COLLECTING")
                                        sections = data.get("sections", [])
                                        await websocket.send_json({
                                            "type": "widget",
                                            "widget_type": "progress_indicator",
                                            "percentage": pct,
                                            "sections": sections,
                                            "status": status,
                                        })
                                except Exception:
                                    pass
                    except Exception as e:
                        logger.warning(f"WebSocket send failed: {e}")
                        break
        except Exception as e:
            import traceback
            logger.error(f"Error in Gemini session: {type(e).__name__}: {e}\n{traceback.format_exc()}")

    try:
        await run_session()
    except Exception as e:
        import traceback
        logger.error(f"Error in Gemini session: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    finally:
        receive_task.cancel()
        if _sender_task_ref[0] is not None:
            _sender_task_ref[0].cancel()
        try:
            await asyncio.wait_for(receive_task, timeout=3.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        if _sender_task_ref[0] is not None:
            try:
                await asyncio.wait_for(_sender_task_ref[0], timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        await handler.close_session(session_id)
        if avatar_state["agent"]:
            try:
                await avatar_state["agent"].stop()
            except Exception:
                pass
        try:
            await asyncio.wait_for(websocket.close(), timeout=2.0)
        except Exception:
            pass
        logger.info(f"Session {session_id} cleanup done, receive_task cancelled")


# ─── Contact Verification API ────────────────────────────────────────────
@app.post("/api/contact/send_code")
async def send_contact_code(request: dict):
    contact = request.get("contact", "").strip()
    contact_type = request.get("contact_type", "EMAIL")
    area_code = request.get("area_code", "1")
    if not contact:
        return {"s": "error", "message": "Contact is required"}
    try:
        result = await handler.send_verification_code(contact, contact_type, area_code)
        return result
    except Exception as e:
        logger.error(f"send_code error: {e}")
        return {"s": "error", "message": str(e)}


@app.post("/api/contact/verify_code")
async def verify_contact_code(request: dict):
    contact = request.get("contact", "").strip()
    contact_type = request.get("contact_type", "EMAIL")
    code = request.get("code", "").strip()
    area_code = request.get("area_code", "1")
    if not contact or not code:
        return {"s": "error", "message": "Contact and code are required"}
    try:
        result = await handler.login(contact, code, contact_type, area_code)
        return result
    except Exception as e:
        logger.error(f"verify_code error: {e}")
        return {"s": "error", "message": str(e)}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
