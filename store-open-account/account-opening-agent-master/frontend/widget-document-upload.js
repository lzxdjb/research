/**
 * Reusable Document Upload Widget — base component for all file-upload widgets.
 *
 * Usage:
 *   createDocumentUploadWidget(config)(msgDiv, data, sendAnswer)
 *
 * Config shape:
 * {
 *   documentType: string,            // e.g. "drivers_license"
 *   title: string,                   // card heading (fallback if data.question missing)
 *   slots: [{                        // upload areas
 *     key: string,                   // unique slot id
 *     label: string,                 // display label
 *     fileType: string,              // backend file_type enum value (static)
 *     fileTypeFromField: string,     // OR: metadata field key whose value provides fileType at upload time
 *     required: boolean,
 *   }],
 *   metadataFields: [{ key, label, type("text"|"date"|"select"|"country"), required, options? }],
 *   reviewTitle: string,              // optional section heading (default: "Confirm Extracted Information")
 *   reviewFields: [{ key, label, type, required }],   // OCR review fields shown above upload slots
 * }
 *
 * Data shape (passed from tool call):
 * {
 *   question: string,
 *   prefill: { ... },               // OCR-extracted field values
 *   default_country: string,
 * }
 */

function createDocumentUploadWidget(config) {
    const {
    documentType,
    title = "Document Upload",
    slots = [],
    metadataFields = [],
    reviewFields = [],
    reviewTitle = "Confirm Extracted Information",
  } = config;

  return function renderWidget(msgDiv, data, sendAnswer) {
    const { question, default_country = "" } = data;
    const prefill = data.prefill || data.fields || {};
    const heading = question || title;

    const defaultCC = (default_country || "").toUpperCase();
    const countries = (typeof window !== "undefined" && window.ISO_COUNTRIES) || [];

    // --- Build review-fields HTML (OCR review area, shown above upload slots) ---
    let reviewHtml = "";
    if (reviewFields.length > 0) {
      const rows = [];
      let rowFields = [];
      reviewFields.forEach((f) => {
        rowFields.push(f);
        if (rowFields.length === 2) {
          rows.push(rowFields);
          rowFields = [];
        }
      });
      if (rowFields.length > 0) rows.push(rowFields);

      reviewHtml = `
        <div class="widget-dl-section">
          <div class="widget-dl-section-title">${reviewTitle}</div>
          ${rows.map((pair) => `
            <div class="widget-dl-row">
              ${pair.map((f) => {
                const val = prefill[f.key] !== undefined ? prefill[f.key] : "";
                if (f.type === "select") {
                  return `
                    <label class="widget-dl-label">
                      <span class="widget-dl-label-text">${f.label}${f.required ? ' <span style="color:#dc3545;">*</span>' : ""}</span>
                      <select class="widget-dl-input widget-dl-select" name="review_${f.key}">
                        ${(f.options || []).map((o) => `<option value="${o}" ${val === o ? "selected" : ""}>${o}</option>`).join("")}
                      </select>
                    </label>`;
                }
                return `
                  <label class="widget-dl-label${pair.length === 1 ? " widget-dl-label--full" : ""}">
                    <span class="widget-dl-label-text">${f.label}${f.required ? ' <span style="color:#dc3545;">*</span>' : ""}</span>
                    <input class="widget-dl-input" type="text"
                      name="review_${f.key}" value="${escapeHtml(String(val))}"
                      placeholder="${f.placeholder || (f.type === "date" ? "YYYY-MM-DD" : "")}" />
                  </label>`;
              }).join("")}
            </div>`).join("")}
        </div>`;
    }

    // --- Build file-slot HTML ---
    const slotsHtml = slots.map((slot) => `
      <div class="widget-doc-row widget-doc-row--file" data-slot="${slot.key}">
        <label class="widget-doc-label">${slot.label}${slot.required ? ' <span style="color:#dc3545;">*</span>' : ""}</label>
        <div class="widget-upload-area" data-slot-key="${slot.key}">
          <input type="file" class="widget-upload-input" data-slot-key="${slot.key}"
            accept="image/*,.pdf" style="display:none;" />
          <button class="widget-upload-btn widget-upload-pick-btn" data-slot-key="${slot.key}">
            Choose File
          </button>
          <button class="widget-upload-btn widget-upload-camera-btn" data-slot-key="${slot.key}">
            Take Photo
          </button>
          <div class="widget-file-preview hidden" data-slot-key="${slot.key}">
            <img class="widget-file-preview-img" data-slot-key="${slot.key}" alt="" />
            <span class="widget-file-preview-name" data-slot-key="${slot.key}"></span>
            <button class="widget-file-preview-remove" data-slot-key="${slot.key}">&times;</button>
          </div>
          <div class="widget-file-status hidden" data-slot-key="${slot.key}"></div>
        </div>
      </div>`).join("");

    // --- Build metadata-fields HTML ---
    const metaHtml = metadataFields.map((f) => {
      const val = prefill[f.key] !== undefined ? prefill[f.key] : "";
      if (f.type === "select") {
        return `
          <div class="widget-doc-row">
            <label class="widget-doc-label">${f.label}${f.required ? ' <span style="color:#dc3545;">*</span>' : ""}</label>
            <select class="widget-address-select" name="meta_${f.key}">
              <option value="">${f.placeholder || "Select..."}</option>
              ${(f.options || []).map((o) => {
                const ov = typeof o === "string" ? o : o.value;
                const ol = typeof o === "string" ? o : o.label;
                return `<option value="${ov}" ${val === ov ? "selected" : ""}>${ol}</option>`;
              }).join("")}
            </select>
          </div>`;
      }
      if (f.type === "country") {
        return `
          <div class="widget-doc-row">
            <label class="widget-doc-label">${f.label}${f.required ? ' <span style="color:#dc3545;">*</span>' : ""}</label>
            <select class="widget-address-select" name="meta_${f.key}">
              <option value="">Select country</option>
              ${countries.map((c) => `<option value="${c.code}" ${defaultCC === c.code ? "selected" : ""}>${c.name} (${c.code})</option>`).join("")}
            </select>
          </div>`;
      }
      if (f.type === "radio") {
        return `
          <div class="widget-doc-row">
            <label class="widget-doc-label">${f.label}${f.required ? ' <span style="color:#dc3545;">*</span>' : ""}</label>
            <div class="widget-proof-radio-row">
              ${(f.options || []).map((o, i) => {
                const ov = typeof o === "string" ? o : o.value;
                const ol = typeof o === "string" ? o : o.label;
                return `
                  <label class="widget-proof-radio">
                    <input type="radio" name="meta_${f.key}" value="${ov}" ${i === 0 ? "checked" : ""} />
                    <span>${ol}</span>
                  </label>`;
              }).join("")}
            </div>
          </div>`;
      }
      return `
        <div class="widget-doc-row">
          <label class="widget-doc-label">${f.label}${f.required ? ' <span style="color:#dc3545;">*</span>' : ""}</label>
          <input class="widget-address-input" name="meta_${f.key}" type="text"
            value="${escapeHtml(String(val))}" placeholder="${f.placeholder || (f.type === "date" ? "YYYY-MM-DD" : "")}" ${f.maxlength ? `maxlength="${f.maxlength}"` : ""} autocomplete="off" />
        </div>`;
    }).join("");

    // --- In-place field updater (for OCR prefill without losing upload state) ---
    msgDiv._widgetUpdateFields = function (newFields) {
      if (!newFields || Object.keys(newFields).length === 0) return;

      // Update review fields
      reviewFields.forEach((f) => {
        const val = newFields[f.key] !== undefined ? newFields[f.key] : null;
        if (val === null) return;
        const el = msgDiv.querySelector(`[name="review_${f.key}"]`);
        if (el) {
          if (el.tagName === "SELECT") {
            const opt = el.querySelector(`option[value="${val.replace(/"/g, "&quot;")}"]`);
            if (opt) opt.selected = true;
          } else {
            el.value = val;
          }
          el.dispatchEvent(new Event("change", { bubbles: true }));
        }
      });

      // Update metadata fields
      metadataFields.forEach((f) => {
        const val = newFields[f.key] !== undefined ? newFields[f.key] : null;
        if (val === null) return;
        if (f.type === "radio") {
          const radio = msgDiv.querySelector(`input[name="meta_${f.key}"][value="${val.replace(/"/g, "&quot;")}"]`);
          if (radio) { radio.checked = true; radio.dispatchEvent(new Event("change", { bubbles: true })); }
        } else {
          const el = msgDiv.querySelector(`[name="meta_${f.key}"]`);
          if (el) {
            if (el.tagName === "SELECT") {
              const opt = el.querySelector(`option[value="${val.replace(/"/g, "&quot;")}"]`);
              if (opt) opt.selected = true;
            } else {
              el.value = val;
            }
            el.dispatchEvent(new Event("change", { bubbles: true }));
          }
        }
      });
    };

    // --- Assemble ---
    msgDiv.innerHTML = `
      <div class="widget-card widget-doc-card widget-doc-upload-card">
        <p class="widget-question">${heading}</p>
        <div class="widget-doc-form">
          ${reviewHtml}
          ${slotsHtml}
          ${metaHtml}
          <div class="widget-address-error hidden" data-error></div>
        </div>
        <button class="widget-submit-btn" data-submit disabled>Submit</button>
      </div>`;

    const chatLog = document.getElementById("chat-log");
    chatLog.appendChild(msgDiv);
    chatLog.scrollTop = chatLog.scrollHeight;

    // --- State ---
    const uploaded = {};       // { slotKey: { filename, fileType } }
    const uploading = {};      // { slotKey: true }
    const errorDiv = msgDiv.querySelector("[data-error]");
    const submitBtn = msgDiv.querySelector("[data-submit]");

    function showError(m) { errorDiv.textContent = m; errorDiv.classList.remove("hidden"); }
    function clearError() { errorDiv.classList.add("hidden"); }

    function refreshSubmit() {
      const allSlotsDone = slots.every((s) => !s.required || uploaded[s.key]);
      submitBtn.disabled = !allSlotsDone;
    }

    // --- File helpers ---
    function readAsBase64(file) {
      return new Promise((resolve, reject) => {
        const r = new FileReader();
        r.onload = () => resolve(String(r.result).split(",")[1]);
        r.onerror = () => reject(new Error("File read failed"));
        r.readAsDataURL(file);
      });
    }

    async function handleFile(slotKey, file) {
      if (!file) return;
      const slot = slots.find((s) => s.key === slotKey);
      if (!slot) return;

      uploading[slotKey] = true;
      uploaded[slotKey] = null;
      const statusEl = msgDiv.querySelector(`.widget-file-status[data-slot-key="${slotKey}"]`);
      const previewEl = msgDiv.querySelector(`.widget-file-preview[data-slot-key="${slotKey}"]`);
      const previewImg = msgDiv.querySelector(`.widget-file-preview-img[data-slot-key="${slotKey}"]`);
      const previewName = msgDiv.querySelector(`.widget-file-preview-name[data-slot-key="${slotKey}"]`);

      function setStatus(text, isError) {
        if (statusEl) {
          statusEl.textContent = text;
          statusEl.classList.toggle("hidden", !text);
          statusEl.classList.toggle("widget-file-status--error", !!isError);
        }
      }

      // Resolve fileType: dynamic from metadata field, or static from slot config
      let resolvedFileType = slot.fileType;
      if (slot.fileTypeFromField) {
        const typeEl = msgDiv.querySelector(`[name="meta_${slot.fileTypeFromField}"]`);
        resolvedFileType = typeEl ? typeEl.value : slot.fileType;
      }
      if (!resolvedFileType) {
        setStatus("Please select a document type first.", true);
        uploading[slotKey] = false;
        return;
      }

      setStatus(`Uploading ${file.name}…`, false);
      try {
        const base64 = await readAsBase64(file);
        await geminiClient.uploadFile(base64, file.name, true, resolvedFileType);
        uploaded[slotKey] = { filename: file.name, fileType: resolvedFileType };
        setStatus(`Uploaded`, false);

        // Show preview
        if (previewEl) {
          previewEl.classList.remove("hidden");
          if (previewImg && file.type.startsWith("image/")) {
            previewImg.src = URL.createObjectURL(file);
          }
          if (previewName) previewName.textContent = file.name;
        }
      } catch (e) {
        setStatus("Upload failed: " + (e.message || e), true);
        uploaded[slotKey] = null;
      } finally {
        uploading[slotKey] = false;
        clearError();
        refreshSubmit();
      }
    }

    // --- Wire up file inputs ---
    slots.forEach((slot) => {
      const fileInput = msgDiv.querySelector(`.widget-upload-input[data-slot-key="${slot.key}"]`);
      const pickBtn = msgDiv.querySelector(`.widget-upload-pick-btn[data-slot-key="${slot.key}"]`);
      const cameraBtn = msgDiv.querySelector(`.widget-upload-camera-btn[data-slot-key="${slot.key}"]`);
      const removeBtn = msgDiv.querySelector(`.widget-file-preview-remove[data-slot-key="${slot.key}"]`);

      if (pickBtn && fileInput) {
        pickBtn.onclick = () => fileInput.click();
      }
      if (cameraBtn && fileInput) {
        // Use capture attribute for mobile camera
        cameraBtn.onclick = () => {
          fileInput.setAttribute("capture", "environment");
          fileInput.click();
          fileInput.removeAttribute("capture");
        };
      }
      if (fileInput) {
        fileInput.onchange = () => {
          const file = fileInput.files && fileInput.files[0];
          fileInput.value = "";
          if (file) handleFile(slot.key, file);
        };
      }
      if (removeBtn) {
        removeBtn.onclick = () => {
          uploaded[slot.key] = null;
          const previewEl = msgDiv.querySelector(`.widget-file-preview[data-slot-key="${slot.key}"]`);
          const statusEl = msgDiv.querySelector(`.widget-file-status[data-slot-key="${slot.key}"]`);
          if (previewEl) previewEl.classList.add("hidden");
          if (statusEl) { statusEl.textContent = ""; statusEl.classList.add("hidden"); }
          refreshSubmit();
        };
      }
    });

    // --- Submit ---
    submitBtn.onclick = () => {
      if (msgDiv.classList.contains("frozen")) return;
      clearError();

      // Check uploads still in progress
      const stillUploading = slots.filter((s) => uploading[s.key]);
      if (stillUploading.length > 0) {
        showError("Please wait — file upload still in progress.");
        return;
      }

      // Check all required slots have uploads
      for (const s of slots) {
        if (s.required && !uploaded[s.key]) {
          showError(`Please upload: ${s.label}`);
          return;
        }
      }

      // Helper — detect "this is an expiration date that must not be in the past".
      // Triggered by date-type fields whose key contains "expir" (expiration_date,
      // expiry_date, etc.). Accepts ISO-ish "YYYY-MM-DD" plus a few common variants.
      function isExpirationKey(key) {
        return /expir|expiry/i.test(key);
      }
      function parseDateLoose(s) {
        if (!s) return null;
        // Try Date.parse first (handles YYYY-MM-DD natively and is locale-tolerant)
        const d = new Date(s);
        return isNaN(d.getTime()) ? null : d;
      }
      function isPast(d) {
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        return d.getTime() < today.getTime();
      }

      // Reject any Chinese characters in fields configured with noChinese=true
      // (used by the ID-card widget so user enters pinyin only).
      function hasChinese(s) {
        return /[一-鿿]/.test(s || "");
      }

      // Gather review fields
      const reviewValues = {};
      for (const f of reviewFields) {
        const el = msgDiv.querySelector(`[name="review_${f.key}"]`);
        const val = el ? el.value.trim() : "";
        if (f.required && !val) {
          showError(`Please fill in: ${f.label}`);
          return;
        }
        if (val && f.type === "date" && isExpirationKey(f.key)) {
          const d = parseDateLoose(val);
          if (!d) {
            showError(`${f.label} is not a valid date (expected YYYY-MM-DD).`);
            return;
          }
          if (isPast(d)) {
            showError(`This document has expired (${f.label}: ${val}). Please use a current, unexpired document.`);
            return;
          }
        }
        if (val && f.noChinese && hasChinese(val)) {
          showError(`${f.label} 请使用英文拼音填写，中文字符无法提交。`);
          return;
        }
        reviewValues[f.key] = val;
      }

      // Gather metadata fields
      const metaValues = {};
      for (const f of metadataFields) {
        if (f.type === "radio") {
          const sel = msgDiv.querySelector(`input[name="meta_${f.key}"]:checked`);
          const val = sel ? sel.value : "";
          if (f.required && !val) {
            showError(`Please select: ${f.label}`);
            return;
          }
          metaValues[f.key] = val;
        } else {
          const el = msgDiv.querySelector(`[name="meta_${f.key}"]`);
          const val = el ? el.value.trim() : "";
          if (f.required && !val) {
            showError(`Please fill in: ${f.label}`);
            return;
          }
          if (val && f.type === "date" && isExpirationKey(f.key)) {
            const d = parseDateLoose(val);
            if (!d) {
              showError(`${f.label} is not a valid date (expected YYYY-MM-DD).`);
              return;
            }
            if (isPast(d)) {
              showError(`This document has expired (${f.label}: ${val}). Please use a current, unexpired document.`);
              return;
            }
          }
          metaValues[f.key] = val;
        }
      }

      // Build result
      const result = {
        document_type: documentType,
        files: slots.map((s) => ({
          slot: s.key,
          file_type: s.fileType,
          filename: (uploaded[s.key] || {}).filename || "",
        })),
        ...reviewValues,
        ...metaValues,
      };

      sendAnswer(JSON.stringify(result));
    };

    // Enable Submit immediately for confirmation-only widgets (no upload slots).
    refreshSubmit();
  };
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
