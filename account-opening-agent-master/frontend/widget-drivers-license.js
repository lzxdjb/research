/**
 * Driver's License Confirmation Widget.
 *
 * Displays OCR-extracted fields for user review. The DL image itself is
 * delivered to the backend BEFORE this widget appears, via one of two routes:
 *   1. User picks a file via the chat attach button (regular upload), or
 *   2. Model calls capture_document(purpose="upload", doc_type="drivers_license_front")
 *      — the frontend snaps a frame from the live webcam and uploads it as a
 *      file. Either way the downstream flow is identical: the model calls
 *      extract_document_info on the stored file, then renders this widget
 *      with the OCR fields as `fields` prefill.
 *
 * Expiration-date sanity check (must not be in the past) is enforced by the
 * shared widget-document-upload base on submit.
 *
 * sendAnswer payload: all confirmed fields as JSON.
 */

const DL_CONFIG = {
  documentType: "drivers_license",
  title: "Driver's License",
  slots: [],
  reviewFields: [
    { key: "first_name",       label: "First Name",       type: "text",   required: false },
    { key: "middle_name",      label: "Middle Name",      type: "text",   required: false },
    { key: "last_name",        label: "Last Name",        type: "text",   required: false },
    { key: "date_of_birth",    label: "Date of Birth",    type: "text",   required: false },
    { key: "gender",           label: "Gender",           type: "select", required: false, options: ["MALE", "FEMALE", "OTHER"] },
    { key: "expiration_date",  label: "Expiration Date",  type: "date",   required: true },
    { key: "address_1",        label: "Address Line 1",   type: "text",   required: false },
    { key: "address_2",        label: "Address Line 2",   type: "text",   required: false },
    { key: "city",             label: "City",             type: "text",   required: false },
    { key: "state",            label: "State",            type: "text",   required: false },
    { key: "zip_code",         label: "Zip Code",         type: "text",   required: false },
  ],
};

const _dlRenderer = createDocumentUploadWidget(DL_CONFIG);

function renderDriversLicenseWidget(msgDiv, data, sendAnswer) {
  const fields = data.fields || data.prefill || {};
  if (fields.expiry_date && !fields.expiration_date) {
    fields.expiration_date = fields.expiry_date;
  }
  if (fields.given_name && !fields.first_name) {
    fields.first_name = fields.given_name;
  }
  if (fields.family_name && !fields.last_name) {
    fields.last_name = fields.family_name;
  }
  return _dlRenderer(msgDiv, { ...data, prefill: fields }, sendAnswer);
}
