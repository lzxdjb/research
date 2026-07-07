"""
Onboarding Tool Schemas (US Residents Only)
Defines all tool parameters for the Light Horse Securities voice-first account opening flow.

FIELD ALIGNMENT NOTE:
This file is strictly aligned with the original collect_information field definitions
(field names, types, enum values, range encoding). Do NOT rename fields or change
encodings without confirming with the backend API.

Architecture:
- AUTH_TOOLS: login, verification code, contact binding
- ACCOUNT_TOOLS: progress query, file upload, camera capture, final submission
- SUBMIT_TOOLS: 10 business submission tools (one per logical section)
- DOCUMENT_TOOLS: OCR extraction from captured/uploaded documents
- UI_TOOLS: frontend widgets
"""

from google.genai import types as gemini_types


# =============================================================================
# AUTH TOOLS
# =============================================================================
AUTH_TOOLS = [
    gemini_types.FunctionDeclaration(
        name="send_verification_code",
        description="""Send a verification code to a mobile number or email address that the user has explicitly provided.
IMPORTANT: You MUST have the user's actual contact information before calling this tool.
- Ask the user which method they want to use (mobile or email) and wait for their answer.
- Then ask for their mobile number or email address (via present_phone_input or present_email_input) and wait for them to provide it.
- Only AFTER the user gives you their contact info, call this tool with the actual value.
NEVER call this tool with placeholder/guessed values or before the user has provided the contact information.""",
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "contact": gemini_types.Schema(type="STRING", description="Mobile number or email address — must be the actual value the user provided"),
                "contact_type": gemini_types.Schema(type="STRING", enum=["MOBILE", "EMAIL"], description="MOBILE or EMAIL"),
                "area_code": gemini_types.Schema(type="STRING", description="Country code (e.g., 1 for US, 86 for China). Only digits, no plus sign. Use whatever country code the user provides — do NOT enforce US-only.")
            },
            required=["contact", "contact_type"]
        )
    ),
    gemini_types.FunctionDeclaration(
        name="login_and_get_token",
        description="""Login with verification code AND get trading access token AND query account opening progress in one call.
Call this after send_verification_code when user provides the code.
This combines login + get_trading_token + query_progress into a single step.
Returns: {status, missing_fields, collected_fields, completion_percentage, sections}.
Required before accessing account opening features.""",
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "contact": gemini_types.Schema(type="STRING", description="Phone number or email used for login"),
                "verification_code": gemini_types.Schema(type="STRING", description="6-digit verification code"),
                "contact_type": gemini_types.Schema(type="STRING", enum=["MOBILE", "EMAIL"], description="MOBILE or EMAIL"),
                "area_code": gemini_types.Schema(type="STRING", description="Country code (e.g., 1 for US, 86 for China). Only digits, no plus sign. Use whatever country code the user provides.")
            },
            required=["contact", "verification_code", "contact_type"]
        )
    ),
    gemini_types.FunctionDeclaration(
        name="get_user_info",
        description="Get current user info to check if mobile and email are bound.",
        parameters=gemini_types.Schema(type="OBJECT", properties={})
    ),
    gemini_types.FunctionDeclaration(
        name="update_email",
        description="""Bind or update email address with verification code.
CRITICAL — you MUST follow this sequence:
1. First call send_verification_code(contact=email, contact_type="EMAIL") to send the code.
2. Wait for the user to provide the verification code they received.
3. Then call update_email with the email AND the code the user provided.
NEVER call this tool without first calling send_verification_code — the code must be sent before it can be verified.""",
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "email": gemini_types.Schema(type="STRING", description="Email address"),
                "auth_code": gemini_types.Schema(type="STRING", description="Verification code sent to email")
            },
            required=["email", "auth_code"]
        )
    ),
    gemini_types.FunctionDeclaration(
        name="update_mobile",
        description="""Bind or update mobile number with verification code.
CRITICAL — you MUST follow this sequence:
1. First call send_verification_code(contact=phone, contact_type="MOBILE") to send the code.
2. Wait for the user to provide the verification code they received.
3. Then call update_mobile with the phone AND the code the user provided.
NEVER call this tool without first calling send_verification_code — the code must be sent before it can be verified.""",
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "phone": gemini_types.Schema(type="STRING", description="Mobile number"),
                "area_code": gemini_types.Schema(type="STRING", description="Country code (digits only, e.g. '1' for US, '86' for China). Use whatever country code the user provides."),
                "auth_code": gemini_types.Schema(type="STRING", description="Verification code sent to phone")
            },
            required=["phone", "area_code", "auth_code"]
        )
    ),
]


