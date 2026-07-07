/**
 * Investment Profile widget — all 5 FINRA Rule 2111 suitability questions in
 * one form. Replaces the previous "5 × present_options" sequence.
 *
 * sendAnswer payload (all 5 fields as enum strings):
 *   {
 *     investment_experience: 'EXTENSIVE'|'GOOD'|'LIMITED'|'NONE',
 *     investment_objective:  'GROWTH'|'INCOME'|'CAPITAL_PRESERVATION'|'SPECULATION',
 *     time_horizon:          'SHORT'|'AVERAGE'|'LONGEST',
 *     risk_tolerance:        'HIGH'|'MEDIUM'|'LOW',
 *     liquidity_needs:       'VERY_IMPORTANT'|'SOMEWHAT_IMPORTANT'|'NOT_IMPORTANT',
 *   }
 */

const INVESTMENT_PROFILE_QUESTIONS = [
  {
    key: "investment_experience",
    label: "Investment experience",
    options: [
      { value: "EXTENSIVE", label: "Extensive" },
      { value: "GOOD",      label: "Good" },
      { value: "LIMITED",   label: "Limited" },
      { value: "NONE",      label: "None" },
    ],
  },
  {
    key: "investment_objective",
    label: "Investment objective",
    options: [
      { value: "GROWTH",               label: "Growth" },
      { value: "INCOME",               label: "Income" },
      { value: "CAPITAL_PRESERVATION", label: "Capital Preservation" },
      { value: "SPECULATION",          label: "Speculation" },
    ],
  },
  {
    key: "time_horizon",
    label: "Time horizon",
    options: [
      { value: "SHORT",   label: "Short" },
      { value: "AVERAGE", label: "Average" },
      { value: "LONGEST", label: "Long" },
    ],
  },
  {
    key: "risk_tolerance",
    label: "Risk tolerance",
    options: [
      { value: "HIGH",   label: "High" },
      { value: "MEDIUM", label: "Medium" },
      { value: "LOW",    label: "Low" },
    ],
  },
  {
    key: "liquidity_needs",
    label: "Liquidity needs",
    options: [
      { value: "VERY_IMPORTANT",     label: "Very important" },
      { value: "SOMEWHAT_IMPORTANT", label: "Somewhat important" },
      { value: "NOT_IMPORTANT",      label: "Not important" },
    ],
  },
];

function renderInvestmentProfileWidget(msgDiv, data, sendAnswer) {
  const { question } = data;

  const rowsHtml = INVESTMENT_PROFILE_QUESTIONS.map((q) => `
    <div class="widget-ip-row" data-key="${q.key}">
      <div class="widget-ip-label">${q.label}</div>
      <div class="widget-ip-options">
        ${q.options.map(o => `
          <button class="widget-ip-option" data-key="${q.key}" data-value="${o.value}">
            ${o.label}
          </button>`).join("")}
      </div>
    </div>
  `).join("");

  msgDiv.innerHTML = `
    <div class="widget-card widget-ip-card">
      <p class="widget-question">${question || "Investment Profile"}</p>
      <div class="widget-ip-form">
        ${rowsHtml}
        <div class="widget-address-error hidden" id="ipError"></div>
      </div>
      <button class="widget-submit-btn" id="ipSubmitBtn" disabled>Submit</button>
    </div>`;

  const chatLog = document.getElementById("chat-log");
  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;

  const errorDiv = msgDiv.querySelector("#ipError");
  const submitBtn = msgDiv.querySelector("#ipSubmitBtn");
  const selections = {};

  function showError(m) { errorDiv.textContent = m; errorDiv.classList.remove("hidden"); }
  function clearError() { errorDiv.classList.add("hidden"); }

  function refreshSubmitState() {
    submitBtn.disabled = INVESTMENT_PROFILE_QUESTIONS.some(q => !selections[q.key]);
  }

  msgDiv.querySelectorAll(".widget-ip-option").forEach((btn) => {
    btn.onclick = () => {
      if (msgDiv.classList.contains("frozen")) return;
      const key = btn.dataset.key;
      const value = btn.dataset.value;
      // Deselect any other option in the same row
      msgDiv.querySelectorAll(`.widget-ip-option[data-key="${key}"]`).forEach(b => b.classList.remove("selected"));
      btn.classList.add("selected");
      selections[key] = value;
      clearError();
      refreshSubmitState();
    };
  });

  submitBtn.onclick = () => {
    if (msgDiv.classList.contains("frozen")) return;
    const missing = INVESTMENT_PROFILE_QUESTIONS.filter(q => !selections[q.key]).map(q => q.label);
    if (missing.length) {
      showError("Please answer: " + missing.join(", "));
      return;
    }
    sendAnswer(JSON.stringify(selections));
  };
}
