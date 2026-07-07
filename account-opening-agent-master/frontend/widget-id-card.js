/**
 * Chinese National ID Card (身份证) Confirmation Widget.
 *
 * Displays OCR-extracted fields for user review. Name and address fields are
 * marked `noChinese: true` — the shared widget-document-upload base strips
 * Chinese characters at render time (helper below) and blocks Submit with an
 * inline red error if any are still present. We avoid `alert()` because it
 * is unreliable inside iOS/Android webviews (requires the host app to
 * implement WKUIDelegate / WebChromeClient.onJsAlert; otherwise silent).
 *
 * sendAnswer payload (JSON):
 *   { document_type: "id_card", id_number, first_name?, last_name?,
 *     date_of_birth?, gender?, address_state?, address_city?,
 *     address_street1?, address_street2?, zip_code? }
 */

const ID_CARD_CONFIG = {
  documentType: "id_card",
  title: "中国身份证信息确认",
  slots: [],
  reviewTitle: "姓名和住址请都填写英文拼音，填写中文无法提交",
  reviewFields: [
    { key: "id_number",      label: "身份证号码",         type: "text",   required: true },
    { key: "last_name",      label: "姓（拼音）",          type: "text",   required: false, noChinese: true },
    { key: "first_name",     label: "名（拼音）",          type: "text",   required: false, noChinese: true },
    { key: "date_of_birth",  label: "出生日期",           type: "text",   required: false, placeholder: "YYYY-MM-DD" },
    { key: "gender",         label: "性别",              type: "select", required: false, options: ["MALE", "FEMALE", "OTHER"] },
    { key: "address_state",  label: "省/州（拼音）",       type: "text",   required: false, noChinese: true },
    { key: "address_city",   label: "市（拼音）",          type: "text",   required: false, noChinese: true },
    { key: "address_street1",label: "街道地址（拼音）",     type: "text",   required: false, noChinese: true },
    { key: "address_street2",label: "楼号/单元（拼音）",    type: "text",   required: false, noChinese: true },
    { key: "zip_code",       label: "邮政编码",           type: "text",   required: true, placeholder: "必填" },
  ],
};

/** Strip Chinese characters, collapse whitespace (used at render time only). */
function _stripChinese(str) {
  if (!str) return str;
  return str.replace(/[一-鿿]/g, "").replace(/\s{2,}/g, " ").trim();
}

const _idCardRenderer = createDocumentUploadWidget(ID_CARD_CONFIG);

function renderIdCardWidget(msgDiv, data, sendAnswer) {
  const fields = data.fields || data.prefill || {};

  // Normalize field names from extract_document_info
  if (fields.document_number && !fields.id_number) {
    fields.id_number = fields.document_number;
  }
  if (fields.given_name && !fields.first_name) {
    fields.first_name = _stripChinese(fields.given_name);
  }
  if (fields.family_name && !fields.last_name) {
    fields.last_name = _stripChinese(fields.family_name);
  }
  if (fields.full_name && !fields.last_name && !fields.first_name) {
    const parts = fields.full_name.trim().split(/\s+/);
    if (parts.length >= 2) {
      fields.last_name = _stripChinese(parts[0]);
      fields.first_name = _stripChinese(parts.slice(1).join(" "));
    }
  }
  // Full address string fallback
  if (fields.address && !fields.address_state && !fields.address_city && !fields.address_street1) {
    fields.address_street1 = _stripChinese(fields.address);
  }
  ["address_state", "address_city", "address_street1", "address_street2"].forEach(function(k) {
    if (fields[k]) fields[k] = _stripChinese(fields[k]);
  });

  // Submit-time Chinese-character check is enforced by the shared base via
  // the `noChinese: true` flag on each reviewField above — no wrapper needed.
  return _idCardRenderer(msgDiv, { ...data, prefill: fields }, sendAnswer);
}
