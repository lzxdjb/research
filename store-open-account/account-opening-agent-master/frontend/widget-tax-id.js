/**
 * Tax ID input widget (FOREIGNER branch).
 * Like widget-ssn.js but without the 9-digit US format constraint.
 * Includes an issuing-country dropdown that defaults to data.default_country.
 *
 * sendAnswer payload: { tax_id, tax_id_country }
 */
function renderTaxIdWidget(msgDiv, data, sendAnswer) {
  const { question, default_country = "" } = data;
  const countries = (typeof window !== "undefined" && window.ISO_COUNTRIES) || [];
  const defaultCC = (default_country || "").toUpperCase();

  msgDiv.innerHTML = `
    <div class="widget-card widget-ssn-card widget-tax-id-card">
      <p class="widget-question">${question}</p>
      <div class="widget-tax-id-form">
        <div class="widget-tax-id-row">
          <label class="widget-tax-id-label">Issuing country</label>
          <select class="widget-address-select" id="taxIdCountry">
            <option value="">Select country</option>
            ${countries.map(c => `<option value="${c.code}" ${defaultCC === c.code ? "selected" : ""}>${c.name} (${c.code})</option>`).join("")}
          </select>
        </div>
        <div class="widget-tax-id-row">
          <label class="widget-tax-id-label">Tax ID</label>
          <input class="widget-address-input" id="taxIdValue" type="text"
            placeholder="Your home-country tax identifier" maxlength="40"
            autocomplete="off" />
        </div>
        <div class="widget-address-error hidden" id="taxIdError"></div>
      </div>
      <button class="widget-submit-btn" id="taxIdSubmitBtn">Submit</button>
    </div>`;

  const chatLog = document.getElementById("chat-log");
  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;

  const countrySel = msgDiv.querySelector("#taxIdCountry");
  const idInput = msgDiv.querySelector("#taxIdValue");
  const errorDiv = msgDiv.querySelector("#taxIdError");
  const submitBtn = msgDiv.querySelector("#taxIdSubmitBtn");

  function showError(msg) {
    errorDiv.textContent = msg;
    errorDiv.classList.remove("hidden");
  }
  function clearError() {
    errorDiv.classList.add("hidden");
  }

  submitBtn.onclick = () => {
    if (msgDiv.classList.contains("frozen")) return;
    clearError();
    const cc = countrySel.value;
    const tid = idInput.value.trim();
    if (!cc) { showError("Please pick the country that issued your tax ID."); return; }
    if (!tid) { showError("Tax ID cannot be empty."); return; }
    if (tid.length < 4) { showError("Tax ID looks too short."); return; }
    sendAnswer(JSON.stringify({ tax_id: tid, tax_id_country: cc }));
  };

  idInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitBtn.click();
  });
}
