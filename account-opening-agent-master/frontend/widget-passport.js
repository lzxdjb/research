/**
 * Passport Confirmation Widget.
 *
 * Displays passport details for user review.  The passport photo is uploaded
 * through chat — this widget is confirmation-only with no upload slot.
 *
 * sendAnswer payload (JSON):
 *   { document_type: "passport", passport_number, expiration_date, issuing_country }
 */

const PASSPORT_CONFIG = {
  documentType: "passport",
  title: "Passport",
  slots: [],
  metadataFields: [
    { key: "passport_number",  label: "Passport Number",  type: "text", required: true, maxlength: 20, placeholder: "Passport number" },
    { key: "expiration_date",  label: "Expiration Date",  type: "date", required: true },
  ],
};

const _passportRenderer = createDocumentUploadWidget(PASSPORT_CONFIG);

function renderPassportWidget(msgDiv, data, sendAnswer) {
  const fields = data.fields || data.prefill || {};

  if (fields.document_number && !fields.passport_number) {
    fields.passport_number = fields.document_number;
  }
  if (fields.expiry_date && !fields.expiration_date) {
    fields.expiration_date = fields.expiry_date;
  }

  return _passportRenderer(msgDiv, { ...data, prefill: fields }, sendAnswer);
}
