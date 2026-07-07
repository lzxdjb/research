// --- Main Application Logic ---
// Shared tracking helpers (UTMTracker, getCookie, setCookie, eraseCookie,
// ensureVisitorId, trackEvent, BASE_URL, UTM_LOG_MAP, getOrCreateFingerprint)
// are defined in static/tracking.js, loaded earlier in the page. The
// `lhsbdw_cot_accountOpenningAgent` page-exposure event also fires from there.

const statusDiv = document.getElementById("status");
const branchSection = document.getElementById("branch-section");
const authSection = document.getElementById("auth-section");

// Onboarding branch — set when the user picks Domestic or Foreigner on the
// branch selection screen, then sent to the backend as a WebSocket query param.
let selectedBranch = null;
const appSection = document.getElementById("app-section");
const sessionEndSection = document.getElementById("session-end-section");
const restartBtn = document.getElementById("restartBtn");
const micBtn = document.getElementById("micBtn");
const cameraBtn = document.getElementById("cameraBtn");
const disconnectBtn = document.getElementById("disconnectBtn");
const textInput = document.getElementById("textInput");
const sendBtn = document.getElementById("sendBtn");
const videoPreview = document.getElementById("video-preview");
const videoPlaceholder = document.getElementById("video-placeholder");
const connectBtn = document.getElementById("connectBtn");
const branchDomesticBtn = document.getElementById("branchDomesticBtn");
const branchForeignerBtn = document.getElementById("branchForeignerBtn");
const branchCardDomestic = document.getElementById("branch-card-domestic");
const branchCardForeigner = document.getElementById("branch-card-foreigner");
const chatLog = document.getElementById("chat-log");
const stopBtn = document.getElementById("stopBtn");
const fileInput = document.getElementById("fileInput");
const attachBtn = document.querySelector(".attach-btn");

let currentGeminiMessageDiv = null;
let currentUserMessageDiv = null;
let thinkingIndicatorDiv = null;
let isReconnecting = false;
let isCapturingDocument = false;
let lastWidgetQuestion = null;
let lastWidgetTime = 0;
let lastWidgetMsgDiv = null;
let capturedDocumentFrame = null;

const mediaHandler = new MediaHandler();
const geminiClient = new GeminiClient({
  onDisconnect: () => {
    // Session ended by user — show session end screen
    isReconnecting = false;
    statusDiv.textContent = "Disconnected";
    statusDiv.className = "status disconnected";
    _showSessionEndForReal();
  },
  onOpen: async () => {
    isReconnecting = false;
    statusDiv.textContent = "Connected";
    statusDiv.className = "status connected";
    disconnectBtn.classList.remove("hidden");
    branchSection.classList.add("hidden");
    authSection.classList.add("hidden");
    sessionEndSection.classList.add("hidden");
    appSection.classList.remove("hidden");

    // 16054: 聊天框_界面展示
    trackEvent("lhsbdw_cot_accountOpenningAgent_agentChat_pageShow", "show");

    // Progress will be updated when session_state message arrives via handleJsonMessage
    // Do NOT initialize progress here — for returning users, session_state already has the correct status

    // Auto-start microphone and introduction only on first connect, not on reconnect
    if (!geminiClient._wasEverConnected) {
      try {
        await mediaHandler.startAudio((data) => {
          if (geminiClient.isConnected()) {
            geminiClient.send(data);
          }
        });
        micBtn.classList.add("active");
        micBtn.title = "Stop Microphone";
      } catch (e) {
        console.warn("Could not auto-start microphone:", e);
      }

      // Wait for session_state (this resolves after backend injects context into model)
      const sessionState = await geminiClient.waitForSessionState();
      // Show thinking indicator while model processes the context and responds
      showThinking();
      if (sessionState && sessionState.s === "ok") {
        // Session is valid — progress will be updated by handleJsonMessage via session_state
        // Nothing to do here; the session_state message is already on its way to update UI
      } else {
        // No valid session — send normal introduction
        geminiClient.sendText(
          "Introduce yourself as the Light Horse Securities onboarding assistant. And ask the user to sign up first.",
        );
        showThinking();
      }
    }
    geminiClient._wasEverConnected = true;
  },
  onMessage: (event) => {
    if (typeof event.data === "string") {
      try {
        const msg = JSON.parse(event.data);
        handleJsonMessage(msg);
      } catch (e) {
        console.error("Parse error:", e);
      }
    } else {
      mediaHandler.playAudio(event.data);
    }
  },
  onClose: (e) => {
    // Set flag FIRST — prevents race with onOpen on fast reconnect
    isReconnecting = true;
    console.log("WS onClose fired, isReconnecting:", isReconnecting);
    statusDiv.textContent = "Disconnected";
    statusDiv.className = "status disconnected";
  },
  onReconnecting: (e) => {
    statusDiv.textContent = `Reconnecting (${e.retryCount}/${e.maxRetries})...`;
    statusDiv.className = "status connecting";
  },
  onError: (e) => {
    if (e && e.type === "reconnect_failed") {
      console.error("WS reconnection failed");
      statusDiv.textContent = "Connection Lost";
      statusDiv.className = "status error";
      _showSessionEndForReal();
    } else {
      console.error("WS Error:", e);
      statusDiv.textContent = "Connection Error";
      statusDiv.className = "status error";
    }
  },
});

