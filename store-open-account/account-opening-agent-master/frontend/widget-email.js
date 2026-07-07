/**
 * Email input widget
 * Renders an email input field.
 */

function renderEmailWidget(msgDiv, data, sendAnswer) {
  console.log("[EmailWidget] renderEmailWidget called with:", data);
  const { question } = data;

  msgDiv.innerHTML = `
    <div class="widget-card widget-email-card">
      <p class="widget-question">${question}</p>
      <input
        type="email"
        class="widget-email-input"
        id="emailInput"
        placeholder="Enter your email address"
      />
      <button class="widget-submit-btn" id="emailSendCodeBtn">Send Code</button>
    </div>`;

  const chatLog = document.getElementById("chat-log");
  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;

  const emailInput = msgDiv.querySelector("#emailInput");
  const sendCodeBtn = msgDiv.querySelector("#emailSendCodeBtn");

  console.log("[EmailWidget] elements found:", { emailInput, sendCodeBtn });

  sendCodeBtn.onclick = () => {
    if (msgDiv.classList.contains("frozen")) return;
    const email = emailInput.value.trim();
    if (!email) return;
    sendAnswer(email);
  };
}