# =============================================================================
# ACCOUNT TOOLS (progress, upload, capture, final submit)
# =============================================================================
ACCOUNT_TOOLS = [
    gemini_types.FunctionDeclaration(
        name="query_progress",
        description="""Query current account opening progress and status.
Returns:
  - status: current stage identifier
  - missing_fields: list of fields still needed
  - collected_fields: list of fields ALREADY submitted (do NOT re-submit)
  - completion_percentage: integer 0-100, overall weighted progress
  - sections: list of {name, status: 'complete'|'in_progress'|'pending', percentage} per section

CALL THIS:
  1. IMMEDIATELY after successful login_and_get_token
  2. After EVERY successful submit_* call (to refresh progress UI)
  3. Before submit_application (to verify 100% completion)

After receiving the response, you MUST refresh the on-screen progress indicator so the user sees their progress advance in real time.""",
        parameters=gemini_types.Schema(type="OBJECT", properties={})
    ),
    gemini_types.FunctionDeclaration(
        name="submit_application",
        description="""Submit the complete account opening application after all information is collected.
CRITICAL: Only call this when:
  1. query_progress returns completion_percentage = 100, AND
  2. The user has verbally confirmed they want to submit.
Never call this just because some submit_* tools succeeded — verify 100% via query_progress first.""",
        parameters=gemini_types.Schema(type="OBJECT", properties={})
    ),
    gemini_types.FunctionDeclaration(
        name="classify_document_type",
        description=(
            "Report the detected document type after examining a user-uploaded image. "
            "Call this tool immediately after the user uploads a file and you have identified its type. "
            "Call BEFORE speaking to the user about the document."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "file_type": gemini_types.Schema(
                    type="STRING",
                    enum=[
                        "drivers_licence_front", "drivers_licence_back",
                        "id_card", "passport", "affiliated_approval",
                        "permanent_resident_card", "visa",
                        "utility_bill", "credit_card_statement", "bank_statement",
                    ],
                    description=(
                        "The detected document type. "
                        "drivers_licence_front/back = Domestic citizen DL. "
                        "permanent_resident_card / visa = Domestic non-citizen. "
                        "passport = Domestic non-citizen and all Foreigner branches. "
                        "id_card = Chinese national identity card (Foreigner-Chinese). "
                        "utility_bill / credit_card_statement / bank_statement = Foreigner address proof."
                    )
                )
            },
            required=["file_type"]
        )
    ),
    gemini_types.FunctionDeclaration(
        name="upload_file",
        description="Upload a document file (ID, passport, bank statement, etc.) for account opening. The file is stored server-side; no file_id is returned.",
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "file_data": gemini_types.Schema(type="STRING", description="Base64 encoded file data"),
                "filename": gemini_types.Schema(type="STRING", description="Original filename with extension"),
                "file_type": gemini_types.Schema(
                    type="STRING",
                    enum=[
                        "drivers_licence_front", "drivers_licence_back",
                        "id_card", "passport", "affiliated_approval",
                        "permanent_resident_card", "visa",
                        "utility_bill", "credit_card_statement", "bank_statement",
                    ],
                    description="Document classification for the backend (matches classify_document_type enum)."
                ),
                "is_need_min": gemini_types.Schema(type="BOOLEAN", description="Whether to generate thumbnail"),
            },
            required=["file_data", "filename", "file_type"]
        )
    ),
    gemini_types.FunctionDeclaration(
        name="capture_document",
        description="""Snap a single frame from the user's webcam and upload it to the server AS A FILE — this is a convenience shortcut for desktop users who'd rather hold the document up to their webcam than fish a photo out of their phone. The captured frame goes through exactly the same upload pipeline as a file-picker upload; downstream handling (`extract_document_info`, the review widget, etc.) does not care which route was used.

Only purpose="upload" is supported in this flow. purpose="ocr" (read text from the live video stream without uploading) is DEPRECATED — do NOT call it. If you need to read fields off a document, wait for it to be uploaded (via attach button OR via capture_document(purpose="upload")) and then call `extract_document_info`.

When to offer this path: at Personal Identity (driver's license) or Document Upload (passport / id card / green card / visa). Offer both options to the user — "upload a photo via the attach button, or hold it up to your webcam and I'll snap a frame" — and let them pick. The user's camera must already be on.

Sequence when the user opts for webcam capture:
1. Call capture_document(purpose="upload", doc_type=<the file_type>).
2. WAIT for a text message from the frontend starting with "CAPTURE_RESULT:" — that's the signal the frame was captured AND uploaded, and a preview now shows in the chat.
3. If you instead receive a "CAMERA_UNAVAILABLE:" message, the camera is off — tell the user to turn it on, then retry.
4. After CAPTURE_RESULT, proceed exactly as you would after a normal file upload: call `extract_document_info` (if OCR is desired) and then the matching review widget.

Do NOT continue speaking or call any other tool between the capture_document call and the CAPTURE_RESULT message.""",
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "doc_type": gemini_types.Schema(
                    type="STRING",
                    enum=["drivers_license_front", "drivers_license_back", "passport", "government_issued_id"],
                    description="Type of document being captured"
                ),
                "purpose": gemini_types.Schema(
                    type="STRING",
                    enum=["ocr", "upload"],
                    description="'ocr' = verify camera and OCR from video stream (no upload); 'upload' = capture frame and upload as file with preview in chat"
                ),
            },
            required=["doc_type"]
        )
    ),
]


