/**
 * Disclosure questionnaire widget
 * Renders all 5 regulatory compliance questions in one card.
 */

function renderDisclosureWidget(msgDiv, data, sendAnswer) {
  const questions =
    data.questions ||
    [data].map((q) => ({
      question: q.question,
      disclosure_type: q.disclosure_type || "",
    }));

  msgDiv.innerHTML = `
    <div class="widget-card widget-disclosure-card">
      <p class="widget-question">Disclosure Questions</p>
      <div class="disclosure-all-questions"></div>
      <div class="disclosure-error hidden"></div>
      <button class="widget-submit-btn">Submit All</button>
    </div>`;

  const chatLog = document.getElementById("chat-log");
  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;

  const container = msgDiv.querySelector(".disclosure-all-questions");
  const submitBtn = msgDiv.querySelector(".widget-submit-btn");
  const errorDiv = msgDiv.querySelector(".disclosure-error");

  const followupFields = {
    company: [
      { name: "companySymbols", placeholder: "Company symbols (e.g. AAPL, GOOGL)", type: "text", required: false },
    ],
    broker: [
      { name: "firmName", placeholder: "Firm name", type: "text", required: true },
      { name: "affiliatedApproval", placeholder: "Upload approval document", type: "file", required: true },
    ],
    political: [
      { name: "immediateFamily", placeholder: "Immediate family member names", type: "text", required: false },
      { name: "politicalOrganization", placeholder: "Political organization", type: "text", required: false },
    ],
    contact: [
      { name: "contactName", placeholder: "Contact person name *", type: "text", required: true },
      { name: "contactPhone", placeholder: "Phone number *", type: "tel", required: true },
      { name: "contactEmail", placeholder: "Email address *", type: "email", required: true },
      { name: "contactBirthdate", placeholder: "Date of birth (MM/DD/YYYY) *", type: "date", required: true },
      { name: "contactAddress", placeholder: "Address *", type: "text", required: true },
      { name: "contactRelationship", placeholder: "Relationship to you *", type: "text", required: true },
    ],
  };

  function buildFollowupHTML(fields) {
    return fields
      .map((field) => {
        if (field.type === "file") {
          return `<div class="widget-followup-item widget-followup-item--file">
            <label class="widget-followup-label">${field.placeholder}</label>
            <div class="widget-file-upload-wrap">
              <input type="file" class="widget-followup-file" name="${field.name}" accept="image/*" ${field.required ? "required" : ""} />
              <div class="widget-file-upload-status hidden"></div>
            </div>
          </div>`;
        }
        return `<div class="widget-followup-item">
          <input type="${field.type}" class="widget-followup-input"
            name="${field.name}" placeholder="${field.placeholder}" ${field.required ? "required" : ""} />
        </div>`;
      })
      .join("");
  }

  async function uploadFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = async (e) => {
        try {
          const formData = new FormData();
          formData.append("file", file);
          formData.append("is_need_min", "0");
          const res = await fetch(
            "http://test-lighthorse-trade.touzime.cn/api/oas/v1/application/file/upload",
            {
              method: "POST",
              headers: {
                "X-Auth-AppName": "AINVEST",
                "X-Auth-ProgId": "7047",
                "X-Platform": "web",
              },
              body: formData,
            },
          );
          const result = await res.json();
          if (result.s === "ok" && result.d?.fileUrl) {
            resolve(result.d.fileUrl);
          } else {
            reject(new Error("Upload failed"));
          }
        } catch (err) {
          reject(err);
        }
      };
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  }

  function setupFileUpload(inputEl) {
    const wrap = inputEl.closest(".widget-file-upload-wrap");
    const statusEl = wrap ? wrap.querySelector(".widget-file-upload-status") : null;
    inputEl.addEventListener("change", async () => {
      const file = inputEl.files && inputEl.files[0];
      if (!file) return;
      if (statusEl) { statusEl.textContent = "Uploading…"; statusEl.classList.remove("hidden"); }
      try {
        const fileUrl = await uploadFile(file);
        if (statusEl) { statusEl.textContent = "✓ " + file.name; statusEl.dataset.url = fileUrl; }
      } catch (e) {
        if (statusEl) { statusEl.textContent = "Upload failed"; }
      }
    });
  }

  const answers = {};
  const itemNodes = [];

  questions.forEach((q) => {
    const itemDiv = document.createElement("div");
    itemDiv.className = "disclosure-item";
    itemDiv.dataset.type = q.disclosure_type;
    itemDiv.dataset.question = q.question;
    itemDiv.innerHTML = `
      <div class="disclosure-item__title">${q.question}</div>
      <div class="disclosure-item__options">
        <button class="widget-option" data-value="YES">Yes</button>
        <button class="widget-option" data-value="NO">No</button>
      </div>
      <div class="disclosure-item__followup hidden"></div>`;
    container.appendChild(itemDiv);

    const yesBtn = itemDiv.querySelector('[data-value="YES"]');
    const noBtn = itemDiv.querySelector('[data-value="NO"]');
    const followupDiv = itemDiv.querySelector(".disclosure-item__followup");
    const key = q.disclosure_type;

    yesBtn.onclick = () => {
      if (msgDiv.classList.contains("frozen")) return;
      yesBtn.classList.add("selected");
      noBtn.classList.remove("selected");
      answers[key] = "YES";
      errorDiv.classList.add("hidden");
      const fields = followupFields[key] || [];
      if (fields.length > 0) {
        followupDiv.innerHTML = buildFollowupHTML(fields);
        followupDiv.classList.remove("hidden");
        followupDiv.querySelectorAll(".widget-followup-file").forEach(setupFileUpload);
      }
    };

    noBtn.onclick = () => {
      if (msgDiv.classList.contains("frozen")) return;
      noBtn.classList.add("selected");
      yesBtn.classList.remove("selected");
      answers[key] = "NO";
      followupDiv.classList.add("hidden");
      followupDiv.innerHTML = "";
    };

    noBtn.classList.add("selected");
    answers[key] = "NO";
    itemNodes.push(itemDiv);
  });

  submitBtn.onclick = async () => {
    if (msgDiv.classList.contains("frozen")) return;

    for (const q of questions) {
      const key = q.disclosure_type;
      if (answers[key] !== "YES") continue;
      const fields = followupFields[key] || [];
      for (const field of fields) {
        if (!field.required) continue;
        const item = itemNodes.find((n) => n.dataset.type === key);
        if (!item) continue;
        const input = item.querySelector(`[name="${field.name}"]`);
        if (!input) continue;
        if (field.type === "file") {
          if (!input.files || input.files.length === 0) {
            errorDiv.textContent = `Please upload: ${field.placeholder}`;
            errorDiv.classList.remove("hidden");
            return;
          }
        } else if (!input.value.trim()) {
          errorDiv.textContent = `Please fill: ${field.placeholder}`;
          errorDiv.classList.remove("hidden");
          input.focus();
          return;
        }
      }
    }

    msgDiv.classList.add("frozen");
    submitBtn.disabled = true;
    errorDiv.classList.add("hidden");

    const parts = [];
    for (const q of questions) {
      const key = q.disclosure_type;
      const answer = answers[key] || "NO";
      if (answer === "YES") {
        const inputs = {};
        const item = itemNodes.find((n) => n.dataset.type === key);
        if (item) {
          for (const input of item.querySelectorAll("input")) {
            if (input.type === "file" && input.files && input.files.length > 0) {
              const wrap = input.closest(".widget-file-upload-wrap");
              const statusEl = wrap ? wrap.querySelector(".widget-file-upload-status") : null;
              const fileUrl = statusEl && statusEl.dataset.url;
              inputs[input.name] = fileUrl || "file not uploaded";
            } else if (input.value) {
              inputs[input.name] = input.value;
            }
          }
        }
        const details = Object.entries(inputs).filter(([_, v]) => v).map(([k, v]) => `${k}: ${v}`).join("; ");
        parts.push(`Q: "${q.question}" - YES${details ? ". Details: " + details : ""}`);
      } else {
        parts.push(`Q: "${q.question}" - NO`);
      }
    }

    const responseText = parts.join(" | ");
    msgDiv.querySelectorAll("button, input").forEach((el) => (el.disabled = true));
    // sendAnswer is injected from appendWidget — calls appendMessage + geminiClient.sendText
    sendAnswer(responseText);
  };
}