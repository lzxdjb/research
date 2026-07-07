/**
 * Financial Range widget — 3 horizontal segmented bars, one per metric.
 *
 * Each bar is a row of clickable segments.  Click a segment → it fills yellow;
 * one selection per row.  Submit enabled when all 3 rows have a selection.
 *
 * sendAnswer payload:
 *   { annual_income: {min,max,label}, liquid_net_worth: {min,max,label},
 *     total_net_worth: {min,max,label} }
 */

const FINANCIAL_BUCKETS = {
  total_net_worth: [
    { label: "$0–50K",       mark: "$0",      min: 0,          max: 50000 },
    { label: "$50K–100K",    mark: "$50K",    min: 50001,      max: 100000 },
    { label: "$100K–200K",   mark: "$100K",   min: 100001,     max: 200000 },
    { label: "$200K–500K",   mark: "$200K",   min: 200001,     max: 500000 },
    { label: "$500K–1M",     mark: "$500K",   min: 500001,     max: 1000000 },
    { label: "$1M–5M",       mark: "$1M",     min: 1000001,    max: 5000000 },
    { label: "$5M+",         mark: "$5M",     min: 5000001,    max: 9999999 },
  ],
  annual_income: [
    { label: "$0–25K",       mark: "$0",      min: 0,          max: 25000 },
    { label: "$25K–50K",     mark: "$25K",    min: 25001,      max: 50000 },
    { label: "$50K–100K",    mark: "$50K",    min: 50001,      max: 100000 },
    { label: "$100K–200K",   mark: "$100K",   min: 100001,     max: 200000 },
    { label: "$200K–300K",   mark: "$200K",   min: 200001,     max: 300000 },
    { label: "$300K–500K",   mark: "$300K",   min: 300001,     max: 500000 },
    { label: "$500K–1.2M",   mark: "$500K",   min: 500001,     max: 1200000 },
    { label: "$1.2M+",       mark: "$1.2M",   min: 1200001,    max: 9999999 },
  ],
  liquid_net_worth: [
    { label: "$0–50K",       mark: "$0",      min: 0,          max: 50000 },
    { label: "$50K–100K",    mark: "$50K",    min: 50001,      max: 100000 },
    { label: "$100K–200K",   mark: "$100K",   min: 100001,     max: 200000 },
    { label: "$200K–500K",   mark: "$200K",   min: 200001,     max: 500000 },
    { label: "$500K–1M",     mark: "$500K",   min: 500001,     max: 1000000 },
    { label: "$1M–5M",       mark: "$1M",     min: 1000001,    max: 5000000 },
    { label: "$5M+",         mark: "$5M",     min: 5000001,    max: 9999999 },
  ],
};

const FINANCIAL_FIELDS = [
  { key: "annual_income",    label: "Annual Income ($)" },
  { key: "liquid_net_worth", label: "Liquid Net Worth ($)" },
  { key: "total_net_worth",  label: "Total Net Worth ($)" },
];

function getBuckets(fieldKey, bucketsParam) {
  if (Array.isArray(bucketsParam) && bucketsParam.length > 0) return bucketsParam;
  if (bucketsParam && typeof bucketsParam === "object" && Array.isArray(bucketsParam[fieldKey]))
    return bucketsParam[fieldKey];
  return FINANCIAL_BUCKETS[fieldKey] || [];
}

function renderFinancialRangeWidget(msgDiv, data, sendAnswer) {
  const { question, buckets } = data;

  const rowsHtml = FINANCIAL_FIELDS.map((f) => {
    const fb = getBuckets(f.key, buckets);
    const n = fb.length;
    return `
    <div class="fr-row" data-key="${f.key}">
      <div class="fr-label">${f.label}</div>
      <div class="fr-bar" data-key="${f.key}">
        ${fb.map((b, i) => `
          <div class="fr-seg" data-key="${f.key}" data-index="${i}"
               style="width:${100 / n}%">
          </div>
        `).join("")}
        ${fb.map((b, i) => `
          <div class="fr-mark" style="left:${(i / Math.max(n - 1, 1)) * 100}%">
            <span class="fr-mark-label">${b.mark || b.label}</span>
          </div>
        `).join("")}
      </div>
      <div class="fr-val" data-key="${f.key}"></div>
    </div>
  `}).join("");

  msgDiv.innerHTML = `
    <div class="widget-card widget-financial-card">
      <p class="widget-question">${question || "Financial Profile"}</p>
      <div class="fr-form">
        ${rowsHtml}
      </div>
      <div class="widget-address-error hidden" id="financialError"></div>
      <button class="widget-submit-btn" id="financialSubmitBtn" disabled>Submit</button>
    </div>`;

  const chatLog = document.getElementById("chat-log");
  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;

  const errorDiv = msgDiv.querySelector("#financialError");
  const submitBtn = msgDiv.querySelector("#financialSubmitBtn");
  const selections = {};

  function showError(m) { errorDiv.textContent = m; errorDiv.classList.remove("hidden"); }
  function clearError() { errorDiv.classList.add("hidden"); }
  function refreshSubmit() { submitBtn.disabled = FINANCIAL_FIELDS.some(f => !selections[f.key]); }

  msgDiv.querySelectorAll(".fr-seg").forEach((seg) => {
    seg.addEventListener("click", () => {
      if (msgDiv.classList.contains("frozen")) return;
      const key = seg.dataset.key;
      const idx = Number(seg.dataset.index);
      const bucket = getBuckets(key, buckets)[idx];
      if (!bucket) return;

      // Clear row
      msgDiv.querySelectorAll(`.fr-seg[data-key="${key}"]`)
        .forEach(s => s.classList.remove("on"));

      // Select this segment
      seg.classList.add("on");

      // Show value
      const valEl = msgDiv.querySelector(`.fr-val[data-key="${key}"]`);
      if (valEl) valEl.textContent = bucket.label;

      selections[key] = bucket;
      clearError();
      refreshSubmit();
    });
  });

  // Default-select the leftmost segment on every row
  FINANCIAL_FIELDS.forEach((f) => {
    const firstSeg = msgDiv.querySelector(`.fr-seg[data-key="${f.key}"][data-index="0"]`);
    if (firstSeg) firstSeg.click();
  });

  submitBtn.onclick = () => {
    if (msgDiv.classList.contains("frozen")) return;
    const missing = FINANCIAL_FIELDS.filter(f => !selections[f.key]).map(f => f.label);
    if (missing.length) {
      showError("Please answer: " + missing.join(", "));
      return;
    }
    const payload = {};
    FINANCIAL_FIELDS.forEach((f) => {
      const b = selections[f.key];
      payload[f.key] = { min: b.min, max: b.max, label: b.label };
    });
    sendAnswer(JSON.stringify(payload));
  };
}
