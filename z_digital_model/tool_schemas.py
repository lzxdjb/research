"""
Onboarding Tool Schemas
Defines all tool parameters for the account opening flow
"""

from google.genai import types as gemini_types

# Auth tools
AUTH_TOOLS = [
    gemini_types.FunctionDeclaration(
        name="send_verification_code",
        description="""Send a verification code to a mobile number or email address that the user has explicitly provided.
        IMPORTANT: You MUST have the user's actual contact information before calling this tool.
        - Ask the user which method they want to use (mobile or email) and wait for their answer.
        - Then ask for their mobile number or email address and wait for them to provide it.
        - Only AFTER the user gives you their contact info, call this tool with the actual value.
        NEVER call this tool with placeholder/guessed values or before the user has provided the contact information.""",
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "contact": gemini_types.Schema(type="STRING", description="Mobile number or email address — must be the actual value the user provided"),
                "contact_type": gemini_types.Schema(type="STRING", enum=["MOBILE", "EMAIL"], description="MOBILE or EMAIL"),
                "area_code": gemini_types.Schema(type="STRING", description="Country code (e.g., 1 for US), Only Number, Do not include other symbols such as the plus sign")
            },
            required=["contact", "contact_type"]
        )
    ),
    gemini_types.FunctionDeclaration(
        name="login_and_get_token",
        description="""Login with verification code AND get trading access token AND query account opening progress in one call.
        Call this after send_verification_code when user provides the code.
        This combines login + get_trading_token + query_progress into a single step.
        Returns: {status, missing_fields, collected_fields} where collected_fields are ALREADY submitted (do NOT re-submit these), missing_fields are still needed.
        Required before accessing account opening features.""",
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "contact": gemini_types.Schema(type="STRING", description="Phone number or email used for login"),
                "verification_code": gemini_types.Schema(type="STRING", description="6-digit verification code"),
                "contact_type": gemini_types.Schema(type="STRING", enum=["MOBILE", "EMAIL"], description="MOBILE or EMAIL"),
                "area_code": gemini_types.Schema(type="STRING", description="Country code (e.g., 1 for US), Do not include other symbols such as the plus sign")
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
        description="Bind or update email address with verification code.",
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
        description="Bind or update mobile number with verification code.",
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "phone": gemini_types.Schema(type="STRING", description="Mobile number"),
                "area_code": gemini_types.Schema(type="STRING", description="Country code"),
                "auth_code": gemini_types.Schema(type="STRING", description="Verification code sent to phone")
            },
            required=["phone", "area_code", "auth_code"]
        )
    ),
]

