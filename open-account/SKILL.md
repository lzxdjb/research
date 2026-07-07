---
name: open-account
description: |
  Trading Account Opening Skill. Triggered when users mention "account opening", "open a account", "register a trading account", "new user account opening", or need help completing the trading system account opening process.
  This skill guides users through the entire process from account registration to application submission.
  Applicable to customers who want to open a trading account but don't know how to proceed, assisted passively by customer service or AI assistants.
triggers:
  - "open account"
  - "help me open an account"
  - "open a trading account"
  - "register a trading account"
  - "new user account opening process"
  - "trading account registration"
compatibility:
  - bash
environment:
  test: "https://test-lighthorse-trade.touzime.cn"
  production: "https://interface.lighthorse.io"
bundled_resources:
  - path: scripts/api.py
    description: |
      API encapsulation module. When the skill executes, directly call the following methods without manually constructing requests:
      - api.send_verification_code(contact, contact_type, area_code) - Send verification code
      - api.login(contact, verification_code, contact_type, area_code) - Login
      - api.get_trading_token() - Get trading access_token
      - api.get_user_info() - Get user information
      - api.update_email(email, auth_code) - Update email
      - api.update_mobile(phone, area_code, auth_code) - Update mobile number
      - api.query_progress() - Query account opening progress
      - api.collect_information(data) - Submit collected account opening information
      - api.submit_application() - Submit account opening application
      - api.upload_file(file_path, is_need_min) - Account opening file upload
  - path: references/fields.md
    description: |
      Account opening information field reference. Contains field descriptions, JSON examples for all batches, and optional value lists for enumerated types. Detailed examples referenced in sections 2.2 and 2.3 are in this file.
---

# Trading Account Opening Skill

This skill helps users complete the account opening process for the trading system. The skill uses a **progressive guidance** approach, only asking users for information needed for the current step.

> **Note**: All API calls are encapsulated in the `TradeAPI` class in `scripts/api.py`, working through fixed request headers and authentication mechanisms defined in `scripts/api.py`. When executing the skill, simply call the encapsulated methods directly.

## Account Opening Process Overview

```
1. Register Account (Authentication Center)
   └─ Send SMS/Email verification code → Login → Complete mobile/email (if needed) → Get trading access token
2. Fill in Account Opening Information (Trading Account Opening)
   └─ Query progress → Collect personal information/funding sources (incremental, multiple times) → Submit account opening application
```

---

## Step 1: Account Registration

### 1.1 Ask User for Login Method

Ask the user which method they want to use to register/login:

- **Mobile Login**: Requires user to input country code + mobile number (e.g.: +1 2503202313), country code and mobile number separated by space, country code required, mobile number required
- **Email Login**: Requires user to input email address, email format validated, email required

### 1.2 Send Verification Code

Based on user selection, call `api.send_verification_code()`:

```python
# Mobile
api.send_verification_code(contact="user mobile number", contact_type="MOBILE", area_code="user input country code")
# Email
api.send_verification_code(contact="user email", contact_type="EMAIL")
```

### 1.3 User Enters Verification Code

Please inform the user of the received verification code (usually 6 digits) and ask the user to input it.

### 1.4 Complete Login

Call `api.login()`:

```python
# Mobile login
api.login(contact="user mobile number", verification_code="user input verification code", contact_type="MOBILE", area_code="user input country code")
# Email login
api.login(contact="user email", verification_code="user input verification code", contact_type="EMAIL")
```

After successful login, the `TradeAPI` instance automatically records `userId` and `token`.

### 1.5 Complete User Information

After login, confirm whether the user's mobile number and email are already bound. Call `api.get_user_info()` to get user information:

```python
api.get_user_info()
```

**Key fields in returned data:**
| Field | Description |
|------|------|
| phone | Mobile number (empty means needs binding) |
| mobile | is deprecated (empty not means need binding,just ignore) |
| emailVerify  | Email verification status (verified = 1, not verified = 0) |

**Decision Logic:**
- Only check `phone` and `emailVerify` fields, do not check other fields
- If `phone` is empty, user needs to input mobile number, country code, SMS verification code,and bind mobile number
- If `emailVerify` is 0, user needs to input email, email verification code,and bind email
- If neither condition is met, both mobile and email are bound, no need to complete information, proceed directly to next step to get trading token

**Mobile Binding Example:**
```python
# 1. First send verification code to new mobile
api.send_verification_code(contact="new mobile number", contact_type="MOBILE", area_code="user input country code")
# 2. Second Update mobile number after user inputs verification code
api.update_mobile(phone="new mobile number", area_code="user input country code", auth_code="user input verification code")
```

**Email Binding Example:**
```python
# 1. First send verification code to new email
api.send_verification_code(contact="new email", contact_type="EMAIL")
# 2. Second Update email after user inputs verification code
api.update_email(email="new email", auth_code="user input verification code")
```

