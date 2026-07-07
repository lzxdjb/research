/**
 * Address input widget — dual mode.
 *
 * mode='US' (default, also used for the deprecated `present_us_address_input` tool):
 *   - State is a 50-state + 6-territory dropdown
 *   - ZIP must match 5 digits or 5+4
 *   - PO Box rejected
 *
 * mode='INTERNATIONAL':
 *   - Country is an ISO α-3 dropdown (searchable via the native <select> + <datalist>)
 *   - State / postal_code are free-form text
 *   - PO Box check skipped
 *
 * sendAnswer is invoked with a JSON string carrying:
 *   { mode, country, state, city, postal_code, street_address1, street_address2 }
 */

// ISO 3166-1 alpha-3 country codes (subset of widely-used countries — extend as
// needed). Kept inline so the widget renders synchronously without a network
// fetch. `USA` is included so users on the US-territories cases still resolve.
const ISO_COUNTRIES = [
  { code: "USA", name: "United States" },
  { code: "CHN", name: "China" },
  { code: "JPN", name: "Japan" },
  { code: "KOR", name: "South Korea" },
  { code: "SGP", name: "Singapore" },
  { code: "MYS", name: "Malaysia" },
  { code: "IDN", name: "Indonesia" },
  { code: "THA", name: "Thailand" },
  { code: "VNM", name: "Vietnam" },
  { code: "PHL", name: "Philippines" },
  { code: "IND", name: "India" },
  { code: "PAK", name: "Pakistan" },
  { code: "BGD", name: "Bangladesh" },
  { code: "AUS", name: "Australia" },
  { code: "NZL", name: "New Zealand" },
  { code: "CAN", name: "Canada" },
  { code: "MEX", name: "Mexico" },
  { code: "BRA", name: "Brazil" },
  { code: "ARG", name: "Argentina" },
  { code: "CHL", name: "Chile" },
  { code: "COL", name: "Colombia" },
  { code: "GBR", name: "United Kingdom" },
  { code: "FRA", name: "France" },
  { code: "DEU", name: "Germany" },
  { code: "ITA", name: "Italy" },
  { code: "ESP", name: "Spain" },
  { code: "PRT", name: "Portugal" },
  { code: "NLD", name: "Netherlands" },
  { code: "BEL", name: "Belgium" },
  { code: "CHE", name: "Switzerland" },
  { code: "AUT", name: "Austria" },
  { code: "SWE", name: "Sweden" },
  { code: "NOR", name: "Norway" },
  { code: "DNK", name: "Denmark" },
  { code: "FIN", name: "Finland" },
  { code: "IRL", name: "Ireland" },
  { code: "POL", name: "Poland" },
  { code: "CZE", name: "Czech Republic" },
  { code: "RUS", name: "Russia" },
  { code: "TUR", name: "Turkey" },
  { code: "ISR", name: "Israel" },
  { code: "ARE", name: "United Arab Emirates" },
  { code: "SAU", name: "Saudi Arabia" },
  { code: "QAT", name: "Qatar" },
  { code: "EGY", name: "Egypt" },
  { code: "ZAF", name: "South Africa" },
  { code: "NGA", name: "Nigeria" },
  { code: "KEN", name: "Kenya" },
];

const US_STATES = [
  { code: "AL", name: "Alabama" }, { code: "AK", name: "Alaska" },
  { code: "AZ", name: "Arizona" }, { code: "AR", name: "Arkansas" },
  { code: "CA", name: "California" }, { code: "CO", name: "Colorado" },
  { code: "CT", name: "Connecticut" }, { code: "DE", name: "Delaware" },
  { code: "FL", name: "Florida" }, { code: "GA", name: "Georgia" },
  { code: "HI", name: "Hawaii" }, { code: "ID", name: "Idaho" },
  { code: "IL", name: "Illinois" }, { code: "IN", name: "Indiana" },
  { code: "IA", name: "Iowa" }, { code: "KS", name: "Kansas" },
  { code: "KY", name: "Kentucky" }, { code: "LA", name: "Louisiana" },
  { code: "ME", name: "Maine" }, { code: "MD", name: "Maryland" },
  { code: "MA", name: "Massachusetts" }, { code: "MI", name: "Michigan" },
  { code: "MN", name: "Minnesota" }, { code: "MS", name: "Mississippi" },
  { code: "MO", name: "Missouri" }, { code: "MT", name: "Montana" },
  { code: "NE", name: "Nebraska" }, { code: "NV", name: "Nevada" },
  { code: "NH", name: "New Hampshire" }, { code: "NJ", name: "New Jersey" },
  { code: "NM", name: "New Mexico" }, { code: "NY", name: "New York" },
  { code: "NC", name: "North Carolina" }, { code: "ND", name: "North Dakota" },
  { code: "OH", name: "Ohio" }, { code: "OK", name: "Oklahoma" },
  { code: "OR", name: "Oregon" }, { code: "PA", name: "Pennsylvania" },
  { code: "RI", name: "Rhode Island" }, { code: "SC", name: "South Carolina" },
  { code: "SD", name: "South Dakota" }, { code: "TN", name: "Tennessee" },
  { code: "TX", name: "Texas" }, { code: "UT", name: "Utah" },
  { code: "VT", name: "Vermont" }, { code: "VA", name: "Virginia" },
  { code: "WA", name: "Washington" }, { code: "WV", name: "West Virginia" },
  { code: "WI", name: "Wisconsin" }, { code: "WY", name: "Wyoming" },
  { code: "DC", name: "District of Columbia" }, { code: "PR", name: "Puerto Rico" },
  { code: "GU", name: "Guam" }, { code: "VI", name: "Virgin Islands" },
  { code: "AS", name: "American Samoa" }, { code: "MP", name: "Northern Mariana Islands" },
];

