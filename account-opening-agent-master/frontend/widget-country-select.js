/**
 * Country select widget — single fuzzy-search input.
 *
 * One <input> drives both filtering and selection. As the user types, a
 * suggestion list appears below; clicking (or pressing Enter on the highlighted
 * row) commits the pick. The widget submits the ISO 3166-1 alpha-3 code.
 *
 * Reuses window.ISO_COUNTRIES exposed by widget-address.js.
 *
 * data:  { question, field_key?, default_country? }
 * answer: ISO α-3 string (e.g. "CHN")
 */
function renderCountrySelectWidget(msgDiv, data, sendAnswer) {
  const { question, default_country = "" } = data;
  const countries = (typeof window !== "undefined" && window.ISO_COUNTRIES) || [];
  const defaultCC = (default_country || "").toUpperCase();
  const defaultMatch = countries.find(c => c.code === defaultCC);
  const initialText = defaultMatch ? `${defaultMatch.name} (${defaultMatch.code})` : "";

  msgDiv.innerHTML = `
    <div class="widget-card widget-country-card">
      <p class="widget-question">${question}</p>
      <div class="widget-country-form">
        <div class="widget-country-combo">
          <input class="widget-address-input" id="countryInput" type="text"
            placeholder="Type a country name or ISO code (e.g. China, CHN)"
            autocomplete="off" spellcheck="false"
            value="${initialText}" />
          <ul class="widget-country-suggest hidden" id="countrySuggest"></ul>
        </div>
        <div class="widget-address-error hidden" id="countryError"></div>
      </div>
      <button class="widget-submit-btn" id="countrySubmitBtn">Submit</button>
    </div>`;

  const chatLog = document.getElementById("chat-log");
  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;

  const inputEl = msgDiv.querySelector("#countryInput");
  const suggestEl = msgDiv.querySelector("#countrySuggest");
  const errorDiv = msgDiv.querySelector("#countryError");
  const submitBtn = msgDiv.querySelector("#countrySubmitBtn");

  // selectedCode is the source of truth — set when the user picks a suggestion
  // or when input.value exactly matches a country.
  let selectedCode = defaultMatch ? defaultMatch.code : "";
  let activeIdx = -1;
  let currentMatches = [];

  function showError(msg) { errorDiv.textContent = msg; errorDiv.classList.remove("hidden"); }
  function clearError()  { errorDiv.classList.add("hidden"); }
  function hideSuggest() { suggestEl.classList.add("hidden"); activeIdx = -1; }

  function renderSuggestions(matches) {
    if (!matches.length) { hideSuggest(); return; }
    suggestEl.innerHTML = matches.map((c, i) =>
      `<li class="widget-country-suggest-item${i === activeIdx ? " active" : ""}"
           data-code="${c.code}" data-idx="${i}">
         <span class="cs-name">${c.name}</span>
         <span class="cs-code">${c.code}</span>
       </li>`
    ).join("");
    suggestEl.classList.remove("hidden");
  }

  function filterAndShow() {
    const q = inputEl.value.trim().toLowerCase();
    if (!q) {
      currentMatches = countries.slice(0, 8);
    } else {
      currentMatches = countries
        .filter(c => c.name.toLowerCase().includes(q) || c.code.toLowerCase().includes(q))
        .slice(0, 8);
    }
    activeIdx = currentMatches.length ? 0 : -1;
    renderSuggestions(currentMatches);

    // Re-resolve selectedCode against the current input — if the user edited
    // the text away from the previously selected country, clear the selection.
    const exact = countries.find(c =>
      `${c.name} (${c.code})`.toLowerCase() === inputEl.value.trim().toLowerCase()
      || c.code.toLowerCase() === inputEl.value.trim().toLowerCase()
    );
    selectedCode = exact ? exact.code : "";
  }

  function commit(country) {
    if (!country) return;
    selectedCode = country.code;
    inputEl.value = `${country.name} (${country.code})`;
    hideSuggest();
    clearError();
  }

  inputEl.addEventListener("focus", filterAndShow);
  inputEl.addEventListener("input", filterAndShow);

  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!currentMatches.length) return;
      activeIdx = (activeIdx + 1) % currentMatches.length;
      renderSuggestions(currentMatches);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (!currentMatches.length) return;
      activeIdx = (activeIdx - 1 + currentMatches.length) % currentMatches.length;
      renderSuggestions(currentMatches);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (activeIdx >= 0 && currentMatches[activeIdx]) {
        commit(currentMatches[activeIdx]);
      } else {
        submitBtn.click();
      }
    } else if (e.key === "Escape") {
      hideSuggest();
    }
  });

  suggestEl.addEventListener("mousedown", (e) => {
    // mousedown (not click) so we win over input blur
    const li = e.target.closest(".widget-country-suggest-item");
    if (!li) return;
    e.preventDefault();
    const code = li.getAttribute("data-code");
    const country = countries.find(c => c.code === code);
    commit(country);
  });

  // Close suggestions when focus leaves the combo.
  inputEl.addEventListener("blur", () => {
    // Delay so a click on a suggestion can still register.
    setTimeout(hideSuggest, 120);
  });

  submitBtn.onclick = () => {
    if (msgDiv.classList.contains("frozen")) return;
    clearError();
    if (!selectedCode) {
      // Last-ditch: try to resolve current text to a unique country.
      const q = inputEl.value.trim().toLowerCase();
      const matches = countries.filter(c =>
        c.name.toLowerCase() === q || c.code.toLowerCase() === q
      );
      if (matches.length === 1) {
        commit(matches[0]);
      } else {
        showError("Please pick a country from the suggestions.");
        return;
      }
    }
    sendAnswer(selectedCode);
  };
}
