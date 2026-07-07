/**
 * GeminiClient: Handles WebSocket communication with auto-reconnect
 */
class GeminiClient {
  constructor(config) {
    this.websocket = null;
    this.onOpen = config.onOpen;
    this.onMessage = config.onMessage;
    this.onClose = config.onClose;
    this.onError = config.onError;
    this.onDisconnect = config.onDisconnect;
    this.onReconnecting = config.onReconnecting;
    this.maxRetries = 3;
    this.retryDelay = 1500;
    this.isManualClose = false;
    // Resolved session state after init cookies verification
    this._sessionState = null;
    this._sessionStateResolve = null;
  }

  connect(cookies = null, options = {}) {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    // Derive WS path from current page path: /onboarding/* → /onboarding/ws, /* → /ws
    const basePath = window.location.pathname.replace(/\/[^/]*$/, "");
    const wsPath = basePath ? `${basePath}/ws` : "/ws";
    let wsUrl = `${protocol}//${window.location.host}${wsPath}`;

    // The onboarding branch (DOMESTIC / FOREIGNER) is selected by the user before
    // connect() and must reach the backend before the Gemini session starts —
    // it decides which system prompt is loaded. Send it as a query string so the
    // FastAPI WebSocket handler can read it at accept() time.
    const branch = (options.branch || "").toUpperCase();
    if (branch === "DOMESTIC" || branch === "FOREIGNER") {
      wsUrl += `?branch=${branch}`;
    }

    this.isManualClose = false;
    // Store for reconnect use
    this._cookies = cookies;
    this._wsUrl = wsUrl;
    this._connect(wsUrl, cookies);
  }

  sendInit(cookies) {
    if (this.websocket && this.websocket.readyState === WebSocket.OPEN) {
      this.websocket.send(JSON.stringify({ type: "init", cookies: cookies }));
    }
  }

  _connect(wsUrl, cookies = null, retryCount = 0) {
    this.websocket = new WebSocket(wsUrl);
    this.websocket.binaryType = "arraybuffer";
    const self = this;

    this.websocket.onopen = () => {
      retryCount = 0;
      // Always send init — backend handles both cookie and no-cookie cases
      const c = cookies || self._cookies;
      self.sendInit(c || {});
      if (self.onOpen) self.onOpen();
    };

    this.websocket.onmessage = (event) => {
      // Intercept session_state to handle it before other processing
      if (event.data && typeof event.data === "string") {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "session_state") {
            this.setSessionState(msg);
          }
        } catch (e) {}
      }
      if (this.onMessage) this.onMessage(event);
    };

    this.websocket.onclose = (event) => {
      if (this.isManualClose) return;
      if (this.onClose) this.onClose(event);

      // Auto-reconnect with retry limit
      if (retryCount < this.maxRetries) {
        const delay = this.retryDelay * Math.pow(2, retryCount);
        retryCount++;
        console.log(`WebSocket closed unexpectedly. Retrying in ${delay}ms (${retryCount}/${this.maxRetries})...`);
        if (this.onReconnecting) {
          this.onReconnecting({ retryCount, maxRetries: this.maxRetries, delay });
        }
        setTimeout(() => this._connect(wsUrl, this._cookies, retryCount), delay);
      } else {
        console.warn("WebSocket reconnection failed after max retries");
        if (this.onError) {
          this.onError({ type: "reconnect_failed" });
        }
      }
    };

    this.websocket.onerror = (event) => {
      if (this.onError) this.onError(event);
    };
  }

  send(data) {
    if (this.websocket && this.websocket.readyState === WebSocket.OPEN) {
      this.websocket.send(data);
    }
  }

  sendText(text) {
    this.send(JSON.stringify({ text: text }));
  }

  sendImage(base64Data, mimeType = "image/jpeg") {
    this.send(
      JSON.stringify({
        type: "image",
        mime_type: mimeType,
        data: base64Data,
      })
    );
  }

  disconnect() {
    this.isManualClose = true;
    this._sessionState = null;
    this._sessionStateResolve = null;
    if (this.websocket) {
      this.websocket.close();
      this.websocket = null;
    }
  }

  isConnected() {
    return this.websocket && this.websocket.readyState === WebSocket.OPEN;
  }

  setSessionState(state) {
    this._sessionState = state;
    if (this._sessionStateResolve) {
      this._sessionStateResolve(state);
      this._sessionStateResolve = null;
    }
  }

  waitForSessionState() {
    if (this._sessionState !== null) {
      return Promise.resolve(this._sessionState);
    }
    return new Promise((resolve) => {
      this._sessionStateResolve = resolve;
    });
  }

  classifyFile(base64Data, filename) {
    return new Promise((resolve) => {
      if (!this.websocket || this.websocket.readyState !== WebSocket.OPEN) {
        resolve("id_card");
        return;
      }
      const self = this;
      const handler = function(event) {
        try {
          const data = JSON.parse(event.data);
          if (data.type === "classify_file_result") {
            self.websocket.removeEventListener("message", handler);
            clearTimeout(timer);
            resolve(data.file_type || "id_card");
          }
        } catch (e) {}
      };
      const timer = setTimeout(() => {
        self.websocket.removeEventListener("message", handler);
        resolve("id_card");
      }, 15000);
      this.websocket.addEventListener("message", handler);
      this.websocket.send(JSON.stringify({
        type: "classify_file",
        data: base64Data,
        filename: filename,
      }));
    });
  }

  uploadFile(base64Data, filename, is_need_min = false, file_type = null) {
    return new Promise((resolve, reject) => {
      if (!this.websocket || this.websocket.readyState !== WebSocket.OPEN) {
        reject(new Error("WebSocket not connected"));
        return;
      }
      const msg = {
        type: "upload_file",
        data: base64Data,
        filename: filename,
        is_need_min: is_need_min,
      };
      if (file_type) msg.file_type = file_type;
      const self = this;
      const handler = function(event) {
        try {
          const data = JSON.parse(event.data);
          if (data.type === "upload_file_result") {
            self.websocket.removeEventListener("message", handler);
            const result = data.result || {};
            const isOk = result.s === "ok" || result.i18nMsg === "success";
            if (isOk) {
              resolve(result.d || {});
            } else {
              reject(new Error("Upload failed: " + (result.errmsg || result.message || JSON.stringify(result))));
            }
          }
        } catch (e) {}
      };
      this.websocket.addEventListener("message", handler);
      setTimeout(() => {
        self.websocket.removeEventListener("message", handler);
        reject(new Error("Upload timeout"));
      }, 30000);
      this.websocket.send(JSON.stringify(msg));
    });
  }
}
