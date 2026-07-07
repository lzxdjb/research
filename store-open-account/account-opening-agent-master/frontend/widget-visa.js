/**
 * US Visa input widget.
 * Collects visa_type + expiration_date and uploads the visa page via
 * geminiClient.uploadFile(file_type='visa').
 *
 * sendAnswer payload: { file_type: 'visa', visa_type, expiration_date, filename }
 */
function renderVisaWidget(msgDiv, data, sendAnswer) {
  const { question } = data;
  const visaTypes = ["H1B", "L1", "F1", "O1", "J1", "B1/B2", "Other"];

  msgDiv.innerHTML = `
    <div class="widget-card widget-doc-card">
      <p class="widget-question">${question}</p>
      <div class="widget-doc-form">
        <div class="widget-doc-row">
          <label class="widget-doc-label">Visa type</label>
          <select class="widget-address-select" id="visaType">
            <option value="">Select visa type</option>
            ${visaTypes.map(v => `<option value="${v}">${v}</option>`).join("")}
          </select>
        </div>
        <div class="widget-doc-row">
          <label class="widget-doc-label">Expiration date</label>
          <input class="widget-address-input" id="visaExp" type="date" />
        </div>
        <div class="widget-doc-row widget-doc-row--file">
          <label class="widget-doc-label">Visa page photo</label>
          <input type="file" id="visaFile" accept="image/*,.pdf" />
          <div class="widget-file-status hidden" id="visaFileStatus"></div>
        </div>
        <div class="widget-address-error hidden" id="visaError"></div>
      </div>
      <button class="widget-submit-btn" id="visaSubmitBtn">Submit</button>
    </div>`;

  const chatLog = document.getElementById("chat-log");
  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;

  const typeSel = msgDiv.querySelector("#visaType");
  const expInput = msgDiv.querySelector("#visaExp");
  const fileInput = msgDiv.querySelector("#visaFile");
  const fileStatus = msgDiv.querySelector("#visaFileStatus");
  const errorDiv = msgDiv.querySelector("#visaError");
  const submitBtn = msgDiv.querySelector("#visaSubmitBtn");

  let uploadedFilename = null;
  let uploading = false;

  function showError(msg) { errorDiv.textContent = msg; errorDiv.classList.remove("hidden"); }
  function clearError() { errorDiv.classList.add("hidden"); }
  function setFileStatus(t, err) {
    fileStatus.textContent = t;
    fileStatus.classList.toggle("hidden", !t);
    fileStatus.classList.toggle("widget-file-status--error", !!err);
  }

  async function readAsBase64(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(",")[1]);
      r.onerror = () => reject(new Error("File read failed"));
      r.readAsDataURL(file);
    });
  }

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    uploading = true;
    uploadedFilename = null;
    setFileStatus(`Uploading ${file.name}…`, false);
    try {
      const base64 = await readAsBase64(file);
      await geminiClient.uploadFile(base64, file.name, true, "visa");
      uploadedFilename = file.name;
      setFileStatus(`✓ ${file.name} uploaded`, false);
    } catch (e) {
      setFileStatus("Upload failed: " + (e.message || e), true);
    } finally {
      uploading = false;
    }
  });

  submitBtn.onclick = () => {
    if (msgDiv.classList.contains("frozen")) return;
    if (uploading) { showError("Please wait — visa photo still uploading."); return; }
    clearError();

    const vt = typeSel.value;
    const exp = expInput.value;

    if (!vt) { showError("Visa type is required."); return; }
    if (!exp) { showError("Expiration date is required."); return; }
    if (!uploadedFilename) { showError("Please upload your visa page first."); return; }

    sendAnswer(JSON.stringify({
      file_type: "visa",
      visa_type: vt,
      expiration_date: exp,
      filename: uploadedFilename,
    }));
  };
}