### 1.6 Get Trading Access Token

Call `api.get_trading_token()`:

```python
api.get_trading_token()
```

After success, the `TradeAPI` instance automatically records `access_token`.

---

## Step 2: Fill in Account Opening Information

### 2.1 Query Current Account Opening Progress

Call `api.query_progress()`:

```python
api.query_progress()
```

**Returned Data Interpretation:**

| status | Meaning | Next Step |
|--------|------|--------|
| NOT_APPLIED | Not applied | Guide user to start filling information |
| COLLECTING | Collecting account opening information | Continue filling information |
| AUDITING | Account opening under review | Inform user to wait for review |
| SUPPLEMENTARY_REQUIRED | Additional information required | Inform user to supplement information or materials, use audit_comment to prompt user |
| REJECTED | Account opening rejected | Inform user application was rejected |
| OPENED | Account opened successfully | Account opening complete |

**Incremental Resumption Fields (supports resuming from where user left off on second entry):**

When status is COLLECTING, the returned data also contains the following two fields to support users resuming without starting over on their second entry:

| Field | Description |
|------|------|
| collected_fields | Fields user has already filled (e.g. `["given_name", "family_name", "date_of_birth"]`) |
| missing_fields | Fields user has not yet filled (e.g. `["gender", "visa_type", "passport_photo"]`) |

**Decision Logic:**
- If `missing_fields` is empty, all required information is filled, can directly call `api.submit_application()` to submit account opening application
- If `missing_fields` is not empty, there are still unfilled fields, **select 1-3 fields from `missing_fields` in batches to continue guiding user to fill in**, do NOT ask about fields already filled (`collected_fields` fields) again
- When user enters account opening for the second time, continue directly from `missing_fields` fields without starting over

### 2.2 Batch Information Collection

When status is COLLECTING, use progressive guidance to have user fill in information, **only ask 1-3 fields at a time**, immediately call `api.collect_information()` after user completes.

> **Important**:
> - Enumerated type fields must be selected from preset options, free input not allowed
> - When prompting user, only show field description, not field name
> - For detailed field descriptions and optional values, refer to `references/fields.md`
> - **Only the first time** calling `api.collect_information()`, add an `application_source` field to the data object to identify the application source. This field is not needed for subsequent calls.

Example (first submission only):
```python
api.collect_information(data={
    "application_source": {
        "source": "AI_OPEN_ACCOUNT_SKILL",
        "packageName": "open-account-skill",
        "appVersion": "1.0.0"
    },
    # Other fields to submit
    "given_name": "li"
})
```

**File Upload Process**: Some fields are file objects, need to upload files first then submit:
1. User prepares file (PDF, JPG, PNG, etc.)
2. Call `api.upload_file(file_path="/path/to/file", is_need_min=True)` to upload
3. Get returned `fileId` and `minFileId`
4. Fill into corresponding file object field, then call `api.collect_information()` to submit



---

#### Account Type (2 fields, can be filled at once)

| Field Description | Optional Values |
|---------|--------|
| Account Type | CASH, MARGIN |
| Enable Cryptocurrency | true, false |

#### Personal Information (1-3 fields at a time)

| Step | Field Description | Optional Values/Format |
|------|---------|-------------|
| 1 | Name (given name, middle name, family name) | Free input |
| 2 | Date of birth, Gender | Format: YYYY-MM-DD; Optional values: MALE, FEMALE, OTHER |
| 3 | Marital Status, Number of Dependents | Optional values: MARRIED, SINGLE, DIVORCED, WIDOWED; Number of dependents is a positive integer |
| 4 | Citizenship, Birth Country, Permanent Resident | Free input country code; Optional values: true, false |
| 5 | Home Address | Must include country, state/province, city, postal code, street info; Format see fields.md |
| 6 | Social Security Number | Format: XXX-XX-XXXX |
| 7 | Visa Type, Visa Expiration Date | Free input visa type (e.g. E1, E2, F1, B1); Format: YYYY-MM-DD |
| 8 | Passport Photo | Need to upload file first to get ID |
| 9 | ID Card Photo | Need to upload file first to get ID |

#### Employment and Financial Information (1-3 fields at a time)

| Step | Field Description | Optional Values/Format |
|------|---------|-------------|
| 1 | Employment Status | All optional values see fields.md |
| 2 | Employer Name, Position, Years Employed, Industry | Years employed is a positive integer; Industry optional values see fields.md |
| 3 | Funding Source, Other Source | Funding source optional values see fields.md; Other source is free input |
| 4 | Annual Income Range (USD) | All optional values see fields.md |
| 5 | Liquid Net Worth Range (USD) | Same as above |
| 6 | Total Net Worth Range (USD) | Same as above |

#### Investment Experience Information (1-3 fields at a time)

