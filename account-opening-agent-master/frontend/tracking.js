// --- Shared tracking + auth helpers ---
// Loaded by both the marketing landing (landing.html) and the agent onboarding
// flow (index.html). Defines the globals main.js consumes (UTMTracker,
// getCookie, eraseCookie, trackEvent, ensureVisitorId, ...) and fires the
// page-exposure event once the visitor id is ready.
//
// Depends on common-v3.js + weblog.js being loaded earlier in the page.

// ─── UTM Tracking ───────────────────────────────────────────────
const UTMTracker = (function () {
  const UTM_KEYS = [
    "utm_medium",
    "utm_source",
    "utm_campaign",
    "utm_content",
    "invite_code",
  ];
  const STORAGE_KEY = "lighthouse_utm";

  // Parse from URL query string, store in sessionStorage for persistence
  function _capture() {
    const params = new URLSearchParams(window.location.search);
    const stored = _load();
    let updated = false;

    UTM_KEYS.forEach(function (key) {
      const val = params.get(key);
      if (val) {
        stored[key] = val;
        updated = true;
      }
    });

    if (updated) {
      _save(stored);
    }
    return stored;
  }

  function _load() {
    try {
      const raw = sessionStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (e) {
      return {};
    }
  }

  function _save(data) {
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(data));
    } catch (e) {}
  }

  const _data = _capture();

  return {
    getAll: function () {
      return Object.assign({}, _data);
    },
  };
})();

// ─── Cookie helpers ────────────────────────────────────────────
function getCookie(name) {
  const match = document.cookie.match(new RegExp("(^| )" + name + "=([^;]+)"));
  return match ? match[2] : "";
}
function getCookieDomain() {
  var hostname = window.location.hostname;
  if (hostname === "localhost") return "";
  var parts = hostname.split(".");
  // Extract second-level domain: open.lighthorse.io → .lighthorse.io
  if (parts.length >= 3) {
    parts.shift();
  }
  return ";domain=." + parts.join(".");
}

function setCookie(name, value, days) {
  const exp = new Date();
  exp.setTime(exp.getTime() + days * 24 * 60 * 60 * 1000);
  document.cookie =
    name +
    "=" +
    value +
    ";path=/" +
    getCookieDomain() +
    ";expires=" +
    exp.toUTCString();
  console.log("[auth] setCookie:", name, "=", value.substring(0, 10) + "...");
}

function eraseCookie(name) {
  document.cookie =
    name +
    "=;path=/" +
    getCookieDomain() +
    ";expires=Thu, 01 Jan 1970 00:00:00 GMT";
}

// ─── Visitor ID ────────────────────────────────────────────────

function uuidv4() {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
    var r = (Math.random() * 16) | 0;
    var v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

var BASE_URL_TEST = "https://test-lighthorse-trade.touzime.cn";
var BASE_URL_PROD = "https://interface.lighthorse.io";
var BASE_URL =
  window.location.hostname.indexOf("test") !== -1 ||
  window.location.hostname.indexOf("touzime.cn") !== -1
    ? BASE_URL_TEST
    : BASE_URL_PROD;

function getOrCreateFingerprint() {
  var udid = getCookie("lighthorse_fingerprint");
  if (udid) return udid;
  udid = uuidv4();
  document.cookie =
    "lighthorse_fingerprint=" +
    udid +
    ";path=/" +
    getCookieDomain() +
    ";max-age=31536000";
  return udid;
}

async function ensureVisitorId() {
  if (getCookie("userid")) return;

  var udid = getOrCreateFingerprint();

  try {
    var body = new URLSearchParams();
    body.append("udid", udid);
    body.append("clientType", "WEB");

    var resp = await fetch(BASE_URL + "/auth/visitor/queryVisitorId", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "x-auth-appname": "AINVEST_BROKER",
        "x-auth-progid": "8047",
      },
      body: body.toString(),
    });
    var json = await resp.json();
    if (json.data && json.data.userid) {
      setCookie("userid", json.data.userid, 30);
      console.log("[auth] visitorId saved:", json.data.userid);
    }
  } catch (e) {
    console.error("[auth] Failed to get visitorId:", e);
  }
}

// ─── Weblog Tracking ────────────────────────────────────────────
window.weblog.setConfig({
  appKey: "a3a2a1d98a",
  domain: "stat.ainvest.com",
  logPrefix: "lhsbdw_cot_accountOpenningAgent",
  debug: false,
});

var UTM_LOG_MAP = {
  utm_medium: "type",
  utm_source: "source",
  utm_campaign: "childType",
  utm_content: "url",
  invite_code: "inviteCode",
};

function trackEvent(id, action, logmap) {
  window.weblog.report({
    id: id,
    action: action,
    logmap: logmap,
  });
}

// 16050: 页面曝光（需等 visitorId 就绪后上报）
// Fired on both the marketing landing and the agent onboarding entry; the
// logmap carries fid + the 5 UTM params captured from the URL on first hit.
ensureVisitorId().then(function () {
  var utmData = UTMTracker.getAll();
  var logmap = {};
  logmap.fid = getOrCreateFingerprint();
  var keys = Object.keys(UTM_LOG_MAP);
  for (var i = 0; i < keys.length; i++) {
    var utmKey = keys[i];
    var val = utmData[utmKey];
    if (val) {
      logmap[UTM_LOG_MAP[utmKey]] = val;
    }
  }
  trackEvent(window.WEBLOG_PAGE_ID || "lhsbdw_cot_landingPage", "show", logmap);
});
