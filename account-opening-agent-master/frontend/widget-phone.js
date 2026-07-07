/**
 * Phone input widget
 * Renders country code selector + phone number input, sends verification code on submit.
 */

function renderPhoneWidget(msgDiv, data, sendAnswer) {
  console.log("[PhoneWidget] renderPhoneWidget called with:", data);
  const { question } = data;

  const areaCodes = [
    { label: "+1 (US)", value: "1" },
    { label: "+44 (UK)", value: "44" },
    { label: "+86 (CN)", value: "86" },
    { label: "+81 (JP)", value: "81" },
    { label: "+82 (KR)", value: "82" },
    { label: "+65 (SG)", value: "65" },
    { label: "+61 (AU)", value: "61" },
    { label: "+33 (FR)", value: "33" },
    { label: "+49 (DE)", value: "49" },
  ];

  const areaCodeOptions = areaCodes
    .map((opt) => `<option value="${opt.value}">${opt.label}</option>`)
    .join("");

  msgDiv.innerHTML = `
    <div class="widget-card widget-phone-card">
      <p class="widget-question">${question}</p>
      <div class="widget-phone-row">
        <select class="widget-phone-area-code widget-phone-input" id="areaCodeSelect">
          ${areaCodeOptions}
        </select>
        <input
          type="tel"
          class="widget-phone-number widget-phone-input"
          id="phoneNumberInput"
          placeholder="Enter phone number"
          maxlength="20"
        />
      </div>
      <button class="widget-submit-btn" id="phoneSendCodeBtn">Send Code</button>
    </div>`;

  const chatLog = document.getElementById("chat-log");
  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;

  const areaCodeSelect = msgDiv.querySelector("#areaCodeSelect");
  const phoneInput = msgDiv.querySelector("#phoneNumberInput");
  const sendCodeBtn = msgDiv.querySelector("#phoneSendCodeBtn");

  console.log("[PhoneWidget] elements found:", { areaCodeSelect, phoneInput, sendCodeBtn });
  console.log("[PhoneWidget] msgDiv innerHTML:", msgDiv.innerHTML);

  sendCodeBtn.onclick = () => {
    if (msgDiv.classList.contains("frozen")) return;
    const phone = phoneInput.value.trim();
    if (!phone) return;

    const areaCode = areaCodeSelect.value;
    const fullPhone = `+${areaCode} ${phone}`;
    sendAnswer(fullPhone);
  };
}