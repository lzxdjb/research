/**
 * Personal Info widget
 * Standalone personal identity form — name, DOB, gender only.
 * No address fields, no expiration date.
 */
function renderPersonalInfoWidget(msgDiv, data, sendAnswer) {
  const {
    question = "Please confirm your personal information",
    prefill = {},
    address_prefill = {}
  } = data;

  const defaults = {
    first_name: "",
    middle_name: "",
    last_name: "",
    date_of_birth: "",
    gender: ""
  };

  const f = { ...defaults, ...prefill };

  const html = `
    <div class="widget-card widget-dl-card">
      <p class="widget-question">${question}</p>
      <div class="widget-dl-form">
        <div class="widget-dl-section">
          <div class="widget-dl-section-title">Personal Information</div>
          <div class="widget-dl-row">
            <label class="widget-dl-label">
              <span class="widget-dl-label-text">First Name</span>
              <input class="widget-dl-input" type="text" name="first_name" value="${escapeHtml(f.first_name)}" />
            </label>
            <label class="widget-dl-label">
              <span class="widget-dl-label-text">Middle Name</span>
              <input class="widget-dl-input" type="text" name="middle_name" value="${escapeHtml(f.middle_name)}" />
            </label>
          </div>
          <div class="widget-dl-row">
            <label class="widget-dl-label">
              <span class="widget-dl-label-text">Last Name</span>
              <input class="widget-dl-input" type="text" name="last_name" value="${escapeHtml(f.last_name)}" />
            </label>
            <label class="widget-dl-label">
              <span class="widget-dl-label-text">Date of Birth</span>
              <input class="widget-dl-input" type="text" name="date_of_birth" value="${escapeHtml(f.date_of_birth)}" placeholder="YYYY-MM-DD" />
            </label>
          </div>
          <div class="widget-dl-row">
            <label class="widget-dl-label">
              <span class="widget-dl-label-text">Gender</span>
              <select class="widget-dl-input widget-dl-select" name="gender">
                <option value="MALE" ${f.gender === "MALE" ? "selected" : ""}>Male</option>
                <option value="FEMALE" ${f.gender === "FEMALE" ? "selected" : ""}>Female</option>
                <option value="OTHER" ${f.gender === "OTHER" ? "selected" : ""}>Other</option>
              </select>
            </label>
            <div class="widget-dl-label" style="visibility:hidden">
              <span class="widget-dl-label-text">&nbsp;</span>
              <div class="widget-dl-input" style="border:none">&nbsp;</div>
            </div>
          </div>
        </div>
      </div>
      <button class="widget-submit-btn widget-dl-confirm-btn">Confirm</button>
    </div>`;

  msgDiv.innerHTML = html;

  const chatLog = document.getElementById("chat-log");
  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;

  const confirmBtn = msgDiv.querySelector(".widget-dl-confirm-btn");
  confirmBtn.onclick = () => {
    if (msgDiv.classList.contains("frozen")) return;

    const inputs = msgDiv.querySelectorAll(".widget-dl-input");
    const values = {};
    inputs.forEach(input => {
      if (input.name) values[input.name] = input.value.trim();
    });

    const resultParts = [
      `First Name: ${values.first_name}`,
      `Middle Name: ${values.middle_name}`,
      `Last Name: ${values.last_name}`,
      `Date of Birth: ${values.date_of_birth}`,
      `Gender: ${values.gender}`
    ];

    // Pass through OCR-extracted address data if available
    if (address_prefill && (address_prefill.address_1 || address_prefill.street_address1)) {
      const addr = address_prefill;
      resultParts.push(
        `OCR Address: ${addr.address_1 || addr.street_address1 || ""}${addr.address_2 || addr.street_address2 ? ", " + (addr.address_2 || addr.street_address2) : ""}`,
        `OCR City: ${addr.city || ""}`,
        `OCR State: ${addr.state || ""}`,
        `OCR Zip: ${addr.postal_code || addr.zip_code || ""}`
      );
    }

    sendAnswer(resultParts.join("; "));
  };
}