function setResponding(active) {
  sendBtn.classList.toggle("hidden", active);
  stopBtn.classList.toggle("hidden", !active);
}

function showThinking() {
  if (thinkingIndicatorDiv) return;
  thinkingIndicatorDiv = document.createElement("div");
  thinkingIndicatorDiv.className = "thinking-indicator";
  thinkingIndicatorDiv.innerHTML = `
    <div class="thinking-dots">
      <div class="thinking-dot"></div>
      <div class="thinking-dot"></div>
      <div class="thinking-dot"></div>
    </div>
  `;
  chatLog.appendChild(thinkingIndicatorDiv);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function removeThinking() {
  if (!thinkingIndicatorDiv) return;
  thinkingIndicatorDiv.remove();
  thinkingIndicatorDiv = null;
}

async function handleJsonMessage(msg) {
  if (msg.type === "avatar_session") {
    _connectAvatar(msg.sfu_url, msg.user_token);
    return;
  }

  // Handle session_state returned after init cookies verification
  if (msg.type === "session_state") {
    if (msg.s === "ok") {
      // Check if user changed — reset UI for new user
      const storedUserId = getCookie("userid");
      if (storedUserId && msg.userId && storedUserId !== msg.userId) {
        console.log(
          "[auth] User changed, resetting UI for new user:",
          msg.userId,
        );
        resetUI();
      }
      if (msg.userId) setCookie("userid", msg.userId, 30);
      console.log(
        "[auth] Valid session restored:",
        msg.status,
        "completion:",
        msg.completion_percentage,
        "%",
      );
      // Update progress display
      const appContainer = document.getElementById("progress-steps");
      if (appContainer) {
        updateProgressIndicator(
          { status: msg.status, percentage: msg.completion_percentage || 0 },
          appContainer,
        );
      }
    } else {
      console.warn("[auth] Session invalid:", msg.errmsg);
    }
    return;
  }

  // Log incoming auth-related messages for debugging
  if (
    msg.type === "tool_call" &&
    (msg.name === "login_and_get_token" ||
      msg.name === "login" ||
      msg.name === "get_trading_token")
  ) {
    console.log(
      "[auth] Received auth message:",
      JSON.stringify(msg).substring(0, 300),
    );
  }

  // Save credentials from tool_call result (login_and_get_token)
  if (
    msg.type === "tool_call" &&
    msg.name === "login_and_get_token" &&
    msg.result &&
    msg.result.d &&
    msg.result.d.userId
  ) {
    const d = msg.result.d;
    if (d.userId) setCookie("userid", d.userId, 30);
    if (d.token) setCookie("sessionid", d.token, 30);
    if (d.access_token) setCookie("access_token", d.access_token, 30);
    console.log("[auth] Credentials saved from tool_call result");
  }
  // Save credentials from tool_call result (login)
  if (
    msg.type === "tool_call" &&
    msg.name === "login" &&
    msg.result &&
    msg.result.userId
  ) {
    if (msg.result.userId) setCookie("userid", msg.result.userId, 30);
    if (msg.result.token) setCookie("sessionid", msg.result.token, 30);
    console.log("[auth] Credentials saved from login tool_call result");
  }
  // Save credentials from tool_call result (get_trading_token)
  if (
    msg.type === "tool_call" &&
    msg.name === "get_trading_token" &&
    msg.result &&
    msg.result.access_token
  ) {
    setCookie("access_token", msg.result.access_token, 30);
    console.log("[auth] Credentials saved from get_trading_token result");
  }
  // Handle auth_expired — clear credentials and prompt re-login
  const authExpired =
    msg.auth_expired === true ||
    (msg.d && msg.d.auth_expired === true) ||
    (msg.result && msg.result.d && msg.result.d.auth_expired === true);
  if (authExpired) {
    console.warn("[auth] Session expired, clearing credentials");
    eraseCookie("userid");
    eraseCookie("sessionid");
    eraseCookie("access_token");
    appendMessage(
      "gemini",
      "Your session has expired. Please provide your phone number or email to log in again.",
    );
    return;
  }

  if (msg.type === "interrupted") {
    removeThinking();
    mediaHandler.stopAudioPlayback();
    currentGeminiMessageDiv = null;
    currentUserMessageDiv = null;
    setResponding(false);
  } else if (msg.type === "turn_complete") {
    removeThinking();
    currentGeminiMessageDiv = null;
    currentUserMessageDiv = null;
    setResponding(false);
  } else if (msg.type === "user") {
    removeThinking();
    if (currentUserMessageDiv) {
      currentUserMessageDiv.textContent += msg.text;
      chatLog.scrollTop = chatLog.scrollHeight;
    } else {
      currentUserMessageDiv = appendMessage("user", msg.text);
    }
  } else if (msg.type === "gemini") {
    if (currentGeminiMessageDiv) {
      currentGeminiMessageDiv.textContent += msg.text;
      chatLog.scrollTop = chatLog.scrollHeight;
    } else {
      removeThinking();
      currentGeminiMessageDiv = appendMessage("gemini", msg.text);
      setResponding(true);
    }
  } else if (msg.type === "widget") {
    appendWidget(msg);
  } else if (msg.type === "capture_document") {
    if (isCapturingDocument) {
      console.log("[capture_document] already capturing, ignoring duplicate");
      return;
    }
    isCapturingDocument = true;
    const docType = msg.doc_type || "document";
    const purpose = msg.purpose || "ocr";
    console.log(
      "[capture_document] received, docType:",
      docType,
      "purpose:",
      purpose,
    );
    // Verify camera is active
    if (!mediaHandler.lastSentFrame && !mediaHandler.videoStream) {
      geminiClient.sendText(
        "CAMERA_UNAVAILABLE: The user's camera is not active. Please remind them to turn on the camera before retrying — do NOT proceed to collect document information verbally.",
      );
      isCapturingDocument = false;
      return;
    }

    if (purpose === "upload") {
      // Upload mode: capture frame, upload to server, show preview card in chat
      const videoEl = document.getElementById("video-preview");
      let base64Data = mediaHandler.lastSentFrame;
      let previewUrl = null;

      if (base64Data) {
        previewUrl = `data:image/png;base64,${base64Data}`;
      } else {
        const blob = mediaHandler.captureFrameAsBlob(videoEl);
        if (!blob) {
          geminiClient.sendText(
            "CAMERA_UNAVAILABLE: The user's camera is not active. Please remind them to turn on the camera before retrying — do NOT proceed to collect document information verbally.",
          );
          isCapturingDocument = false;
          return;
        }
        base64Data = await mediaHandler.blobToBase64(blob);
        previewUrl = URL.createObjectURL(blob);
      }

      const labelMap = {
        drivers_license_front: "Driver's License Front",
        drivers_license_back: "Driver's License Back",
        passport: "Passport",
        government_issued_id: "Government ID",
      };
      const label = labelMap[docType] || docType;
      const approxSize = Math.round(base64Data.length * 0.75);
      const fileCard = appendFileMessage(`${label}.jpg`, approxSize, true);
      if (previewUrl) {
        updateFilePreview(fileCard, previewUrl, true);
      }

      const FILE_TYPE_MAP = {
        drivers_license_front: "drivers_licence_front",
        drivers_license_back: "drivers_licence_back",
        passport: "passport",
        government_issued_id: "id_card",
      };
      const fileType = FILE_TYPE_MAP[docType] || "id_card";
      try {
        console.log(
          "[capture_document] uploading via WS, file_type:",
          fileType,
        );
        await geminiClient.uploadFile(
          base64Data,
          `${docType}.jpg`,
          true,
          fileType,
        );
        console.log("[capture_document] upload success");
        geminiClient.sendText(
          `CAPTURE_RESULT: ${docType} | Uploaded with preview`,
        );
      } catch (e) {
        console.error("[capture_document] upload failed:", e);
        geminiClient.sendText(
          "Document capture and upload failed. Please try again.",
        );
      }
    } else {
      // OCR mode (default): verify camera, no upload. Gemini OCRs from video stream.
      console.log(
        "[capture_document] frame ready for OCR, docType:",
        docType,
        mediaHandler.lastSentFrame
          ? "(lastSentFrame available)"
          : "(video stream active)",
      );
      geminiClient.sendText(`CAPTURE_RESULT: ${docType} | Frame ready for OCR`);
    }
    isCapturingDocument = false;
  } else if (msg.type === "tool_call") {
    // Log tool calls to console for debugging
    console.log("Tool call event:", msg);
    const toolMsg = appendMessage("system", `[Tool: ${msg.name}]`);
    toolMsg.style.color = "#ff6600";
    toolMsg.style.fontStyle = "italic";
  }
}

function appendMessage(type, text) {
  const msgDiv = document.createElement("div");
  msgDiv.className = `message ${type}`;
  msgDiv.textContent = text;
  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;
  return msgDiv;
}

function updateProgressIndicator(data, container) {
  if (!container) return;
  const { percentage = 0, status = "NOT_STARTED" } = data;

  // 4 status rows
  const steps = [
    { label: "Not Started", status_key: "NOT_STARTED" },
    { label: "Collecting", status_key: "COLLECTING" },
    { label: "Submitted", status_key: "SUBMITTED" },
    { label: "Approved", status_key: "APPROVED" },
  ];

  // Backend status mapping: NOT_APPLIED->NOT_STARTED, OPENED->APPROVED, others->SUBMITTED
  const DISPLAY_STATUS = {
    NOT_STARTED: "NOT_STARTED",
    COLLECTING: "COLLECTING",
    SUBMITTED: "SUBMITTED",
    APPROVED: "APPROVED",
    NOT_APPLIED: "NOT_STARTED",
    OPENED: "APPROVED",
  };
  const mappedStatus = DISPLAY_STATUS[status] || "SUBMITTED";

  const statusOrder = ["NOT_STARTED", "COLLECTING", "SUBMITTED", "APPROVED"];
  // Before login (NOT_STARTED) keep all circles gray; -1 means no step is active yet
  const currentIndex =
    mappedStatus === "NOT_STARTED" ? -1 : statusOrder.indexOf(mappedStatus);

  let html = "";
  steps.forEach((step, i) => {
    const stepIndex = statusOrder.indexOf(step.status_key);
    let s;
    if (stepIndex < currentIndex) {
      s = "complete";
    } else if (stepIndex === currentIndex) {
      s = "in_progress";
    } else {
      s = "pending";
    }

    const isLast = i === steps.length - 1;
    const circleClass =
      s === "complete" ? "completed" : s === "in_progress" ? "active" : "";
    const nameClass = s === "in_progress" ? "active" : "";
    const connActive = s === "complete" || s === "in_progress" ? "active" : "";
    const connectorClass = `step-connector ${connActive}`;
    const circleContent = s === "complete" ? "✓" : String(i + 1);
    const desc =
      s === "in_progress" && mappedStatus === "COLLECTING" && percentage > 0
        ? ` <span class="step-name ${nameClass}">${percentage}%</span>`
        : "";
    const labelHtml = `<div class="step-name ${nameClass}">${step.label}${desc}</div>`;

    html += `
      <div class="step-item">
        <div class="step-circle ${circleClass}">${circleContent}</div>
        ${!isLast ? `<div class="${connectorClass}"></div>` : ""}
        <div class="step-info">
          ${labelHtml}
        </div>
      </div>`;
  });

  container.innerHTML = html;
}

function appendWidget(data) {
  // data.widget_type is the actual widget type (single/multi/date/checklist/disclosure)
  // data.type is always "widget" (the websocket message type)
  const actualType = data.widget_type || data.type;
  const question = data.question || "";
  console.log("[Widget] Received:", actualType, question);

  // Dedup: replace existing widget of the same single-instance type
  const SINGLE_INSTANCE_SELECTORS = {
    financial_range: ".widget-financial-card",
    investment_profile: ".widget-ip-card",
    disclosure: ".widget-disclosure-card",
    employment: ".widget-employment-card",
  };
  if (actualType in SINGLE_INSTANCE_SELECTORS) {
    const existing = document.querySelector(
      SINGLE_INSTANCE_SELECTORS[actualType],
    );
    if (existing) {
      const existingMsg = existing.closest(".widget-message");
      if (existingMsg) {
        console.log("[Widget] Replacing existing:", actualType);
        existingMsg.remove();
      }
    }
  }

  // Dedup document confirmation widgets — passport, id_card, address_proof_upload
  const DOC_CONFIRM_TYPES = ["passport", "id_card", "address_proof_upload"];
  if (DOC_CONFIRM_TYPES.includes(actualType)) {
    const existing = document.querySelector(".widget-doc-card");
    if (existing) {
      const existingMsg = existing.closest(".widget-message");
      if (existingMsg) {
        console.log("[Widget] Replacing existing doc widget:", actualType);
        existingMsg.remove();
      }
    }
  }

  // Dedup / OCR update: if same question + type within 3 seconds
  const now = Date.now();
  const dedupKey = actualType + "|" + question;
  const hasFields = data.fields && Object.keys(data.fields).length > 0;
  if (dedupKey === lastWidgetQuestion && now - lastWidgetTime < 3000) {
    if (hasFields && lastWidgetMsgDiv && lastWidgetMsgDiv._widgetUpdateFields) {
      console.log(
        "[Widget] OCR prefill update (in-place):",
        actualType,
        question,
      );
      lastWidgetMsgDiv._widgetUpdateFields(data.fields);
      lastWidgetTime = now;
      return;
    }
    console.log("[Widget] Duplicate widget suppressed:", dedupKey);
    return;
  }
  lastWidgetQuestion = dedupKey;
  lastWidgetTime = now;

  // Silent widget — updates progress panel in side bar, no chat message
  if (actualType === "progress_indicator") {
    const appContainer = document.getElementById("progress-steps");
    if (appContainer) updateProgressIndicator(data, appContainer);
    return;
  }

  const msgDiv = document.createElement("div");
  msgDiv.className = "message gemini widget-message";
  lastWidgetMsgDiv = msgDiv;

  function sendAnswer(value) {
    console.log(
      "[sendAnswer] value:",
      value,
      "| msgDiv in DOM:",
      document.body.contains(msgDiv),
      "| connected:",
      geminiClient.isConnected(),
    );
    msgDiv.classList.add("frozen");
    msgDiv
      .querySelectorAll("button, input")
      .forEach((el) => (el.disabled = true));
    console.log("[sendAnswer] calling appendMessage...");
    appendMessage("user", value);
    console.log("[sendAnswer] calling geminiClient.sendText...");
    if (geminiClient.isConnected()) {
      geminiClient.sendText(`${question} My answer: ${value}`);
    } else {
      console.warn("[sendAnswer] geminiClient not connected, text not sent");
    }
  }

  if (actualType === "date") {
    renderDateWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "disclosure") {
    renderDisclosureWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "single" || actualType === "multi") {
    renderOptionsWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "country_select") {
    renderCountrySelectWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "phone") {
    renderPhoneWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "email") {
    renderEmailWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "drivers_license") {
    renderDriversLicenseWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "us_address") {
    // Legacy dispatch — emit US-mode address widget.
    renderAddressWidget(msgDiv, { ...data, mode: "US" }, sendAnswer);
  } else if (actualType === "address") {
    // New dual-mode address widget — mode comes from data.mode (US/INTERNATIONAL).
    renderAddressWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "ssn") {
    renderSsnWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "tax_id") {
    renderTaxIdWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "passport") {
    renderPassportWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "visa") {
    renderVisaWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "green_card") {
    renderGreenCardWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "id_card") {
    renderIdCardWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "address_proof_upload") {
    renderAddressProofUploadWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "financial_range") {
    // Dedup: remove any existing financial-range widget before rendering a new one
    const existing = document.querySelector(".widget-financial-card");
    if (existing) {
      existing.closest(".widget-message")?.remove();
    }
    renderFinancialRangeWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "investment_profile") {
    renderInvestmentProfileWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "employment") {
    renderEmploymentWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "personal_info") {
    renderPersonalInfoWidget(msgDiv, data, sendAnswer);
  } else if (actualType === "agreement") {
    renderAgreementsWidget(msgDiv, data, sendAnswer);
  }
}

// "Back to app" exit button — only visible when running inside the native
// webview (the bridge is injected by the host app). In a regular browser
// this button stays hidden so we don't tease users with an action that
// can't actually close the tab.
(function wireExitBtn() {
  const btn = document.getElementById("exitBtn");
  if (!btn) return;

  function reveal() {
    if (
      typeof window.isInNativeWebview === "function" &&
      window.isInNativeWebview()
    ) {
      btn.classList.remove("hidden");
      return true;
    }
    return false;
  }

  // Try a few times — some bridge implementations inject after DOMContentLoaded.
  if (!reveal()) {
    let tries = 0;
    const id = setInterval(() => {
      tries += 1;
      if (reveal() || tries >= 10) clearInterval(id);
    }, 300);
  }

  btn.onclick = () => {
    if (typeof window.exitWebview === "function") {
      window.exitWebview();
    }
  };
})();

async function _connectAvatar(sfuUrl, userToken) {
  if (!window.LivekitSDK) {
    console.warn("[avatar] SDK not loaded");
    return;
  }
  const containerEl = document.getElementById("avatar-container");
  try {
    const client = window.LivekitSDK.createClient({
      connectConfig: {
        type: "direct",
        config: { sfuUrl, userToken },
      },
      video: { containerElement: containerEl },
      audio: { output: { enabled: true, volume: 1.0, muted: false } },
      debug: false,
    });
    await client.connect();
    client.setVolume(1.0);
    try {
      client.unmute();
    } catch (_) {}
    console.log("[avatar] connected");
  } catch (e) {
    console.error("[avatar] connect failed:", e);
  }
}

// Connect Button Handler
connectBtn.onclick = async () => {
  // 16053: 开户引导弹窗_startonboarding
  trackEvent(
    "lhsbdw_cot_accountOpenningAgent_openaccountpop_startonboarding",
    "click",
  );
  statusDiv.textContent = "Connecting...";
  connectBtn.disabled = true;

  try {
    // Initialize audio context on user gesture
    await mediaHandler.initializeAudio();

    // Read stored credentials from cookies
    const cookies = {
      userid: getCookie("userid") || "",
      sessionid: getCookie("sessionid") || "",
      access_token: getCookie("access_token") || "",
    };
    // Only send cookies if at least one is non-empty
    const hasCookies =
      cookies.userid || cookies.sessionid || cookies.access_token;
    // Fall back to DOMESTIC if the user somehow reached connect without picking
    // a branch — keeps backwards compatibility with any deep-link / refresh case.
    const branch = selectedBranch || "DOMESTIC";
    geminiClient.connect(hasCookies ? cookies : null, { branch });
  } catch (error) {
    console.error("Connection error:", error);
    statusDiv.textContent = "Connection Failed: " + error.message;
    statusDiv.className = "status error";
    connectBtn.disabled = false;
  }
};

// Branch Selection — picks DOMESTIC or FOREIGNER, then proceeds to connect.
// Branch is now the first screen of the agent flow (the Agent/Classic mode
// selection was removed — reaching index.html implies the user already chose
// the agent path on the marketing landing).
function startWithBranch(branch) {
  selectedBranch = branch;
  branchSection.classList.add("hidden");
  connectBtn.click();
}

branchDomesticBtn.onclick = () => {
  trackEvent("lhsbdw_cot_accountOpenningAgent_branch_domestic", "click");
  startWithBranch("DOMESTIC");
};

branchForeignerBtn.onclick = () => {
  trackEvent("lhsbdw_cot_accountOpenningAgent_branch_foreigner", "click");
  startWithBranch("FOREIGNER");
};

// Make entire branch cards clickable
branchCardDomestic.onclick = (e) => {
  if (e.target.closest("button")) return;
  branchDomesticBtn.click();
};
branchCardForeigner.onclick = (e) => {
  if (e.target.closest("button")) return;
  branchForeignerBtn.click();
};

// UI Controls
disconnectBtn.onclick = () => {
  disconnectBtn.classList.add("hidden");
  _showSessionEndForReal();
  geminiClient.disconnect();
};

micBtn.onclick = async () => {
  if (mediaHandler.isRecording) {
    mediaHandler.stopAudio();
    micBtn.classList.remove("active");
    micBtn.title = "Microphone";
  } else {
    try {
      await mediaHandler.startAudio((data) => {
        if (geminiClient.isConnected()) {
          geminiClient.send(data);
        }
      });
      micBtn.classList.add("active");
      micBtn.title = "Stop Microphone";
    } catch (e) {
      alert("Could not start audio capture");
    }
  }
};

cameraBtn.onclick = async () => {
  if (cameraBtn.classList.contains("active")) {
    mediaHandler.stopVideo(videoPreview);
    cameraBtn.classList.remove("active");
    cameraBtn.title = "Camera";
    videoPlaceholder.classList.remove("hidden");
  } else {
    if (mediaHandler.videoStream) {
      mediaHandler.stopVideo(videoPreview);
    }

    try {
      await mediaHandler.startVideo(videoPreview, (base64Data) => {
        if (geminiClient.isConnected()) {
          geminiClient.sendImage(base64Data);
        }
      });
      cameraBtn.classList.add("active");
      cameraBtn.title = "Stop Camera";
      videoPlaceholder.classList.add("hidden");
    } catch (e) {
      alert("Could not access camera");
    }
  }
};

stopBtn.onclick = () => {
  mediaHandler.stopAudioPlayback();
  geminiClient.send(JSON.stringify({ type: "interrupt" }));
  setResponding(false);
};

sendBtn.onclick = sendText;
textInput.onkeypress = (e) => {
  if (e.key === "Enter") sendText();
};

function sendText() {
  const text = textInput.value;
  if (text && geminiClient.isConnected()) {
    geminiClient.sendText(text);
    appendMessage("user", text);
    textInput.value = "";
    showThinking();
  }
}

function resetUI() {
  branchSection.classList.remove("hidden");
  authSection.classList.add("hidden");
  appSection.classList.add("hidden");
  sessionEndSection.classList.add("hidden");
  // Force the user to re-pick a branch on the next run rather than silently
  // reusing the previous one.
  selectedBranch = null;

  mediaHandler.stopAudio();
  mediaHandler.stopVideo(videoPreview);
  videoPlaceholder.classList.remove("hidden");

  micBtn.classList.remove("active");
  micBtn.title = "Microphone";
  cameraBtn.classList.remove("active");
  cameraBtn.title = "Camera";
  chatLog.innerHTML = "";
  setResponding(false);
  initializeProgress();

  isReconnecting = false;
  geminiClient._wasEverConnected = false;
}

function showSessionEnd() {
  appSection.classList.add("hidden");
  sessionEndSection.classList.add("hidden");
  mediaHandler.stopAudio();
  mediaHandler.stopVideo(videoPreview);
}

function _showSessionEndForReal() {
  appSection.classList.add("hidden");
  sessionEndSection.classList.remove("hidden");
  mediaHandler.stopAudio();
  mediaHandler.stopVideo(videoPreview);
}

restartBtn.onclick = () => {
  resetUI();
};

// ─── File Upload ───────────────────────────────────────────────
async function uploadFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const base64 = reader.result.split(",")[1];
        let fileType = null;
        if (file.type.startsWith("image/")) {
          fileType = await geminiClient.classifyFile(base64, file.name);
          console.log("[uploadFile] detected file_type:", fileType);
        }
        const result = await geminiClient.uploadFile(
          base64,
          file.name,
          true,
          fileType,
        );
        resolve(result);
      } catch (e) {
        reject(e);
      }
    };
    reader.onerror = () => reject(new Error("File read failed"));
    reader.readAsDataURL(file);
  });
}

