/**
 * Date / Time input widget
 * Renders a native date/time picker and sends the formatted value back.
 */

function renderDateWidget(msgDiv, data, sendAnswer) {
  const { question, format: fmt = "date" } = data;

  const inputType =
    fmt === "time" ? "time" : fmt === "datetime" ? "datetime-local" : "date";

  msgDiv.innerHTML = `
    <div class="widget-card">
      <p class="widget-question">${question}</p>
      <div class="widget-date-wrap">
        <span class="widget-date-display">${fmt === "time" ? "--:-- --" : fmt === "datetime" ? "MM/DD/YYYY --:--" : "MM/DD/YYYY"}</span>
        <svg class="widget-date-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="3" y="4" width="18" height="18" rx="2"/>
          <line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/>
          <line x1="3" y1="10" x2="21" y2="10"/>
        </svg>
        <input class="widget-date-input" type="${inputType}" lang="en-US" />
      </div>
      <button class="widget-submit-btn">Submit</button>
    </div>`;

  const chatLog = document.getElementById("chat-log");
  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;

  const wrap = msgDiv.querySelector(".widget-date-wrap");
  const input = msgDiv.querySelector(".widget-date-input");
  const display = msgDiv.querySelector(".widget-date-display");
  const submit = msgDiv.querySelector(".widget-submit-btn");

  function formatUS(value) {
    if (!value) return "";
    if (fmt === "time") {
      const [h, m] = value.split(":");
      const hour = parseInt(h, 10);
      const ampm = hour >= 12 ? "PM" : "AM";
      const h12 = hour % 12 || 12;
      return `${h12}:${m} ${ampm}`;
    }
    if (fmt === "datetime") {
      const [date, time] = value.split("T");
      const [y, mo, d] = date.split("-");
      const [h, mi] = time.split(":");
      const hour = parseInt(h, 10);
      const ampm = hour >= 12 ? "PM" : "AM";
      const h12 = hour % 12 || 12;
      return `${mo}/${d}/${y} ${h12}:${mi} ${ampm}`;
    }
    const [y, mo, dd] = value.split("-");
    return `${mo}/${dd}/${y}`;
  }

  wrap.onclick = () => {
    if (msgDiv.classList.contains("frozen")) return;
    try {
      input.focus();
      if (typeof input.showPicker === "function") {
        input.showPicker();
      } else {
        input.click();
      }
    } catch (err) {
      input.click();
    }
  };

  input.onchange = () => {
    display.textContent = formatUS(input.value) || display.textContent;
  };

  submit.onclick = () => {
    if (msgDiv.classList.contains("frozen") || !input.value) return;
    sendAnswer(formatUS(input.value));
  };
}