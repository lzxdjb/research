/**
 * Native WebViewJavascriptBridge helper.
 *
 * The host app injects `window.WebViewJavascriptBridge` and a
 * `window.callNativeHandler(name, params, callback)` API. When this page is
 * loaded in a regular browser instead of the app's webview, those globals are
 * absent and every call is a silent no-op (resolves with errorCode 0) so the
 * UI doesn't break in dev.
 *
 * Bridge response shape (per app contract):
 *   { errorCode: number, errorMsg: string, data: { ... } }
 *   errorCode === 0 means success.
 */
function promiseCallNativeHandler(bridgeName, bridgeParams) {
  return new Promise((resolve, reject) => {
    if (
      typeof window.WebViewJavascriptBridge !== "undefined" &&
      typeof window.callNativeHandler === "function"
    ) {
      const params = bridgeParams || {};
      window.callNativeHandler(bridgeName, params, (responseData) => {
        if (!responseData || responseData.errorCode !== 0) {
          console.error("[bridge] " + bridgeName + " failed:", responseData);
          reject(responseData);
        } else {
          console.log("[bridge] " + bridgeName + " ok:", responseData);
          resolve(responseData);
        }
      });
    } else {
      console.log(
        "[bridge] non-app environment — " + bridgeName + " is a no-op",
      );
      resolve({
        errorCode: 0,
        errorMsg: "non-app environment",
        data: { userId: "", token: "", isLogin: "", fingerprint: "" },
      });
    }
  });
}

/**
 * Returns true iff the page is running inside the native webview (i.e. the
 * bridge is available). UI elements that only make sense inside the app
 * (e.g. the "back to app" button) can use this to conditionally show.
 */
function isInNativeWebview() {
  return (
    typeof window.WebViewJavascriptBridge !== "undefined" &&
    typeof window.callNativeHandler === "function"
  );
}

/** Ask the host app to close / pop this webview. */
function exitWebview() {
  return promiseCallNativeHandler("goBack").catch((err) => {
    console.warn("[bridge] goBack failed:", err);
  });
}

// Expose to other scripts loaded after this one.
window.promiseCallNativeHandler = promiseCallNativeHandler;
window.isInNativeWebview = isInNativeWebview;
window.exitWebview = exitWebview;
