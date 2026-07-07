/**
 * Options widget (single select)
 * Renders clickable option buttons.
 */

function renderOptionsWidget(msgDiv, data, sendAnswer) {
  const { question, options = [], widget_type } = data;

  const optionsHTML = options
    .map(
      (opt, i) =>
        `<button class="widget-option" data-index="${i}">${opt}</button>`,
    )
    .join("");

  msgDiv.innerHTML = `
    <div class="widget-card">
      <p class="widget-question">${question}</p>
      <div class="widget-options">${optionsHTML}</div>
    </div>`;

  const chatLog = document.getElementById("chat-log");
  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;

  const optionBtns = msgDiv.querySelectorAll(".widget-option");
  optionBtns.forEach((btn) => {
    btn.onclick = () => {
      if (msgDiv.classList.contains("frozen")) return;
      btn.classList.add("selected");
      sendAnswer(btn.textContent);
    };
  });
}