| Step | Field Description | Optional Values |
|------|---------|--------|
| 1 | Investment Experience, Investment Objective | All optional values see fields.md |
| 2 | Investment Time Horizon, Risk Tolerance, Liquidity Needs | All optional values see fields.md |
| 3 | Equity Investment Experience, Margin Trading Experience | All optional values see fields.md |

#### Disclosures and Agreements (1-3 fields at a time)

| Step | Field Description | Optional Values/Format |
|------|---------|-------------|
| 1 | Control Person, Affiliated Company Stock Symbols | true, false; Stock symbols free input |
| 2 | Affiliated with Exchange or FINRA, Institution Name | true, false; Institution name free input |
| 3 | Affiliated Approval Document | Need to upload file first to get ID |
| 4 | Politically Exposed Person, Immediate Family, Political Organization | true, false; Others free input |
| 5 | Commodity Trading, Commodity Description | true, false; Description free input |
| 6 | Trade Authorization Needed, Agent Name | true, false; Name free input |
| 7 | Identity Verification Needed, Contact Information | true, false; Contact must include name, phone, email, date of birth, address, relationship |
| 8 | Agreements Accepted | true, false |
| 9 | Driver's License | Need to upload front and back files separately |
| 10 | Government Issued ID | Need to upload front and back files separately |

**For field input restrictions, format, and optional values, strictly refer to:** `references/fields.md`

### 2.3 Submit Collected Information

After user completes filling fields for the current step, immediately call `api.collect_information()`:

```python
api.collect_information(data=collected information dictionary)
```
**Please strictly refer to the json format passed to the interface:** `references/fields.md`

If returns `{"s": "ok"}` means submission successful, continue guiding user to fill next step. Otherwise, based on `s` and `errmsg` in the returned object, inform user of fill error to retry.

---

## Step 3: Submit Account Opening Application

When all required information is filled, call `api.submit_application()`:

```python
api.submit_application()
```

**Returned Data Interpretation:**

| s | Meaning | Next Step |
|---|------|--------|
| ok | Submission successful, awaiting review | Inform user under review |
| other | Submission failed | Check missing_fields, inform user which information is still missing |

**After successful submission, inform user:**
> Account opening application has been submitted. The trading system will review it within 1-3 business days. Once approved, you can trade using the trading system. Review results will be notified via SMS/email.

---

## Error Handling

### Authentication Center Error Codes

| code | msg | Handling |
|------|-----|----------|
| -100005 | Google recaptcha V3 verification failed | Retry or contact customer service |
| -100006 | Google recaptcha V2 verification failed | Retry or contact customer service |
| -100007 | captcha-sider risk block | Operation too frequent, please try again later |
| -100008 | Sending verification code prohibited | Today's verification code sending limit exhausted |

### Trading Account Opening Errors

- **status is not ok**: Check `i18nMsg` or error description in API docs
- **missing_fields returns non-empty array**: Inform user which required fields are still missing

---

## Environment Switching

Unless otherwise specified, the **test environment** is used by default.

- Test environment: `https://test-lighthorse-trade.touzime.cn`
- Production environment: `https://interface.lighthorse.io`

To switch, please ask user at the start of the skill, or determine based on context.

## API Call Example

```python
from scripts.api import TradeAPI

# Initialize API (default test environment)
api = TradeAPI(environment="test")

# Send verification code
api.send_verification_code(contact="13800138000", contact_type="MOBILE", area_code="86")

# User inputs verification code to login
api.login(contact="13800138000", verification_code="123456", contact_type="MOBILE", area_code="86")

# Get user info, check if mobile and email are bound
user_info = api.get_user_info()
if not user_info.get("data", {}).get("phone"):
    # Mobile is empty, needs binding
    api.send_verification_code(contact="new mobile number", contact_type="MOBILE", area_code="mobile country code")
    api.update_mobile(phone="new mobile number", area_code="mobile country code", auth_code="verification code")

if user_info.get("data", {}).get("emailVerify") == 0:
    # Email is empty, needs binding
    api.send_verification_code(contact="new email", contact_type="EMAIL")
    api.update_email(email="new email", auth_code="verification code")

# Get trading token
api.get_trading_token()

# Query progress
api.query_progress()

# Submit collected information (example: personal information)
api.collect_information(data={
    "given_name": "li",
    "middle_name": "ll",
    "family_name": "ll",
    "date_of_birth": "2005-03-28",
    "gender": "FEMALE",
    "visa_type": "E1",
    "visa_expiration_date": "2025-12-09",
    "passport_photo": {
        "min_file_id": "min0TJGLA0B30gJUIMEW20609",
        "file_id": "0TJGLA0B30gJUIMEW20609"
    }
})

# Submit account opening application
api.submit_application()

# Upload account opening file
api.upload_file(file_path="/path/to/document.pdf", is_need_min=False)
```