# Account opening tools
ACCOUNT_TOOLS = [
    gemini_types.FunctionDeclaration(
        name="query_progress",
        description="Query current account opening progress and status. Returns: status (current stage), missing_fields (fields still needed to submit), collected_fields (fields ALREADY submitted - do NOT re-submit these).",
        parameters=gemini_types.Schema(type="OBJECT", properties={})
    ),
    gemini_types.FunctionDeclaration(
        name="collect_information",
        description="""Submit collected account opening information in batch. Use this to collect and submit KYC data.
        IMPORTANT: Use EXACT field names as listed below. Common fields:
        - account_type: "CASH" or "MARGIN"
        - is_open_crypto: true or false
        - Personal: gvie_name, middle_name, family_name, date_of_birth (YYYY-MM-DD), gender (MALE/FEMALE/OTHER), marital_status (MARRIED/SINGLE/DIVORCED/WIDOWED), num_dependents, citizenship_country, birth_country, permanent_resident (true/false), visa_type, visa_expiration_date (YYYY-MM-DD), social_security_number
        - Address: home_address: {country, state, city, postal_code, street_address1, street_address2}
        - Employment: employment_status (EMPLOYED/SELF_EMPLOYED/UNEMPLOYED/RETIRED/STUDENT), employer, position_employed, years_employed (number), industry
        - Finance: funding_source (Savings/Inheritance/Pension/Rental Income/Social Security/Other), annual_income_usd_min/max, liquid_net_worth_usd_min/max, total_net_worth_usd_min/max (use enum pairs like 100000/200000)
        - Investment: investment_experience (EXTENSIVE/GOOD/LIMITED/NONE), investment_objective, time_horizon, risk_tolerance (HIGH/MEDIUM/LOW), liquidity_needs
        - Documents: passport_photo, card_photo, drivers_license, government_issued_id (all use {file_id, min_file_id} after calling upload_file)
        """,
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "data": gemini_types.Schema(
                    type="OBJECT",
                    description="Account opening data object with EXACT field names",
                    properties={
                        "account_type": gemini_types.Schema(type="STRING", enum=["CASH", "MARGIN"]),
                        "is_open_crypto": gemini_types.Schema(type="BOOLEAN"),
                        "gvie_name": gemini_types.Schema(type="STRING"),
                        "middle_name": gemini_types.Schema(type="STRING"),
                        "family_name": gemini_types.Schema(type="STRING"),
                        "date_of_birth": gemini_types.Schema(type="STRING"),
                        "gender": gemini_types.Schema(type="STRING", enum=["MALE", "FEMALE", "OTHER"]),
                        "marital_status": gemini_types.Schema(type="STRING", enum=["MARRIED", "SINGLE", "DIVORCED", "WIDOWED"]),
                        "num_dependents": gemini_types.Schema(type="INTEGER"),
                        "citizenship_country": gemini_types.Schema(type="STRING"),
                        "birth_country": gemini_types.Schema(type="STRING"),
                        "permanent_resident": gemini_types.Schema(type="BOOLEAN"),
                        "visa_type": gemini_types.Schema(type="STRING"),
                        "visa_expiration_date": gemini_types.Schema(type="STRING"),
                        "social_security_number": gemini_types.Schema(type="STRING"),
                        "home_address": gemini_types.Schema(
                            type="OBJECT",
                            properties={
                                "country": {"type": "STRING"},
                                "state": {"type": "STRING"},
                                "city": {"type": "STRING"},
                                "postal_code": {"type": "STRING"},
                                "street_address1": {"type": "STRING"},
                                "street_address2": {"type": "STRING"}
                            }
                        ),
                        "employment_status": gemini_types.Schema(type="STRING", enum=["EMPLOYED", "SELF_EMPLOYED", "UNEMPLOYED", "RETIRED", "STUDENT"]),
                        "employer": gemini_types.Schema(type="STRING"),
                        "position_employed": gemini_types.Schema(type="STRING"),
                        "years_employed": gemini_types.Schema(type="INTEGER"),
                        "industry": gemini_types.Schema(type="STRING"),
                        "funding_source": gemini_types.Schema(type="STRING", enum=["Savings", "Inheritance", "Pension", "Rental Income", "Social Security", "Other"]),
                        "other_source": gemini_types.Schema(type="STRING"),
                        "annual_income_usd_min": gemini_types.Schema(type="INTEGER", description="Income range min: use 0, 100000, 200000, 500000, 1000000, or 5000000"),
                        "annual_income_usd_max": gemini_types.Schema(type="INTEGER", description="Income range max: use 100000, 200000, 500000, 1000000, 5000000, or 999999999"),
                        "liquid_net_worth_usd_min": gemini_types.Schema(type="INTEGER", description="Net worth range min: use 0, 100000, 200000, 500000, 1000000, or 5000000"),
                        "liquid_net_worth_usd_max": gemini_types.Schema(type="INTEGER", description="Net worth range max: use 100000, 200000, 500000, 1000000, 5000000, or 999999999"),
                        "total_net_worth_usd_min": gemini_types.Schema(type="INTEGER", description="Net worth range min: use 0, 100000, 200000, 500000, 1000000, or 5000000"),
                        "total_net_worth_usd_max": gemini_types.Schema(type="INTEGER", description="Net worth range max: use 100000, 200000, 500000, 1000000, 5000000, or 999999999"),
                        "investment_experience": gemini_types.Schema(type="STRING", enum=["EXTENSIVE", "GOOD", "LIMITED", "NONE"]),
                        "investment_objective": gemini_types.Schema(type="STRING"),
                        "time_horizon": gemini_types.Schema(type="STRING"),
                        "risk_tolerance": gemini_types.Schema(type="STRING", enum=["HIGH", "MEDIUM", "LOW"]),
                        "liquidity_needs": gemini_types.Schema(type="STRING"),
                        "is_control_person": gemini_types.Schema(type="BOOLEAN"),
                        "company_symbols": gemini_types.Schema(type="STRING"),
                        "is_affiliated_exchangeorfinra": gemini_types.Schema(type="BOOLEAN"),
                        "firm_name": gemini_types.Schema(type="STRING"),
                        "is_politically_exposed": gemini_types.Schema(type="BOOLEAN"),
                        "immediate_family": gemini_types.Schema(type="STRING"),
                        "political_organization": gemini_types.Schema(type="STRING"),
                        "is_commodity": gemini_types.Schema(type="BOOLEAN"),
                        "commodity": gemini_types.Schema(type="STRING"),
                        "is_trade_authorization": gemini_types.Schema(type="BOOLEAN"),
                        "agent_name": gemini_types.Schema(type="STRING"),
                        "is_identify": gemini_types.Schema(type="BOOLEAN"),
                        "agreements_accepted": gemini_types.Schema(type="BOOLEAN"),
                        "passport_photo": gemini_types.Schema(type="OBJECT", description="{file_id, min_file_id}"),
                        "card_photo": gemini_types.Schema(type="OBJECT", description="{file_id, min_file_id}"),
                        "drivers_license": gemini_types.Schema(type="OBJECT"),
                        "government_issued_id": gemini_types.Schema(type="OBJECT"),
                    }
                )
            },
            required=["data"]
        )
    ),
    gemini_types.FunctionDeclaration(
        name="submit_application",
        description="Submit the complete account opening application after all information is collected.",
        parameters=gemini_types.Schema(type="OBJECT", properties={})
    ),
    gemini_types.FunctionDeclaration(
        name="upload_file",
        description="Upload a document file (ID, passport, bank statement, etc.) for account opening. Returns file_id and min_file_id for use in collect_information.",
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "file_data": gemini_types.Schema(type="STRING", description="Base64 encoded file data"),
                "filename": gemini_types.Schema(type="STRING", description="Original filename with extension"),
                "is_need_min": gemini_types.Schema(type="BOOLEAN", description="Whether to generate thumbnail")
            },
            required=["file_data", "filename"]
        )
    ),
    gemini_types.FunctionDeclaration(
        name="capture_document",
        description="""Tell the frontend to capture a photo from the live camera feed, upload it to the server, and return file_id / min_file_id.

IMPORTANT: After calling this tool:
1. Wait for a text message from the frontend containing "CAPTURE_RESULT:"
2. That message will contain File URL, File ID, and Min File ID
3. Extract those values and use them in subsequent calls

Do NOT continue speaking or call any other tool until you receive the CAPTURE_RESULT message.""",
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "doc_type": gemini_types.Schema(type="STRING", description="Type of document: drivers_license_front, drivers_license_back, passport, government_issued_id")
            },
            required=["doc_type"]
        )
    ),
]


