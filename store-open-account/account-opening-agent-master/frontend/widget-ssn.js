/**
 * SSN input widget
 * Renders a masked 9-digit SSN input (XXX-XX-XXXX).
 */

function renderSsnWidget(msgDiv, data, sendAnswer) {
  const { question } = data;

  msgDiv.innerHTML = `
    <div class="widget-card widget-ssn-card">
      <p class="widget-question">${question}</p>
      <div class="widget-ssn-input-row" id="ssnInputRow">
        <input class="widget-ssn-group" id="ssnGroup1" type="text"
          inputmode="numeric" pattern="[0-9]*" maxlength="3" placeholder="XXX" />
        <span class="widget-ssn-dash">-</span>
        <input class="widget-ssn-group" id="ssnGroup2" type="text"
          inputmode="numeric" pattern="[0-9]*" maxlength="2" placeholder="XX" />
        <span class="widget-ssn-dash">-</span>
        <input class="widget-ssn-group" id="ssnGroup3" type="text"
          inputmode="numeric" pattern="[0-9]*" maxlength="4" placeholder="XXXX" />
      </div>
      <div class="widget-ssn-error hidden" id="ssnError"></div>
    </div>`;

  const chatLog = document.getElementById("chat-log");
  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;

  const group1 = msgDiv.querySelector("#ssnGroup1");
  const group2 = msgDiv.querySelector("#ssnGroup2");
  const group3 = msgDiv.querySelector("#ssnGroup3");
  const errorDiv = msgDiv.querySelector("#ssnError");

  function showError(msg) {
    errorDiv.textContent = msg;
    errorDiv.classList.remove("hidden");
  }

  function clearError() {
    errorDiv.classList.add("hidden");
  }

  // Auto-advance focus when group is filled
  group1.addEventListener("input", () => {
    if (group1.value.length === 3) group2.focus();
  });
  group2.addEventListener("input", () => {
    if (group2.value.length === 2) group3.focus();
  });

  // Allow backspace to go back
  group2.addEventListener("keydown", (e) => {
    if (e.key === "Backspace" && group2.value === "") group1.focus();
  });
  group3.addEventListener("keydown", (e) => {
    if (e.key === "Backspace" && group3.value === "") group2.focus();
  });

  function submitIfComplete() {
    if (msgDiv.classList.contains("frozen")) return;
    clearError();

    const g1 = group1.value.trim();
    const g2 = group2.value.trim();
    const g3 = group3.value.trim();

    if (g1.length !== 3 || g2.length !== 2 || g3.length !== 4) {
      showError("Please enter your full 9-digit SSN.");
      return;
    }

    sendAnswer(g1 + "-" + g2 + "-" + g3);
  }

  group3.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitIfComplete();
  });

  // Auto-submit when last group is filled
  group3.addEventListener("input", () => {
    if (group3.value.length === 4) {
      submitIfComplete();
    }
  });
}