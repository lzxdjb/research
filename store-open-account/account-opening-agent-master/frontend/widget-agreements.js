/**
 * Agreements widget
 * Displays a list of required agreements (name + PDF link) and an "I Agree" confirm button.
 */

function renderAgreementsWidget(msgDiv, data, sendAnswer) {
  const { question = "Please review and accept the following agreements to continue.", agreements = [] } = data;

  const rowsHTML = agreements.map((a) => `
    <div class="agreement-row">
      <span class="agreement-name">${a.name}</span>
      <a class="agreement-link" href="${a.url}" target="_blank" rel="noopener noreferrer">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
          <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
          <polyline points="15 3 21 3 21 9"/>
          <line x1="10" y1="14" x2="21" y2="3"/>
        </svg>
        View
      </a>
    </div>`).join("");

  msgDiv.innerHTML = `
    <div class="widget-card widget-agreements-card">
      <p class="widget-question">${question}</p>
      <div class="agreements-list">${rowsHTML}</div>
      <label class="agreements-checkbox-label">
        <input type="checkbox" class="agreements-checkbox" />
        <span>I have read and agree to all the above agreements</span>
      </label>
      <div class="agreements-error hidden">Please check the box to confirm you have read all agreements.</div>
      <button class="widget-submit-btn agreements-confirm-btn" disabled>I Agree</button>
    </div>`;

  const chatLog = document.getElementById("chat-log");
  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;

  const checkbox = msgDiv.querySelector(".agreements-checkbox");
  const confirmBtn = msgDiv.querySelector(".agreements-confirm-btn");
  const errorDiv = msgDiv.querySelector(".agreements-error");

  checkbox.onchange = () => {
    confirmBtn.disabled = !checkbox.checked;
    if (checkbox.checked) errorDiv.classList.add("hidden");
  };

  confirmBtn.onclick = () => {
    if (msgDiv.classList.contains("frozen")) return;
    if (!checkbox.checked) {
      errorDiv.classList.remove("hidden");
      return;
    }
    msgDiv.classList.add("frozen");
    confirmBtn.disabled = true;
    checkbox.disabled = true;
    sendAnswer("I have read and agreed to all required agreements.");
  };
}
