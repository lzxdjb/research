/**
 * Employment input widget
 * Renders employment information fields: employer, position, years, industry.
 */

function renderEmploymentWidget(msgDiv, data, sendAnswer) {
  const { question, prefill = {} } = data;

  const industries = [
    "Agriculture",
    "Business Management",
    "Construction",
    "Education",
    "Environmental",
    "Finance",
    "Food & Hospitality",
    "Gaming",
    "Health Services",
    "Information Technology",
    "Insurance",
    "Legal",
    "Motor Vehicle",
    "Real Estate",
    "Security",
    "Telecom",
    "Transportation",
    "Utilities",
    "Other",
  ];

  const industryOptions = industries
    .map(
      (ind) =>
        `<option value="${ind}" ${prefill.industry === ind ? "selected" : ""}>${ind}</option>`,
    )
    .join("");

  msgDiv.innerHTML = `
    <div class="widget-card widget-employment-card">
      <p class="widget-question">${question}</p>
      <div class="widget-employment-form">
        <div class="widget-employment-row">
          <input class="widget-employment-input" id="empEmployer" type="text"
            placeholder="Employer / Company Name" maxlength="100"
            value="${prefill.employer || ""}" />
        </div>
        <div class="widget-employment-row">
          <input class="widget-employment-input" id="empPosition" type="text"
            placeholder="Job Title / Position" maxlength="100"
            value="${prefill.position_employed || ""}" />
        </div>
        <div class="widget-employment-row-split">
          <input class="widget-employment-input widget-employment-years" id="empYears" type="number"
            placeholder="Years" maxlength="2" min="0" max="50"
            value="${prefill.years_employed !== undefined ? prefill.years_employed : ""}" />
          <select class="widget-employment-select" id="empIndustry">
            <option value="">Select your industry</option>
            ${industryOptions}
          </select>
        </div>
        <div class="widget-employment-error hidden" id="empError"></div>
      </div>
      <button class="widget-submit-btn" id="empSubmitBtn">Submit</button>
    </div>`;

  const chatLog = document.getElementById("chat-log");
  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;

  const employerInput = msgDiv.querySelector("#empEmployer");
  const positionInput = msgDiv.querySelector("#empPosition");
  const yearsInput = msgDiv.querySelector("#empYears");
  const industrySelect = msgDiv.querySelector("#empIndustry");
  const errorDiv = msgDiv.querySelector("#empError");
  const submitBtn = msgDiv.querySelector("#empSubmitBtn");

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

    const employer = employerInput.value.trim();
    const position = positionInput.value.trim();
    const years =
      yearsInput.value === "" ? null : parseInt(yearsInput.value.trim(), 10);
    const industry = industrySelect.value;

    if (!employer) {
      showError("Employer name is required.");
      return;
    }
    if (!position) {
      showError("Job title is required.");
      return;
    }
    if (years === null || isNaN(years)) {
      showError("Years employed is required.");
      return;
    }
    if (!industry) {
      showError("Industry is required.");
      return;
    }

    const result = {
      employer,
      position_employed: position,
      years_employed: years,
      industry,
    };

    sendAnswer(JSON.stringify(result));
  };
}
