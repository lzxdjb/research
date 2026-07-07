/**
 * US Permanent Resident Card (Green Card) Confirmation Widget.
 *
 * Displays OCR-extracted fields for user review.  The card image is uploaded
 * through chat — this widget is confirmation-only.
 *
 * sendAnswer payload (JSON):
 *   { document_type: "permanent_resident_card", card_number, expiration_date }
 */

const GREEN_CARD_CONFIG = {
  documentType: "permanent_resident_card",
  title: "Green Card (US Permanent Resident Card)",
  slots: [],
  reviewFields: [
    { key: "card_number",     label: "USCIS / Card Number",  type: "text", required: true },
    { key: "expiration_date", label: "Expiration Date",      type: "date", required: true },
  ],
};

const _gcRenderer = createDocumentUploadWidget(GREEN_CARD_CONFIG);

function renderGreenCardWidget(msgDiv, data, sendAnswer) {
  const fields = data.fields || data.prefill || {};

  // Normalize from extract_document_info
  if (fields.document_number && !fields.card_number) {
    fields.card_number = fields.document_number;
  }
  if (fields.expiry_date && !fields.expiration_date) {
    fields.expiration_date = fields.expiry_date;
  }

  return _gcRenderer(msgDiv, { ...data, prefill: fields }, sendAnswer);
}