attachBtn.onclick = () => fileInput.click();

fileInput.onchange = async () => {
  const file = fileInput.files[0];
  if (!file) return;
  fileInput.value = "";

  const isImage = file.type.startsWith("image/");
  const localUrl = isImage ? URL.createObjectURL(file) : null;
  const loadingDiv = appendFileMessage(file.name, file.size, true, isImage);

  try {
    await uploadFile(file);
    if (isImage && localUrl) {
      updateFilePreview(loadingDiv, localUrl, true);
    } else {
      loadingDiv.querySelector(".file-uploading")?.remove();
      const sizeEl = loadingDiv.querySelector(".file-card-size");
      if (sizeEl) {
        const s = file.size;
        sizeEl.textContent =
          s > 1024 * 1024
            ? `${(s / 1024 / 1024).toFixed(1)} MB`
            : `${(s / 1024).toFixed(0)} KB`;
      }
    }
    if (geminiClient.isConnected()) {
      geminiClient.sendText(
        `The user has uploaded a document: "${file.name}". ` +
          `The file has been uploaded to the server. ` +
          `Please now examine the image you saw, call extract_document_info to read ` +
          `all visible fields, and then call the appropriate present_*_input widget ` +
          `with the extracted fields passed as the 'fields' parameter.`,
      );
    }
  } catch (e) {
    console.error("Upload error:", e);
    if (localUrl) URL.revokeObjectURL(localUrl);
    const info = loadingDiv.querySelector(".file-card-info");
    if (info) {
      info.innerHTML = `
        <span class="file-card-name">${file.name}</span>
        <span class="file-upload-error">Upload failed</span>`;
    }
  }
};

