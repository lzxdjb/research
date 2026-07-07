# Open Account Skill

A trading account opening assistant skill that guides users through the complete account registration and opening process.

## Overview

This skill helps users open a trading account via a **progressive, step-by-step approach**. Rather than asking for all information at once, it collects data in batches, making the process less overwhelming and supporting **resume from where the user left off** for returning users.

## Features

- **Multi-method registration**: Supports registration via mobile phone or email
- **Progressive data collection**: Collects account information in small batches (1-3 fields per step)
- **Resume support**: Tracks `collect_fields` and `missing_fields` to allow users to continue from where they left off
- **File upload**: Supports document uploads for identity verification (driver's license, passport, etc.)
- **Progress tracking**: Real-time progress monitoring via the `query_progress` API

## Workflow

```
1. Account Registration (Authentication Center)
   └─ Send verification code → Login → Complete phone/email (if needed) → Get trading token

2. Fill in Account Information (Trading Account)
   └─ Query progress → Collect information in batches → Submit application

3. Submit Application
   └─ Review and submit → Wait for approval (1-3 business days)
```

## API Modules

All API calls are encapsulated in `scripts/api.py` (`TradeAPI` class):

| Method | Description |
|--------|-------------|
| `send_verification_code()` | Send verification code to phone or email |
| `login()` | Login with verification code |
| `get_user_info()` | Get user info and check phone/email binding |
| `update_mobile()` | Bind mobile phone number |
| `update_email()` | Bind email address |
| `get_trading_token()` | Get trading system access token |
| `query_progress()` | Query account opening progress |
| `collect_information()` | Submit collected account information |
| `submit_application()` | Submit account opening application |
| `upload_file()` | Upload verification documents |

## Usage

When the user mentions account opening (e.g., "帮我开户", "I want to open an account"), this skill is triggered automatically.

### Example Conversation Flow

1. Ask user for login method (phone/email)
2. Send verification code
3. User inputs code and logs in
4. Check phone/email binding status
5. Get trading token
6. Query progress and collect information progressively
7. Submit application when all fields are complete

## Environments

- **Test**: `https://test-lighthorse-trade.touzime.cn`
- **Production**: `https://interface.lighthorse.io`

Default: Test environment

## Not Yet Implemented

The following features are planned but not yet implemented:

1. **Non-US Account Opening** - The skill currently supports US applicants. Non-US applicants may encounter differences in required fields and compliance workflows that are not yet fully handled.

2. **OCR Document Recognition** - Automatic extraction of data from uploaded documents (e.g., driver's license, passport) via OCR is not yet available. Users must manually enter information from their documents.

3. **Complete Field Coverage** - Additional fields for non-US or other branching scenarios may not be fully documented in `fields.md`.