# Document extraction tool
DOCUMENT_TOOLS = [
    gemini_types.FunctionDeclaration(
        name="extract_document_info",
        description="""Extract structured information from a document image (ID, passport, driver's license, bank statement).
        Call this when the user shows a document to the camera. The extracted fields should be used to pre-fill account opening forms.
        Extract all visible fields including gender from driver's licenses. Return MALE/FEMALE/OTHER for gender when available.""",
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "document_type": gemini_types.Schema(
                    type="STRING",
                    enum=["ID_CARD", "PASSPORT", "DRIVERS_LICENSE", "BANK_STATEMENT", "OTHER"],
                    description="Type of document"
                ),
                "document_image": gemini_types.Schema(
                    type="STRING",
                    description="Base64 encoded document image (optional, usually passed automatically from camera)"
                ),
                "extracted_fields": gemini_types.Schema(
                    type="OBJECT",
                    description="Fields extracted from the document",
                    properties={
                        "full_name": gemini_types.Schema(type="STRING", description="Full name as shown on document"),
                        "given_name": gemini_types.Schema(type="STRING", description="First/given name"),
                        "family_name": gemini_types.Schema(type="STRING", description="Last/family name"),
                        "date_of_birth": gemini_types.Schema(type="STRING", description="Date of birth (YYYY-MM-DD)"),
                        "gender": gemini_types.Schema(type="STRING", description="Gender if visible on document (MALE/FEMALE/OTHER)"),
                        "document_number": gemini_types.Schema(type="STRING", description="Document number/ID number"),
                        "expiry_date": gemini_types.Schema(type="STRING", description="Document expiry date (YYYY-MM-DD)"),
                        "issuing_country": gemini_types.Schema(type="STRING", description="Issuing country code"),
                        "address": gemini_types.Schema(type="STRING", description="Address if shown on document"),
                        "bank_name": gemini_types.Schema(type="STRING", description="Bank name (for bank statements)"),
                        "account_last_four": gemini_types.Schema(type="STRING", description="Last 4 digits of account number"),
                    }
                )
            },
            required=["document_type", "extracted_fields"]
        )
    ),
]