function renderAddressWidget(msgDiv, data, sendAnswer) {
  const { question, prefill = {} } = data;
  const mode = (data.mode || "US").toUpperCase();
  const isUS = mode !== "INTERNATIONAL";

  // Build the state/region input — dropdown in US mode, free text in INTERNATIONAL.
  const stateInputHtml = isUS
    ? `<select class="widget-address-select" id="addrState">
         <option value="">State</option>
         ${US_STATES.map(s => `<option value="${s.code}" ${prefill.state === s.code ? "selected" : ""}>${s.name} (${s.code})</option>`).join("")}
       </select>`
    : `<input class="widget-address-input" id="addrState" type="text"
         placeholder="State / Province / Region" maxlength="50"
         value="${prefill.state || ""}" />`;

  // Country selector — only shown in INTERNATIONAL mode (US mode is implicitly USA).
  const countryHtml = isUS
    ? ""
    : `<div class="widget-address-row">
         <select class="widget-address-select" id="addrCountry">
           <option value="">Country</option>
           ${ISO_COUNTRIES.map(c => `<option value="${c.code}" ${(prefill.country || "").toUpperCase() === c.code ? "selected" : ""}>${c.name} (${c.code})</option>`).join("")}
         </select>
       </div>`;

  const zipPlaceholder = isUS ? "ZIP Code" : "Postal Code";

  msgDiv.innerHTML = `
    <div class="widget-card widget-address-card">
      <p class="widget-question">${question}</p>
      <div class="widget-address-form">
        ${countryHtml}
        <div class="widget-address-row">
          <input class="widget-address-input" id="addrStreet1" type="text"
            placeholder="Street Address" maxlength="100"
            value="${prefill.street_address1 || ""}" />
        </div>
        <div class="widget-address-row">
          <input class="widget-address-input" id="addrStreet2" type="text"
            placeholder="Apt, Suite, Unit (optional)" maxlength="100"
            value="${prefill.street_address2 || ""}" />
        </div>
        <div class="widget-address-row">
          <input class="widget-address-input" id="addrCity" type="text"
            placeholder="City" maxlength="50"
            value="${prefill.city || ""}" />
        </div>
        <div class="widget-address-row-split">
          ${stateInputHtml}
          <input class="widget-address-input" id="addrZip" type="text"
            placeholder="${zipPlaceholder}" maxlength="20"
            value="${prefill.postal_code || ""}" />
        </div>
        <div class="widget-address-error hidden" id="addrError"></div>
      </div>
      <button class="widget-submit-btn" id="addrSubmitBtn">Submit</button>
    </div>`;

  const chatLog = document.getElementById("chat-log");
  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;

  const country = msgDiv.querySelector("#addrCountry");
  const street1 = msgDiv.querySelector("#addrStreet1");
  const street2 = msgDiv.querySelector("#addrStreet2");
  const city = msgDiv.querySelector("#addrCity");
  const state = msgDiv.querySelector("#addrState");
  const zip = msgDiv.querySelector("#addrZip");
  const errorDiv = msgDiv.querySelector("#addrError");
  const submitBtn = msgDiv.querySelector("#addrSubmitBtn");

  function showError(msg) {
    errorDiv.textContent = msg;
    errorDiv.classList.remove("hidden");
  }
  function clearError() {
    errorDiv.classList.add("hidden");
  }

  function validateUsZip(val) {
    return /^\d{5}(-\d{4})?$/.test(val);
  }

  function isPoBox(val) {
    const patterns = [
      /^\s*p\.?\s*o\.?\s*box\s*\d*\s*$/i,
      /^\s*post\s*office\s*box\s*\d*\s*$/i,
      /^box\s+\d+$/i,
    ];
    return patterns.some(p => p.test(val));
  }

  submitBtn.onclick = () => {
    if (msgDiv.classList.contains("frozen")) return;
    clearError();

    const s1 = street1.value.trim();
    const s2 = street2.value.trim();
    const c = city.value.trim();
    const st = (state.value || "").trim();
    const z = zip.value.trim();
    const cc = isUS ? "USA" : ((country && country.value) || "").trim();

    if (!isUS && !cc) { showError("Country is required."); return; }
    if (!s1) { showError("Street address is required."); return; }
    if (isUS && isPoBox(s1)) { showError("PO Boxes are not accepted. Please provide a residential address."); return; }
    if (!c) { showError("City is required."); return; }
    if (!st) { showError(isUS ? "State is required." : "State / Province / Region is required."); return; }
    if (!z) { showError(isUS ? "ZIP code is required." : "Postal code is required."); return; }
    if (isUS && !validateUsZip(z)) { showError("Please enter a valid US ZIP code (5 digits or 5+4)."); return; }

    sendAnswer(JSON.stringify({
      mode,
      country: cc,
      state: st,
      city: c,
      postal_code: z,
      street_address1: s1,
      street_address2: s2,
    }));
  };
}

// Backwards-compatible alias for any legacy dispatcher still calling the old name.
function renderUsAddressWidget(msgDiv, data, sendAnswer) {
  return renderAddressWidget(msgDiv, { ...data, mode: "US" }, sendAnswer);
}

// Expose the ISO α-3 country list so sibling widgets (passport, tax-id, etc.)
// can reuse it without duplicating ~50 entries each.
if (typeof window !== "undefined") {
  window.ISO_COUNTRIES = ISO_COUNTRIES;
}
