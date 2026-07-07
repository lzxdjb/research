/**
 * Address Proof Confirmation Widget (FOREIGNER branch).
 *
 * Displays proof type dropdown and OCR-extracted name/address for review.
 * The document is uploaded through chat — this widget is confirmation-only.
 *
 * sendAnswer payload (JSON):
 *   { document_type: "address_proof", proof_type, name_on_doc?, address_on_doc? }
 */

const ADDRESS_PROOF_CONFIG = {
  documentType: "address_proof",
  title: "Address Proof",
  slots: [],
  metadataFields: [
    {
      key: "proof_type",
      label: "Document Type",
      type: "select",
      required: true,
      placeholder: "Select document type...",
      options: [
        { value: "utility_bill",          label: "Utility Bill" },
        { value: "credit_card_statement", label: "Credit Card Statement" },
        { value: "bank_statement",        label: "Bank Statement" },
      ],
    },
  ],
  reviewFields: [
    { key: "name_on_doc",    label: "Name on Document",    type: "text", required: false },
    { key: "address_on_doc", label: "Address on Document",  type: "text", required: false },
  ],
};

const _addressProofRenderer = createDocumentUploadWidget(ADDRESS_PROOF_CONFIG);

function renderAddressProofUploadWidget(msgDiv, data, sendAnswer) {
  const fields = data.fields || data.prefill || {};

  if (fields.full_name && !fields.name_on_doc) {
    fields.name_on_doc = fields.full_name;
  }
  if (fields.address && !fields.address_on_doc) {
    fields.address_on_doc = fields.address;
  }

  return _addressProofRenderer(msgDiv, { ...data, prefill: fields }, sendAnswer);
}