function openLightbox(imageUrl) {
  const overlay = document.createElement("div");
  overlay.className = "lightbox-overlay";
  overlay.innerHTML = `
    <span class="lightbox-close">&times;</span>
    <img src="${imageUrl}" alt="full preview" />
  `;
  overlay.onclick = (e) => {
    if (e.target === overlay || e.target.classList.contains("lightbox-close")) {
      overlay.remove();
    }
  };
  document.addEventListener("keydown", function onEsc(e) {
    if (e.key === "Escape") {
      overlay.remove();
      document.removeEventListener("keydown", onEsc);
    }
  });
  document.body.appendChild(overlay);
}

function updateFilePreview(msgDiv, fileUrl, isImage) {
  if (isImage) {
    msgDiv.innerHTML = `
      <div class="file-card">
        <img class="file-preview-img" src="${fileUrl}" alt="preview" onclick="openLightbox('${fileUrl.replace(/'/g, "\\'")}')" />
        <div class="file-card-info">
          <span class="file-card-name">${msgDiv.querySelector(".file-card-name")?.textContent || ""}</span>
        </div>
      </div>`;
  } else {
    const nameEl = msgDiv.querySelector(".file-card-name");
    msgDiv.innerHTML = `
      <div class="file-card">
        <div class="file-card-icon">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
            <polyline points="14 2 14 8 20 8"/>
          </svg>
        </div>
        <div class="file-card-info">
          <a class="file-card-link" href="${fileUrl}" target="_blank">${nameEl?.textContent || "File"}</a>
        </div>
      </div>`;
  }
}