# UI Widget tools
UI_TOOLS = [
    gemini_types.FunctionDeclaration(
        name="present_options",
        description=(
            "Render clickable choice buttons in the user's chat UI. "
            "Call this tool FIRST — the widget appears immediately when the tool is called. "
            "Do NOT say 'I'll show you options' or 'please select from the options below' before calling this tool. "
            "The tool call itself is what makes the widget appear. "
            "Call this tool first, receive the 'Widget displayed.' response, THEN speak your verbal line."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(type="STRING", description="The question or prompt shown above the options"),
                "options": gemini_types.Schema(
                    type="ARRAY",
                    items=gemini_types.Schema(type="STRING"),
                    description="2–8 choices for the user to pick from",
                ),
                "type": gemini_types.Schema(type="STRING", enum=["single", "multi"], description="'single' = one choice (radio), 'multi' = multiple choices (checkboxes)"),
                "layout": gemini_types.Schema(type="STRING", enum=["buttons", "checklist"], description="'buttons' = horizontal toggle buttons (default), 'checklist' = vertical checkbox list (better for long lists)"),
            },
            required=["question", "options", "type"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_date_input",
        description=(
            "Render a date/time picker widget in the user's chat UI. "
            "Call this tool FIRST — the widget appears immediately when the tool is called. "
            "Do NOT say 'please pick a date' or describe the date picker before calling this tool. "
            "The tool call itself is what makes the widget appear. "
            "Call this tool first, receive the 'Widget displayed.' response, THEN speak your verbal line."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(type="STRING", description="The question or prompt shown above the input (e.g. 'Date of birth?')"),
                "format": gemini_types.Schema(type="STRING", enum=["date", "time", "datetime"], description="'date' = calendar (YYYY-MM-DD), 'time' = clock (HH:MM), 'datetime' = both"),
            },
            required=["question", "format"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_phone_input",
        description=(
            "Render a phone number input widget with country code selector in the user's chat UI. "
            "Call this tool FIRST — the widget appears immediately when the tool is called. "
            "Do NOT say 'please enter your phone number' or describe the input before calling this tool. "
            "The tool call itself is what makes the widget appear. "
            "Call this tool first, receive the 'Widget displayed.' response, THEN speak your verbal line."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(type="STRING", description="The question or prompt shown above the phone input (e.g. 'Enter your mobile number')"),
            },
            required=["question"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_email_input",
        description=(
            "Render an email input widget in the user's chat UI. "
            "Call this tool FIRST — the widget appears immediately when the tool is called. "
            "Do NOT say 'please enter your email' or describe the input before calling this tool. "
            "The tool call itself is what makes the widget appear. "
            "Call this tool first, receive the 'Widget displayed.' response, THEN speak your verbal line."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(type="STRING", description="The question or prompt shown above the email input (e.g. 'Enter your email address')"),
            },
            required=["question"],
        )
    ),
    gemini_types.FunctionDeclaration(
        name="present_drivers_license_review",
        description=(
            "Render a driver's license review widget in the user's chat UI. "
            "Call this tool FIRST — the widget appears immediately when the tool is called. "
            "The tool call itself is what makes the widget appear. "
            "Call this tool first, receive the 'Widget displayed.' response, THEN speak your verbal line."
        ),
        parameters=gemini_types.Schema(
            type="OBJECT",
            properties={
                "question": gemini_types.Schema(type="STRING", description="The prompt shown above the form"),
                "fields": gemini_types.Schema(
                    type="OBJECT",
                    description="Extracted license fields",
                    properties={
                        "first_name": {"type": "STRING"},
                        "middle_name": {"type": "STRING"},
                        "last_name": {"type": "STRING"},
                        "date_of_birth": {"type": "STRING"},
                        "gender": {"type": "STRING"},
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
            "Render a disclosure questionnaire widget in the user's chat UI. "
            "Call this tool FIRST — the widget appears immediately when the tool is called. "
            "Do NOT ask disclosure questions verbally. The tool call itself is what makes the widget appear. "
            "Call this tool first, receive the 'Widget displayed.' response, THEN speak your verbal line."
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
                            "disclosure_type": gemini_types.Schema(type="STRING", enum=["company", "broker", "political", "behalf", "contact"], description="Type of disclosure to determine follow-up fields"),
                        },
                        required=["question", "disclosure_type"],
                    ),
                    description="Array of all 5 disclosure question objects",
                ),
            },
            required=["questions"],
        )
    ),
]


def get_all_tools():
    return [
        gemini_types.Tool(function_declarations=AUTH_TOOLS),
        gemini_types.Tool(function_declarations=ACCOUNT_TOOLS),
        gemini_types.Tool(function_declarations=DOCUMENT_TOOLS),
        gemini_types.Tool(function_declarations=UI_TOOLS),
    ]