# =============================================================================
# SUBMIT TOOLS (10 business sections)
# =============================================================================
SUBMIT_TOOLS = [

    # -------------------------------------------------------------------------
    # 1. Account Type
    # -------------------------------------------------------------------------
    gemini_types.FunctionDeclaration(
        name="submit_account_type",
        description=(
            "Submit the user's account type selection. "
            "Call this AFTER the user picks from the present_options widget for account type."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "account_type": gemini_types.Schema(
                    type="STRING",
                    enum=["CASH", "MARGIN"],
                    description="CASH = cash account, MARGIN = margin account"
                ),
            },
            required=["account_type"],
        )
    ),

    # -------------------------------------------------------------------------
    # 2. Personal Identity  (FIX: gvie_name restored to match backend API)
    # -------------------------------------------------------------------------
    gemini_types.FunctionDeclaration(
        name="submit_personal_identity",
        description=(
            "Submit personal identity information. Branch-aware: "
            "  - DOMESTIC: social_security_number is REQUIRED; tax_id/tax_id_country must be omitted. "
            "  - FOREIGNER: tax_id AND tax_id_country are REQUIRED; social_security_number must be omitted. "
            "Typically called after the user confirms personal info (driver's license review for "
            "Domestic, present_personal_info_input for Foreigner) AND after collecting the right "
            "tax identifier via present_ssn_input (Domestic) or present_tax_id_input (Foreigner) "
            "AND marital_status / num_dependents via present_options."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "given_name": gemini_types.Schema(
                    type="STRING",
                    description="First/given name."
                ),
                "middle_name": gemini_types.Schema(type="STRING", description="Middle name, optional"),
                "family_name": gemini_types.Schema(type="STRING", description="Last/family name"),
                "date_of_birth": gemini_types.Schema(
                    type="STRING",
                    description="ISO 8601 format YYYY-MM-DD"
                ),
                "gender": gemini_types.Schema(
                    type="STRING",
                    enum=["MALE", "FEMALE", "OTHER"]
                ),
                "marital_status": gemini_types.Schema(
                    type="STRING",
                    enum=["MARRIED", "SINGLE", "DIVORCED", "WIDOWED"]
                ),
                "num_dependents": gemini_types.Schema(
                    type="INTEGER",
                    description="Number of dependents (0 or more)"
                ),
                "social_security_number": gemini_types.Schema(
                    type="STRING",
                    description="DOMESTIC ONLY. 9 digits, format XXX-XX-XXXX (with dashes). "
                                "MUST come from present_ssn_input widget, never from voice. "
                                "Omit for FOREIGNER branch."
                ),
                "tax_id": gemini_types.Schema(
                    type="STRING",
                    description="FOREIGNER ONLY. Tax identifier issued by the user's home country. "
                                "MUST come from present_tax_id_input widget. Omit for DOMESTIC branch."
                ),
                "tax_id_country": gemini_types.Schema(
                    type="STRING",
                    description="FOREIGNER ONLY. ISO 3166-1 alpha-3 country code (3 letters, uppercase) "
                                "of the authority that issued the tax_id (defaults to citizenship_country)."
                ),
            },
            required=[
                "given_name", "family_name", "date_of_birth", "gender",
                "marital_status", "num_dependents"
            ],
        )
    ),

    # -------------------------------------------------------------------------
    # 3. Residency Status (3-letter ISO codes per user requirement)
    # -------------------------------------------------------------------------
    gemini_types.FunctionDeclaration(
        name="submit_residency_status",
        description=(
            "Submit citizenship and residency information. Branch-aware: "
            "  - DOMESTIC: permanent_resident is REQUIRED. If citizenship_country is not 'USA' AND "
            "    permanent_resident is false, the user is on the wrong branch — instruct them to "
            "    refresh and pick the Foreigner path. "
            "  - FOREIGNER: permanent_resident is NOT required (will be coerced to false by the backend). "
            "    No US-residency check is performed."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "citizenship_country": gemini_types.Schema(
                    type="STRING",
                    description="ISO 3166-1 alpha-3 country code (3 letters, uppercase), e.g. 'USA', 'CHN', 'MEX', 'IND'"
                ),
                "birth_country": gemini_types.Schema(
                    type="STRING",
                    description="ISO 3166-1 alpha-3 country code (3 letters, uppercase), e.g. 'USA', 'CHN', 'MEX', 'IND'"
                ),
                "permanent_resident": gemini_types.Schema(
                    type="BOOLEAN",
                    description="DOMESTIC ONLY. True if user holds a US green card or other permanent residency status. "
                                "Omit / set false for FOREIGNER branch."
                ),
                "visa_type": gemini_types.Schema(
                    type="STRING",
                    description="DOMESTIC non-citizen non-PR only. Visa type (e.g. H1B, L1, F1)."
                ),
                "visa_expiration_date": gemini_types.Schema(
                    type="STRING",
                    description="ISO 8601 YYYY-MM-DD. Required only if visa_type is provided."
                ),
            },
            required=["citizenship_country", "birth_country"],
        )
    ),

    # -------------------------------------------------------------------------
    # 4. Home Address (matches original collect_information.home_address nested object)
    # -------------------------------------------------------------------------
    gemini_types.FunctionDeclaration(
        name="submit_home_address",
        description=(
            "Submit user's residential address as a nested home_address object. "
            "Branch-aware: pass `address_branch` so the backend picks the right validator. "
            "  - US: country='USA' enforced; state limited to 50 states + 6 US territories; "
            "    postal_code must match 5-digit ZIP or ZIP+4; PO Boxes rejected. "
            "  - INTERNATIONAL: country accepts any ISO 3166-1 alpha-3; state/postal_code free-form; "
            "    PO Box check skipped. "
            "MUST be called with values returned from present_us_address_input / present_address_input "
            "widget — do NOT collect address components verbally."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "address_branch": gemini_types.Schema(
                    type="STRING",
                    enum=["US", "INTERNATIONAL"],
                    description="Selects validator. Domestic session → 'US'; Foreigner session → 'INTERNATIONAL'."
                ),
                "home_address": gemini_types.Schema(
                    type="OBJECT",
                    description="Residential address object (matches backend collect_information.home_address structure).",
                    properties={
                        "country": gemini_types.Schema(
                            type="STRING",
                            description="ISO 3166-1 alpha-3 country code. 'USA' in US mode; any country in INTERNATIONAL mode."
                        ),
                        "state": gemini_types.Schema(
                            type="STRING",
                            description="US mode: two-letter state/territory code from the 50 states or AS/DC/GU/MP/PR/VI. "
                                        "INTERNATIONAL mode: free-form province / region / state name."
                        ),
                        "city": gemini_types.Schema(type="STRING"),
                        "postal_code": gemini_types.Schema(
                            type="STRING",
                            description="US mode: 5-digit ZIP or ZIP+4 (12345 or 12345-6789). "
                                        "INTERNATIONAL mode: free-form postal code."
                        ),
                        "street_address1": gemini_types.Schema(
                            type="STRING",
                            description="Street address. PO Box rejected in US mode; accepted in INTERNATIONAL mode."
                        ),
                        "street_address2": gemini_types.Schema(
                            type="STRING",
                            description="Apartment, suite, unit, etc. Optional."
                        ),
                    },
                    required=["country", "state", "city", "postal_code", "street_address1"],
                ),
            },
            required=["address_branch", "home_address"],
        )
    ),

    # -------------------------------------------------------------------------
    # 5. Employment (FIX: industry restored to free-form STRING to match original)
    # -------------------------------------------------------------------------
    gemini_types.FunctionDeclaration(
        name="submit_employment",
        description=(
            "Submit employment information. Conditional fields: "
            "If employment_status is EMPLOYED or SELF_EMPLOYED, then employer, position_employed, "
            "years_employed, and industry are REQUIRED. "
            "If employment_status is UNEMPLOYED, RETIRED, or STUDENT, only employment_status is required."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "employment_status": gemini_types.Schema(
                    type="STRING",
                    enum=["EMPLOYED", "SELF_EMPLOYED", "UNEMPLOYED", "RETIRED", "STUDENT"]
                ),
                "employer": gemini_types.Schema(
                    type="STRING",
                    description="Employer/company name. Required if EMPLOYED or SELF_EMPLOYED."
                ),
                "position_employed": gemini_types.Schema(
                    type="STRING",
                    description="Job title. Required if EMPLOYED or SELF_EMPLOYED."
                ),
                "years_employed": gemini_types.Schema(
                    type="INTEGER",
                    description="Years at current employer. Required if EMPLOYED or SELF_EMPLOYED."
                ),
                "industry": gemini_types.Schema(
                    type="STRING",
                    description="Industry name (free-form text). Required if EMPLOYED or SELF_EMPLOYED."
                ),
            },
            required=["employment_status"],
        )
    ),

    # -------------------------------------------------------------------------
    # 6. Financial Profile (FIX: reverted to min/max integer pairs per backend API)
    # -------------------------------------------------------------------------
    gemini_types.FunctionDeclaration(
        name="submit_financial_profile",
        description=(
            "Submit financial profile. "
            "Income and net worth use min/max integer PAIRS (NOT single bucket strings). "
            "Each field has its own allowed boundary values (see individual field descriptions). "
            "The frontend bucket widget returns these integer pairs directly — pass them through unchanged."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "funding_source": gemini_types.Schema(
                    type="STRING",
                    enum=["Savings", "Inheritance", "Pension", "Rental Income",
                          "Social Security", "Other"]
                ),
                "other_source": gemini_types.Schema(
                    type="STRING",
                    description="Description of funding source. Required only if funding_source='Other'."
                ),
                "annual_income_usd_min": gemini_types.Schema(
                    type="INTEGER",
                    description="Annual income range min. "
                                "Allowed: 0, 25001, 50001, 100001, 200001, 300001, 500001, 1200001"
                ),
                "annual_income_usd_max": gemini_types.Schema(
                    type="INTEGER",
                    description="Annual income range max. "
                                "Allowed: 25000, 50000, 100000, 200000, 300000, 500000, 1200000, 9999999"
                ),
                "liquid_net_worth_usd_min": gemini_types.Schema(
                    type="INTEGER",
                    description="Liquid net worth range min. "
                                "Allowed: 0, 50001, 100001, 200001, 500001, 1000001, 5000001"
                ),
                "liquid_net_worth_usd_max": gemini_types.Schema(
                    type="INTEGER",
                    description="Liquid net worth range max. "
                                "Allowed: 50000, 100000, 200000, 500000, 1000000, 5000000, 9999999"
                ),
                "total_net_worth_usd_min": gemini_types.Schema(
                    type="INTEGER",
                    description="Total net worth range min. "
                                "Allowed: 0, 50001, 100001, 200001, 500001, 1000001, 5000001"
                ),
                "total_net_worth_usd_max": gemini_types.Schema(
                    type="INTEGER",
                    description="Total net worth range max. "
                                "Allowed: 50000, 100000, 200000, 500000, 1000000, 5000000, 9999999"
                ),
            },
            required=[
                "funding_source",
                "annual_income_usd_min", "annual_income_usd_max",
                "liquid_net_worth_usd_min", "liquid_net_worth_usd_max",
                "total_net_worth_usd_min", "total_net_worth_usd_max"
            ],
        )
    ),

    # -------------------------------------------------------------------------
    # 7. Investment Profile (FINRA Rule 2111 Suitability)
    # -------------------------------------------------------------------------
    gemini_types.FunctionDeclaration(
        name="submit_investment_profile",
        description=(
            "Submit FINRA Rule 2111 suitability profile. All 5 fields together form the "
            "regulatory suitability assessment. Each field MUST come from a present_options "
            "widget interaction — do NOT infer values from free-form user speech."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "investment_experience": gemini_types.Schema(
                    type="STRING",
                    enum=["EXTENSIVE", "GOOD", "LIMITED", "NONE"]
                ),
                "investment_objective": gemini_types.Schema(
                    type="STRING",
                    enum=["GROWTH", "INCOME", "CAPITAL_PRESERVATION", "SPECULATION"]
                ),
                "time_horizon": gemini_types.Schema(
                    type="STRING",
                    enum=["SHORT", "AVERAGE", "LONGEST"],
                    description="SHORT = <3 years, AVERAGE = 3-10 years, LONGEST = >10 years"
                ),
                "risk_tolerance": gemini_types.Schema(
                    type="STRING",
                    enum=["HIGH", "MEDIUM", "LOW"]
                ),
                "liquidity_needs": gemini_types.Schema(
                    type="STRING",
                    enum=["VERY_IMPORTANT", "SOMEWHAT_IMPORTANT", "NOT_IMPORTANT"]
                ),
            },
            required=[
                "investment_experience", "investment_objective", "time_horizon",
                "risk_tolerance", "liquidity_needs"
            ],
        )
    ),

    # -------------------------------------------------------------------------
    # 8. Disclosures  (5 disclosure questions handled by present_disclosure widget)
    # -------------------------------------------------------------------------
    gemini_types.FunctionDeclaration(
        name="submit_disclosures",
        description=(
            "Submit all 5 regulatory disclosures together. Called after the user completes the "
            "present_disclosure widget. Each disclosure has a boolean answer plus conditional "
            "follow-up text fields that are required only if the boolean is true. "
            "The 5 disclosures: control person, FINRA/exchange affiliation, politically exposed "
            "person, trade authorization, and trusted contact (is_identify)."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "is_control_person": gemini_types.Schema(
                    type="BOOLEAN",
                    description="Is the user a 10%+ shareholder, director, or officer of a public company?"
                ),
                "company_symbols": gemini_types.Schema(
                    type="STRING",
                    description="Comma-separated ticker symbols. Required if is_control_person=true."
                ),

                "is_affiliated_exchangeorfinra": gemini_types.Schema(
                    type="BOOLEAN",
                    description="Is the user affiliated with FINRA or a US stock exchange?"
                ),
                "firm_name": gemini_types.Schema(
                    type="STRING",
                    description="Affiliated firm name. Required if is_affiliated_exchangeorfinra=true."
                ),

                "is_politically_exposed": gemini_types.Schema(
                    type="BOOLEAN",
                    description="Is the user (or immediate family) a politically exposed person (PEP)?"
                ),
                "immediate_family": gemini_types.Schema(
                    type="STRING",
                    description="Name and relationship of PEP family member. Required if is_politically_exposed=true."
                ),
                "political_organization": gemini_types.Schema(
                    type="STRING",
                    description="Political organization name. Required if is_politically_exposed=true."
                ),

                "is_trade_authorization": gemini_types.Schema(
                    type="BOOLEAN",
                    description="Will anyone other than the account holder trade on this account?"
                ),
                "agent_name": gemini_types.Schema(
                    type="STRING",
                    description="Authorized agent name. Required if is_trade_authorization=true."
                ),

                "is_identify": gemini_types.Schema(
                    type="BOOLEAN",
                    description="Does the user want to designate a trusted contact person? "
                                "(FINRA Rule 4512 — a person the firm may contact about the account "
                                "for welfare-related concerns or to confirm account information.)"
                ),
            },
            required=[
                "is_control_person", "is_affiliated_exchangeorfinra",
                "is_politically_exposed", "is_trade_authorization", "is_identify"
            ],
        )
    ),

    # -------------------------------------------------------------------------
    # 10. Agreements
    # -------------------------------------------------------------------------
    gemini_types.FunctionDeclaration(
        name="submit_agreements",
        description=(
            "Record that the user has accepted all required customer agreements "
            "(account agreement, margin agreement if applicable, privacy policy, etc.). "
            "Should be called only after the user has actively confirmed acceptance via UI — "
            "do NOT call this based on verbal 'I agree' alone, as agreement acceptance must be "
            "logged with a UI interaction for compliance purposes."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "agreements_accepted": gemini_types.Schema(
                    type="BOOLEAN",
                    description="True when user has accepted all required agreements via the UI modal."
                ),
            },
            required=["agreements_accepted"],
        )
    ),

    # -------------------------------------------------------------------------
    # 11. Documents — unified submission for the branch-specific document matrix.
    # The backend matches branch + citizenship to the matrix in requirement.md §3.4
    # and rejects the call (with a list of still-missing types) if requirements
    # are not met. upload_file is still used per-file to push bytes; this tool
    # only acknowledges the metadata-complete set.
    # -------------------------------------------------------------------------
    gemini_types.FunctionDeclaration(
        name="submit_documents",
        description=(
            "Submit the metadata of every identity / address-proof document the user has uploaded in this "
            "session. This is a metadata pass-through to the broker's collect endpoint — pass the full "
            "`documents` array (file_type + filename + any per-type fields like passport_number, "
            "expiration_date, id_number, issuing_country, visa_type, card_number). The backend "
            "validates completeness against the user's branch / citizenship on its side; this tool does "
            "not do any local precheck, so do NOT rely on a `sub_branch` / `missing` field in the response. "
            "Expected document coverage per branch (for your own bookkeeping when assembling the list): "
            "  - Domestic-Citizen: drivers_licence_front + drivers_licence_back "
            "  - Domestic-PR: passport + permanent_resident_card "
            "  - Domestic-Visa: passport + visa "
            "  - Foreigner-Other: passport + (utility_bill | credit_card_statement | bank_statement) "
            "  - Foreigner-Chinese: passport + id_card + (utility_bill | credit_card_statement | bank_statement) "
            "Call this AFTER every required upload_file in the section is done."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "documents": gemini_types.Schema(
                    type="ARRAY",
                    description="List of documents that have been uploaded for this session.",
                    items=gemini_types.Schema(
                        type="OBJECT",
                        properties={
                            "file_type": gemini_types.Schema(
                                type="STRING",
                                enum=[
                                    "drivers_licence_front", "drivers_licence_back",
                                    "passport", "permanent_resident_card", "visa",
                                    "id_card",
                                    "utility_bill", "credit_card_statement", "bank_statement",
                                ],
                                description="Same enum as upload_file.file_type."
                            ),
                            "filename": gemini_types.Schema(
                                type="STRING",
                                description="Original filename of the upload."
                            ),
                            "expiration_date": gemini_types.Schema(
                                type="STRING",
                                description="ISO 8601 YYYY-MM-DD. Required for passport / visa / DL."
                            ),
                            "issue_date": gemini_types.Schema(
                                type="STRING",
                                description="ISO 8601 YYYY-MM-DD. Optional. If the user provides an issue date for address-proof documents, pass it through."
                            ),
                            "passport_number": gemini_types.Schema(
                                type="STRING",
                                description="Required if file_type='passport'."
                            ),
                            "issuing_country": gemini_types.Schema(
                                type="STRING",
                                description="ISO 3166-1 alpha-3. Required for passport / visa / permanent_resident_card."
                            ),
                            "visa_type": gemini_types.Schema(
                                type="STRING",
                                description="H1B / L1 / F1 / O1 / J1 / Other. Required if file_type='visa'."
                            ),
                            "id_number": gemini_types.Schema(
                                type="STRING",
                                description="18-digit Chinese ID number. Required if file_type='id_card'."
                            ),
                            "card_number": gemini_types.Schema(
                                type="STRING",
                                description="USCIS Green Card number. Required if file_type='permanent_resident_card'."
                            ),
                        },
                        required=["file_type"],
                    ),
                ),
            },
            required=["documents"],
        )
    ),
]