function appendFileMessage(name, size, uploading) {
  const sizeStr =
    size > 1024 * 1024
      ? `${(size / 1024 / 1024).toFixed(1)} MB`
      : `${(size / 1024).toFixed(0)} KB`;

  const msgDiv = document.createElement("div");
  msgDiv.className = "message user file";
  msgDiv.innerHTML = `
    <div class="file-card">
      <div class="file-card-icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
        </svg>
      </div>
      <div class="file-card-info">
        <span class="file-card-name">${name}</span>
        <span class="file-card-size">${uploading ? "" : sizeStr}</span>
        ${uploading ? '<span class="file-uploading">Uploading...</span>' : ""}
      </div>
    </div>`;

  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;
  return msgDiv;
}

// ─── Mobile avatar-side mic/camera mirror buttons ──────────────────
// Forward clicks to the original buttons and mirror their .active state.
// Also toggle .camera-active on .video-container so it fullscreens on mobile.
(function setupMobileAvatarControls() {
  const micBtnMobile = document.getElementById("micBtnMobile");
  const cameraBtnMobile = document.getElementById("cameraBtnMobile");
  const videoContainer = document.querySelector(".video-container");
  if (!micBtnMobile || !cameraBtnMobile) return;

  micBtnMobile.addEventListener("click", () => micBtn.click());
  cameraBtnMobile.addEventListener("click", () => cameraBtn.click());

  const syncMic = () => {
    micBtnMobile.classList.toggle(
      "active",
      micBtn.classList.contains("active"),
    );
    micBtnMobile.title = micBtn.title;
  };
  const syncCamera = () => {
    const active = cameraBtn.classList.contains("active");
    cameraBtnMobile.classList.toggle("active", active);
    cameraBtnMobile.title = cameraBtn.title;
    if (videoContainer)
      videoContainer.classList.toggle("camera-active", active);
  };

  new MutationObserver(syncMic).observe(micBtn, {
    attributes: true,
    attributeFilter: ["class", "title"],
  });
  new MutationObserver(syncCamera).observe(cameraBtn, {
    attributes: true,
    attributeFilter: ["class", "title"],
  });
})();