# =============================================================================
# DOCUMENT TOOLS (OCR)
# FIX: reverted to single 'address' string to match original; kept issuing_state/middle_name
#      as additive fields (they don't conflict, just extra info).
# =============================================================================
DOCUMENT_TOOLS = [
    gemini_types.FunctionDeclaration(
        name="extract_document_info",
        description=(
            "Extract structured information from an identity document image the user has uploaded via the "
            "chat attach button (driver's license, passport, ID card, etc.). Call this after the frontend "
            "confirms 'The file has been uploaded to the server', to pre-fill the matching review widget "
            "(present_drivers_license_review / present_passport_input / present_id_card_input). "
            "Do NOT pass any image URLs — Gemini reads the uploaded image directly. "
            "CRITICAL: Only call this tool if you can clearly see the expected physical document with readable "
            "printed text. If the image is not a document, the wrong type, or unreadable, do NOT call this tool "
            "— tell the user what was wrong and ask them to upload again. NEVER fabricate or infer field values; "
            "only report text you can actually read from the image. "
            "POLICY: For driver's license, if issuing_country is not 'USA' or a US territory, halt the flow and "
            "redirect the user to web onboarding — do NOT proceed to submit_personal_identity."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "document_type": gemini_types.Schema(
                    type="STRING",
                    enum=["ID_CARD", "PASSPORT", "DRIVERS_LICENSE", "BANK_STATEMENT", "OTHER"],
                ),
                "extracted_fields": gemini_types.Schema(
                    type="OBJECT",
                    description="Fields extracted from the document front",
                    properties={
                        "full_name": gemini_types.Schema(type="STRING", description="Full name as shown on document"),
                        "given_name": gemini_types.Schema(type="STRING", description="First/given name"),
                        "middle_name": gemini_types.Schema(type="STRING", description="Middle name"),
                        "family_name": gemini_types.Schema(type="STRING", description="Last/family name"),
                        "date_of_birth": gemini_types.Schema(type="STRING", description="YYYY-MM-DD"),
                        "gender": gemini_types.Schema(type="STRING", enum=["MALE", "FEMALE", "OTHER"]),
                        "document_number": gemini_types.Schema(type="STRING", description="Document number/ID number"),
                        "expiry_date": gemini_types.Schema(type="STRING", description="YYYY-MM-DD"),
                        "issuing_country": gemini_types.Schema(type="STRING", description="ISO 3166-1 alpha-3, e.g. 'USA'"),
                        "issuing_state": gemini_types.Schema(type="STRING", description="US state two-letter code if applicable"),
                        "address": gemini_types.Schema(type="STRING", description="Full address string as shown on document"),
                        "address_state": gemini_types.Schema(type="STRING", description="Province/state from address (in pinyin for Chinese documents)"),
                        "address_city": gemini_types.Schema(type="STRING", description="City from address (in pinyin)"),
                        "address_street1": gemini_types.Schema(type="STRING", description="Street address line 1 (in pinyin)"),
                        "address_street2": gemini_types.Schema(type="STRING", description="Street address line 2 — building/unit/room (in pinyin)"),
                        "bank_name": gemini_types.Schema(type="STRING", description="Bank name (for bank statements)"),
                        "account_last_four": gemini_types.Schema(type="STRING", description="Last 4 digits of account number"),
                    }
                )
            },
            required=["document_type", "extracted_fields"]
        )
    ),
]


# =============================================================================
# UI TOOLS (frontend widgets) - unchanged from previous version
# =============================================================================
UI_TOOLS = [
    gemini_types.FunctionDeclaration(
        name="present_options",
        description=(
            "Render clickable choice buttons in the user's chat UI. "
            "Call this tool FIRST — the widget appears immediately in the chat. "
            "AFTER the tool returns, speak a brief natural line. "
            "The 'question' parameter should be a short label (e.g. 'Account type'), "
            "NOT a long sentence — the widget shows above your text, so the user reads it first. "
            "Never say 'below', 'select from the following', or reference widget position. "
            "For enum fields, use the EXACT enum value (e.g. 'HIGH' not 'High risk') as the option label, "
            "so the value returned can be passed directly to the corresponding submit_* tool."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(type="STRING", description="The question or prompt shown above the options"),
                "options": gemini_types.Schema(
                    type="ARRAY",
                    items=gemini_types.Schema(type="STRING"),
                    description="2–8 choices. Use exact enum values where applicable."
                ),
                "type": gemini_types.Schema(type="STRING", enum=["single"], description="'single' = one choice (radio)"),
                "layout": gemini_types.Schema(type="STRING", enum=["buttons"], description="'buttons' = horizontal toggle buttons (default)"),
                "field_key": gemini_types.Schema(
                    type="STRING",
                    description="Optional business field identifier (e.g. 'risk_tolerance', 'account_type'). "
                                "When provided, the frontend can render field-specific styling and disclosure text."
                ),
            },
            required=["question", "options", "type"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_country_select",
        description=(
            "Render a searchable country picker (ISO 3166-1 alpha-3) in the user's chat UI. "
            "Use this for ANY country-of-... field: citizenship_country, birth_country, "
            "tax_id_country, passport issuing country, etc. "
            "DO NOT use `present_options` for country selection — that widget is for 2–8 enum choices, "
            "not the full 200+ country list. "
            "Call this tool FIRST. AFTER it returns, speak a brief natural line. "
            "The widget returns the selected ISO α-3 code (e.g. 'USA', 'CHN') as the user's answer; "
            "pass that value directly to the matching `submit_*` field."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(
                    type="STRING",
                    description="Short label shown above the picker (e.g. 'Country of birth', 'Citizenship')."
                ),
                "field_key": gemini_types.Schema(
                    type="STRING",
                    description="Business field identifier — 'citizenship_country', 'birth_country', "
                                "'tax_id_country', or 'issuing_country'."
                ),
                "default_country": gemini_types.Schema(
                    type="STRING",
                    description="Optional ISO α-3 to pre-select (e.g. 'USA' for citizenship in domestic flow, "
                                "or citizenship_country when collecting birth_country)."
                ),
            },
            required=["question", "field_key"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_date_input",
        description=(
            "Render a date/time picker widget in the user's chat UI. "
            "Call this tool FIRST. AFTER the tool returns, speak a brief line. "
            "The 'question' should be a short label, not a long description. "
            "Never say 'below' or reference the widget's screen position."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(type="STRING", description="The question or prompt shown above the input"),
                "format": gemini_types.Schema(type="STRING", enum=["date", "time", "datetime"], description="'date' = calendar (YYYY-MM-DD)"),
            },
            required=["question", "format"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_phone_input",
        description=(
            "Render a phone number input widget with country code selector. "
            "Call this tool FIRST. AFTER the tool returns, speak a brief line."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(type="STRING", description="The question or prompt shown above the phone input"),
            },
            required=["question"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_email_input",
        description=(
            "Render an email input widget. "
            "Call this tool FIRST. AFTER the tool returns, speak a brief line."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(type="STRING", description="The question or prompt shown above the email input"),
            },
            required=["question"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_address_input",
        description=(
            "Render a residential address input widget — dual mode. "
            "  - mode='US' (DOMESTIC sessions): state dropdown limited to 50 states + 6 territories, "
            "    ZIP validated (5 or 5+4), PO Boxes rejected. "
            "  - mode='INTERNATIONAL' (FOREIGNER sessions): country dropdown (ISO α-3) with search; "
            "    state/postal_code free-form; PO Box check skipped. "
            "Call this tool FIRST. AFTER the tool returns, speak a brief line. "
            "Do NOT ask for address components verbally."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(
                    type="STRING",
                    description="e.g. 'What is your current residential address?'"
                ),
                "mode": gemini_types.Schema(
                    type="STRING",
                    enum=["US", "INTERNATIONAL"],
                    description="Validator + UI mode. Defaults to 'US' if omitted (backwards compat with present_us_address_input)."
                ),
                "prefill": gemini_types.Schema(
                    type="OBJECT",
                    description="Optional prefilled address fields (e.g. from driver's license OCR)",
                    properties={
                        "country": {"type": "STRING"},
                        "street_address1": {"type": "STRING"},
                        "street_address2": {"type": "STRING"},
                        "city": {"type": "STRING"},
                        "state": {"type": "STRING"},
                        "postal_code": {"type": "STRING"},
                    }
                ),
            },
            required=["question"],
        )
    ),
    # Legacy alias — kept so older prompt files / cached schemas referencing the
    # old US-only name keep working. The handler routes both to the same widget.
    # TODO(M3): remove after 1 release of grace period.
    gemini_types.FunctionDeclaration(
        name="present_us_address_input",
        description=(
            "DEPRECATED — use present_address_input(mode='US', ...) instead. "
            "Kept temporarily for backwards compatibility; behaves identically to "
            "present_address_input with mode='US'."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(
                    type="STRING",
                    description="e.g. 'What is your current US residential address?'"
                ),
                "prefill": gemini_types.Schema(
                    type="OBJECT",
                    description="Optional prefilled address fields (e.g. from driver's license OCR)",
                    properties={
                        "street_address1": {"type": "STRING"},
                        "street_address2": {"type": "STRING"},
                        "city": {"type": "STRING"},
                        "state": {"type": "STRING"},
                        "postal_code": {"type": "STRING"},
                    }
                ),
            },
            required=["question"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_ssn_input",
        description=(
            "Render a Social Security Number input widget with masking and numeric keyboard. "
            "Displays as XXX-XX-XXXX, validates 9-digit format, encrypts before transmission. "
            "Call this tool FIRST. AFTER the tool returns, speak a brief line. NEVER ask the user to speak their SSN verbally."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(
                    type="STRING",
                    description="e.g. 'Please enter your Social Security Number'"
                ),
            },
            required=["question"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_employment_input",
        description=(
            "Render an employment information widget with fields: employer, position, years employed, and industry. "
            "Call this tool FIRST. AFTER the tool returns, speak a brief line. Do NOT ask the user to speak employment details verbally. "
            "Employment fields (employer, position, years, industry) are shown only when status is EMPLOYED or SELF_EMPLOYED. "
            "Unemployed, Retired, and Student status require only the employment_status field."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(
                    type="STRING",
                    description="The prompt shown above the employment form"
                ),
                "prefill": gemini_types.Schema(
                    type="OBJECT",
                    description="Optional pre-filled values from previous session or OCR",
                    properties={
                        "employment_status": {"type": "STRING"},
                        "employer": {"type": "STRING"},
                        "position_employed": {"type": "STRING"},
                        "years_employed": {"type": "INTEGER"},
                        "industry": {"type": "STRING"},
                    }
                ),
            },
            required=["question"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_drivers_license_review",
        description=(
            "Render a combined driver's license widget that shows OCR-extracted fields for user confirmation "
            "AND provides front/back upload slots in a single form. "
            "The user reviews and edits fields, uploads both sides (via file picker or camera), "
            "then clicks Submit. One response returns all confirmed fields + upload confirmations. "
            "Call this AFTER extract_document_info (or with empty fields if OCR was skipped). "
            "Do NOT call capture_document(purpose='upload') separately for DL — the widget handles uploads. "
            "Call this tool FIRST. AFTER the tool returns, tell the user to review, upload both sides, and click Confirm."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(type="STRING", description="The prompt shown above the form"),
                "fields": gemini_types.Schema(
                    type="OBJECT",
                    description="Extracted license fields. expiration_date is required (OCR may return this as expiry_date).",
                    properties={
                        "first_name": {"type": "STRING"},
                        "middle_name": {"type": "STRING"},
                        "last_name": {"type": "STRING"},
                        "date_of_birth": {"type": "STRING"},
                        "gender": {"type": "STRING"},
                        "expiration_date": {"type": "STRING", "description": "License expiration date in YYYY-MM-DD format (required). OCR may return this as expiry_date."},
                        "address_1": {"type": "STRING"},
                        "address_2": {"type": "STRING"},
                        "city": {"type": "STRING"},
                        "state": {"type": "STRING"},
                        "zip_code": {"type": "STRING"},
                    }
                ),
            },
            required=["question", "fields"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_disclosure",
        description=(
            "Render a disclosure questionnaire widget with all 5 regulatory questions "
            "(control person, FINRA/exchange affiliation, politically exposed person, "
            "trade authorization, trusted contact). "
            "Call this tool FIRST. AFTER the tool returns, speak a brief line. Do NOT ask disclosure questions verbally."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "questions": gemini_types.Schema(
                    type="ARRAY",
                    items=gemini_types.Schema(
                        type="OBJECT",
                        properties={
                            "question": gemini_types.Schema(type="STRING", description="The disclosure question text"),
                            "disclosure_type": gemini_types.Schema(
                                type="STRING",
                                enum=["company", "broker", "political", "behalf", "contact"],
                                description="Type of disclosure to determine follow-up fields. "
                                            "'company' = control person, 'broker' = FINRA affiliation, "
                                            "'political' = PEP, 'behalf' = trade authorization, "
                                            "'contact' = trusted contact (is_identify)."
                            ),
                        },
                        required=["question", "disclosure_type"],
                    ),
                    description="Array of all 5 disclosure question objects",
                ),
            },
            required=["questions"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_personal_info_input",
        description=(
            "Render a personal information form widget with fields: first name, middle name, last name, "
            "date of birth, and gender. "
            "Use this when the user does NOT have a driver's license, when OCR/camera is unavailable or fails, "
            "or when the user prefers to type their personal info manually. "
            "Call this tool FIRST. AFTER the tool returns, speak a brief line. "
            "The 'prefill' parameter can pre-populate fields from OCR results or a previous session. "
            "The 'address_prefill' parameter carries OCR-extracted address data (not shown in the form, "
            "but included in the confirmation result so you can submit it via submit_home_address)."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(
                    type="STRING",
                    description="The prompt shown above the form, e.g. 'Please confirm your personal information'"
                ),
                "prefill": gemini_types.Schema(
                    type="OBJECT",
                    description="Optional pre-filled field values from OCR or previous session",
                    properties={
                        "first_name": {"type": "STRING"},
                        "middle_name": {"type": "STRING"},
                        "last_name": {"type": "STRING"},
                        "date_of_birth": {"type": "STRING"},
                        "gender": {"type": "STRING"},
                    }
                ),
                "address_prefill": gemini_types.Schema(
                    type="OBJECT",
                    description="Optional OCR-extracted address data. Not shown in the form UI "
                                "but passed through in the confirmation result for submit_home_address.",
                    properties={
                        "address_1": {"type": "STRING"},
                        "address_2": {"type": "STRING"},
                        "city": {"type": "STRING"},
                        "state": {"type": "STRING"},
                        "zip_code": {"type": "STRING"},
                    }
                ),
            },
            required=["question"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_agreements",
        description=(
            "Render the agreements widget in the user's chat UI. "
            "The backend will fetch the required agreement list based on account_type and push it to the frontend. "
            "Call this tool FIRST. AFTER the tool returns, ask the user to review and accept the agreements. "
            "Only call submit_agreements after the user has clicked 'I Agree' in the widget."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(
                    type="STRING",
                    description="Prompt shown above the agreement list, e.g. 'Please review and accept the following agreements to open your account.'"
                ),
                "account_type": gemini_types.Schema(
                    type="STRING",
                    enum=["CASH", "MARGIN"],
                    description="Account type — determines which agreements are required."
                ),
            },
            required=["question", "account_type"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_progress_indicator",
        description=(
            "Render or refresh a visual progress bar with per-section checklist. "
            "Call this AFTER every query_progress call. "
            "Do NOT narrate the progress numerically — the widget shows it visually."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "percentage": gemini_types.Schema(
                    type="INTEGER",
                    description="Overall completion percentage 0-100 from query_progress"
                ),
                "sections": gemini_types.Schema(
                    type="ARRAY",
                    items=gemini_types.Schema(
                        type="OBJECT",
                        properties={
                            "name": gemini_types.Schema(type="STRING"),
                            "status": gemini_types.Schema(type="STRING", enum=["complete", "in_progress", "pending"]),
                        },
                        required=["name", "status"],
                    ),
                ),
                "status": gemini_types.Schema(
                    type="STRING",
                    enum=["NOT_STARTED", "COLLECTING", "SUBMITTED", "APPROVED"],
                    description="Overall application status"
                ),
                "branch": gemini_types.Schema(
                    type="STRING",
                    enum=["DOMESTIC", "FOREIGNER"],
                    description="Optional branch hint — lets the indicator render branch-specific step labels. "
                                "If omitted, frontend falls back to the session's branch."
                ),
            },
            required=["percentage", "sections", "status"],
        )
    ),

    # ─── Foreigner-branch widgets ──────────────────────────────────────────────
    # These widgets back the FOREIGNER identity / document flow and replace the
    # Domestic-only widgets (present_ssn_input, present_drivers_license_review,
    # present_us_address_input) for users without a US Social Security Number.
    gemini_types.FunctionDeclaration(
        name="present_tax_id_input",
        description=(
            "Render a masked Tax ID input widget for FOREIGNER branch identity collection. "
            "Behaves like present_ssn_input but without the 9-digit US format constraint — any "
            "alphanumeric tax identifier is accepted. The widget also includes an issuing-country "
            "dropdown (defaulting to the user's citizenship_country). "
            "Call this tool FIRST. AFTER the tool returns, speak a brief line. "
            "NEVER ask the user to recite their tax ID verbally."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(
                    type="STRING",
                    description="e.g. 'Please enter your home-country tax identifier'"
                ),
                "default_country": gemini_types.Schema(
                    type="STRING",
                    description="Default issuing country (ISO 3166-1 alpha-3). Pre-selects in the country dropdown. "
                                "Should match the user's citizenship_country."
                ),
            },
            required=["question"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_passport_input",
        description=(
            "Render a passport confirmation form: passport_number + expiration_date. "
            "Pass OCR-extracted fields from extract_document_info as the 'fields' parameter "
            "(document_number → passport_number, expiry_date → expiration_date). "
            "The user uploads their passport photo via chat — you see it, extract the fields, "
            "and present this form for review. "
            "Required on Foreigner branch AND on Domestic non-citizen sub-branches (PR / Visa). "
            "Call this tool FIRST. AFTER the tool returns, speak a brief line."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(
                    type="STRING",
                    description="e.g. 'Please confirm your passport details'"
                ),
                "fields": gemini_types.Schema(
                    type="OBJECT",
                    description="OCR-extracted fields from extract_document_info. "
                                "Pass document_number as passport_number, expiry_date as expiration_date.",
                    properties={
                        "passport_number": {"type": "STRING", "description": "Passport number (from document_number)"},
                        "expiration_date": {"type": "STRING", "description": "Expiration date YYYY-MM-DD (from expiry_date)"},
                    }
                ),
            },
            required=["question"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_visa_input",
        description=(
            "Render a US visa collection form: visa_type (H1B/L1/F1/O1/J1/Other) + expiration_date + "
            "visa image upload. After the user submits, the image is uploaded with file_type='visa'. "
            "Required on Domestic non-permanent-resident (visa-based) sub-branch. "
            "Call this tool FIRST. AFTER the tool returns, speak a brief line."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(
                    type="STRING",
                    description="e.g. 'Please enter your US visa details and upload your visa page'"
                ),
            },
            required=["question"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_green_card_input",
        description=(
            "Render a Green Card confirmation form: card_number (USCIS number) + expiration_date. "
            "Pass OCR-extracted fields from extract_document_info as the 'fields' parameter "
            "(document_number → card_number, expiry_date → expiration_date). "
            "Required on Domestic permanent-resident sub-branch (alongside passport). "
            "Call this tool FIRST. AFTER the tool returns, speak a brief line."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(
                    type="STRING",
                    description="e.g. 'Please confirm your Green Card details'"
                ),
                "fields": gemini_types.Schema(
                    type="OBJECT",
                    description="OCR-extracted fields. Pass document_number as card_number, expiry_date as expiration_date.",
                    properties={
                        "card_number": {"type": "STRING", "description": "USCIS number (from document_number)"},
                        "expiration_date": {"type": "STRING", "description": "Expiration date YYYY-MM-DD (from expiry_date)"},
                    }
                ),
            },
            required=["question"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_id_card_input",
        description=(
            "Render a Chinese national ID (身份证) confirmation form showing OCR-extracted fields "
            "(id_number, first_name, last_name, date_of_birth, gender) for user review. "
            "Pass real fields from extract_document_info as the 'fields' parameter. "
            "Required on Foreigner-Chinese sub-branch ONLY (citizenship_country='CHN'). "
            "Do NOT present this widget for any other citizenship. "
            "Call this tool FIRST. AFTER the tool returns, speak a brief line."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(
                    type="STRING",
                    description="e.g. '请输入您的身份证号码并上传身份证人像面照片' / 'Enter your Chinese ID and upload the photo side'"
                ),
                "fields": gemini_types.Schema(
                    type="OBJECT",
                    description="OCR-extracted fields from extract_document_info. "
                                "Pass document_number as id_number, given_name as first_name, family_name as last_name. "
                                "Address sub-fields (state, city, street1, street2) come from extract_document_info; "
                                "zip_code must be entered by the user.",
                    properties={
                        "id_number": {"type": "STRING", "description": "18-digit Chinese ID number (from document_number)"},
                        "first_name": {"type": "STRING", "description": "First/given name in pinyin"},
                        "last_name": {"type": "STRING", "description": "Last/family name in pinyin"},
                        "date_of_birth": {"type": "STRING"},
                        "gender": {"type": "STRING"},
                        "address_state": {"type": "STRING", "description": "Province/state in pinyin"},
                        "address_city": {"type": "STRING", "description": "City in pinyin"},
                        "address_street1": {"type": "STRING", "description": "Street address in pinyin"},
                        "address_street2": {"type": "STRING", "description": "Building/unit in pinyin"},
                    }
                ),
            },
            required=["question"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_address_proof_upload",
        description=(
            "Render an address-proof confirmation form for FOREIGNER branch. The user selects one of "
            "utility_bill / credit_card_statement / bank_statement from a dropdown. "
            "Pass OCR-extracted fields from extract_document_info as the 'fields' parameter "
            "(name_on_doc from full_name, address_on_doc from address). "
            "The user uploads the document via chat — you see it, extract the fields, "
            "and present this form for review. "
            "Required on every Foreigner sub-branch. "
            "Call this tool FIRST. AFTER the tool returns, speak a brief line."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(
                    type="STRING",
                    description="e.g. 'Please upload a recent address proof (utility bill, credit card statement, or bank statement)'"
                ),
                "fields": gemini_types.Schema(
                    type="OBJECT",
                    description="OCR-extracted fields from extract_document_info. "
                                "Pass full_name as name_on_doc, address as address_on_doc.",
                    properties={
                        "name_on_doc": {"type": "STRING", "description": "Name extracted from the document (from full_name)"},
                        "address_on_doc": {"type": "STRING", "description": "Address extracted from the document (from address)"},
                    }
                ),
            },
            required=["question"],
        )
    ),

    # ─── Shared financial / investment widgets (Domestic + Foreigner) ─────────
    # These replace the previous "5 × present_options" pattern for FINRA suitability
    # and the "4 × present_options" pattern for financial profile, cutting widget
    # turn count from 9 to 4 in the Financial+Investment sections.
    gemini_types.FunctionDeclaration(
        name="present_financial_range_input",
        description=(
            "Render a SINGLE combined form that asks all 3 financial buckets at once "
            "(annual_income, liquid_net_worth, total_net_worth) — one row per field with "
            "selectable bucket options. Submit returns all 3 {min, max} pairs in one answer. "
            "Call this tool ONCE — do NOT call 3 separate times. "
            "Default per-field buckets (when 'buckets' is omitted):\n"
            "  annual_income:    (0,25000) (25001,50000) (50001,100000) (100001,200000) "
            "(200001,300000) (300001,500000) (500001,1200000) (1200001,9999999)\n"
            "  liquid_net_worth: (0,50000) (50001,100000) (100001,200000) (200001,500000) "
            "(500001,1000000) (1000001,5000000) (5000001,9999999)\n"
            "  total_net_worth:  (0,50000) (50001,100000) (100001,200000) (200001,500000) "
            "(500001,1000000) (1000001,5000000) (5000001,9999999)\n"
            "Do NOT use present_options for these fields."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(
                    type="STRING",
                    description="Form title, e.g. 'Financial Profile'"
                ),
                "currency": gemini_types.Schema(
                    type="STRING",
                    description="ISO 4217 currency code. Defaults to USD."
                ),
                "buckets": gemini_types.Schema(
                    type="ARRAY",
                    description="Optional custom bucket list. If a plain array, shared across all 3 rows. "
                                "If an object keyed by field name (annual_income/liquid_net_worth/total_net_worth), "
                                "each field uses its own set. Each item is {label, min, max}. Omit to use per-field defaults.",
                    items=gemini_types.Schema(
                        type="OBJECT",
                        properties={
                            "label": gemini_types.Schema(type="STRING"),
                            "min": gemini_types.Schema(type="INTEGER"),
                            "max": gemini_types.Schema(type="INTEGER"),
                        },
                        required=["label", "min", "max"],
                    ),
                ),
            },
            required=["question"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_investment_profile_input",
        description=(
            "Render a single form widget that asks all 5 investment suitability questions at once "
            "(investment_experience, investment_objective, time_horizon, risk_tolerance, liquidity_needs). "
            "User picks one option per row; Submit returns all 5 values as enum strings. "
            "Call this tool FIRST. AFTER the tool returns, speak a brief line. "
            "Do NOT call present_options separately for each FINRA field — this widget replaces that pattern."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(
                    type="STRING",
                    description="Form title, e.g. 'Investment Profile (FINRA Rule 2111)'"
                ),
            },
            required=["question"],
        )
    ),
]


# =============================================================================
# ASSEMBLE
# =============================================================================
def get_all_tools():
    """Return all tool declarations grouped for Gemini Live API."""
    return [
        gemini_types.Tool(function_declarations=AUTH_TOOLS),
        gemini_types.Tool(function_declarations=ACCOUNT_TOOLS),
        gemini_types.Tool(function_declarations=SUBMIT_TOOLS),
        gemini_types.Tool(function_declarations=DOCUMENT_TOOLS),
        gemini_types.Tool(function_declarations=UI_TOOLS),
    ]