# Account Opening Information Field Reference

## Field Input Instructions

The following fields are **enumerated types**, must be selected from preset options, free input not allowed:

| Field | Optional Values |
|------|--------|
| account_type | CASH, MARGIN |
| is_open_crypto | true, false |
| gender | MALE, FEMALE, OTHER |
| permanent_resident | true, false |
| marital_status | MARRIED, SINGLE, DIVORCED, WIDOWED |
| employment_status | EMPLOYED, SELF_EMPLOYED, UNEMPLOYED, RETIRED, STUDENT |
| industry | Agriculture, Business Management,Construction,Education,Environmental,Finance,Food & Hospitality,Gaming,Health Services,Information Technology,Insurance,Legal,Motor Vehicle,Real Estate,Security,Telecom,Transportation,Utilities,Other |
| funding_source | Savings,Inheritance,Pension,Rental Income,Social Security,Other |
| investment_experience | EXTENSIVE, GOOD, LIMITED, NONE |
| investment_objective | GROWTH, INCOME, CAPITAL_PRESERVATION, SPECULATION, OTHER |
| time_horizon | LONGEST, AVERAGE, SHORT |
| risk_tolerance | HIGH, MEDIUM, LOW |
| liquidity_needs | VERY_IMPORTANT, SOMEWHAT_IMPORTANT, NOT_IMPORTANT |
| is_control_person | true, false |
| is_affiliated_exchangeorfinra | true, false |
| is_politically_exposed | true, false |
| is_commodity | true, false |
| is_trade_authorization | true, false |
| is_identify | true, false |
| agreements_accepted | true, false |

The following fields are **free input** types:
- Text fields: gvie_name, middle_name, family_name, employer, position_employed, firm_name, immediate_family, political_organization, commodity, agent_name
- Numeric fields: years_employed, num_dependents
- Date fields: date_of_birth, visa_expiration_date
- Special fields: social_security_number, company_symbols, other_source
- Object fields: home_address, identify
- **File object fields** (must first call `api.upload_file()` to upload file, get `fileId` and `minFileId`, map to this field, then call `api.collect_information()` to submit):
  - passport_photo, card_photo, affiliated_approval, drivers_license, government_issued_id

The following fields are **range enumerated types** (annual income, liquid net worth, total net worth range enumerations, used in pairs, sharing the same set of enumerated options):

| Enumerated Option | *_usd_min | *_usd_max |
|----------|-----------|-----------|
| 0-100K USD | 0 | 100000 |
| 100K-200K USD | 100000 | 200000 |
| 200K-500K USD | 200000 | 500000 |
| 500K-1M USD | 500000 | 1000000 |
| 1M-5M USD | 1000000 | 5000000 |
| Greater than 5M USD | 5000000 | 999999999 |

> Note: annual_income_usd_min/max, liquid_net_worth_usd_min/max, total_net_worth_usd_min/max all belong to range enumerated types and must be used in pairs. After user selects an enumerated option, set both corresponding min and max values simultaneously.

---

## Account Type (Batch 1)

| Field | Description | Optional Values |
|------|------|--------|
| account_type | Account Type | CASH, MARGIN |
| is_open_crypto | Enable Cryptocurrency | true, false |

### JSON Example

```json
{
  "account_type": "CASH",
  "is_open_crypto": true
}
```

---

## Personal Information (Batch 2)

| Field | Description | Optional Values |
|------|------|--------|
| given_name | Given Name | (Free input) |
| middle_name | Middle Name | (Free input) |
| family_name | Family Name | (Free input) |
| date_of_birth | Date of Birth | (Free input, format: YYYY-MM-DD) |
| gender | Gender | MALE, FEMALE, OTHER |
| home_address | Home Address | Format see home_address object, including (country, state/province, city, postal code, street info), all required |
| social_security_number | Social Security Number | (Free input) |
| citizenship_country | Citizenship | (Free input, e.g.: USA, CHN) |
| birth_country | Birth Country | (Free input, e.g.: USA, CHN) |
| permanent_resident | Permanent Resident | true, false |
| visa_type | Visa Type | (Free input, e.g.: E1, E2, F1, B1) |
| visa_expiration_date | Visa Expiration Date | (Free input, format: YYYY-MM-DD) |
| passport_photo | Passport Photo | See object below |
| card_photo | ID Card Photo | See object below |
| marital_status | Marital Status | MARRIED, SINGLE, DIVORCED, WIDOWED |
| num_dependents | Number of Dependents | (Free input, positive integer) |

### home_address Object

```json
{
  "country": "USA",
  "state": "NY",
  "city": "NEW YORK",
  "postal_code": "10001",
  "street_address1": "",
  "street_address2": ""
}
```

### passport_photo / card_photo Object

> **Upload Process**: First call `api.upload_file(file_path="/path/to/file", is_need_min=True)` to get `fileId` and `minFileId`, then fill into the structure below.

```json
{
  "min_file_id": "min0TJGLA0B30gJUIMEW20609",
  "file_id": "0TJGLA0B30gJUIMEW20609"
}
```

---

## Employment and Financial Information (Batch 3)

| Field | Description | Optional Values |
|------|------|--------|
| employment_status | Employment Status | EMPLOYED, SELF_EMPLOYED, UNEMPLOYED, RETIRED, STUDENT |
| employer | Employer | (Free input) |
| position_employed | Position | (Free input) |
| years_employed | Years Employed | (Free input, positive integer) |
| industry | Industry | Agriculture, Finance, Technology, Healthcare, Real Estate, Retail, Manufacturing, Energy, Transportation, Construction, Education, Legal, Other |
| funding_source | Funding Source | Savings,Inheritance,Pension,Rental Income,Social Security,Other |
| other_source | Other Source | (Free input) |
| annual_income_usd_min | Annual Income Min (range enum, paired, see enum options table above) | See enum options table above |
| annual_income_usd_max | Annual Income Max (range enum, paired, see enum options table above) | See enum options table above |
| liquid_net_worth_usd_min | Liquid Net Worth Min (range enum, paired, see enum options table above) | See enum options table above |
| liquid_net_worth_usd_max | Liquid Net Worth Max (range enum, paired, see enum options table above) | See enum options table above |
| total_net_worth_usd_min | Total Net Worth Min (range enum, paired, see enum options table above) | See enum options table above |
| total_net_worth_usd_max | Total Net Worth Max (range enum, paired, see enum options table above) | See enum options table above |

### JSON Example

```json
{
  "employment_status": "EMPLOYED",
  "employer": "Example Corp",
  "position_employed": "Engineer",
  "years_employed": 5,
  "industry": "Finance",
  "funding_source": "Savings",
  "other_source": "",
  "annual_income_usd_min": 100000,
  "annual_income_usd_max": 200000,
  "liquid_net_worth_usd_min": 100000,
  "liquid_net_worth_usd_max": 200000,
  "total_net_worth_usd_min": 100000,
  "total_net_worth_usd_max": 200000
}
```

---

## Investment Experience Information (Batch 4)

| Field | Description | Optional Values |
|------|------|--------|
| investment_experience | Investment Experience | EXTENSIVE, INTERMEDIATE, BASIC, NONE |
| investment_objective | Investment Objective | GROWTH, INCOME, PRESERVATION, SPECULATION |
| time_horizon | Investment Time Horizon | LONGEST, AVERAGE, SHORT |
| risk_tolerance | Risk Tolerance | HIGH, MEDIUM, LOW |
| liquidity_needs | Liquidity Needs | VERY_IMPORTANT, SOMEWHAT_IMPORTANT, NOT_IMPORTANT |
| experience_equity | Equity Investment Experience | Less than 5 years, 5-10 years, More than 10 years |
| experience_margin | Margin Trading Experience | Less than 5 years, 5-10 years, More than 10 years |

### JSON Example

```json
{
  "investment_experience": "EXTENSIVE",
  "investment_objective": "GROWTH",
  "time_horizon": "LONGEST",
  "risk_tolerance": "HIGH",
  "liquidity_needs": "VERY_IMPORTANT",
  "experience_equity": "5-10 years",
  "experience_margin": "Less than 5 years"
}
```

---

## Disclosures and Agreements (Batch 5)

| Field | Description | Optional Values |
|------|------|--------|
| is_control_person | Control Person | true, false |
| company_symbols | Affiliated Company Stock Symbols | (Free input, e.g.: AAPL) |
| is_affiliated_exchange_or_finra | Affiliated with Exchange or FINRA | true, false |
| firm_name | Institution Name | (Free input) |
| affiliated_approval | Affiliated Approval Document | See object below |
| is_politically_exposed | Politically Exposed Person | true, false |
| immediate_family | Immediate Family | (Free input) |
| political_organization | Political Organization | (Free input) |
| is_commodity | Commodity Trading | true, false |
| commodity | Commodity Description | (Free input) |
| is_trade_authorization | Trade Authorization Needed | true, false |
| agent_name | Agent Name | (Free input) |
| is_identify | Identity Verification Needed | true, false |
| identify | Contact Information | See object below |
| agreements_accepted | Agreements Accepted | true, false |
| drivers_license | Driver's License | See object below |
| government_issued_id | Government Issued ID | See object below |

### affiliated_approval Object

> **Upload Process**: First call `api.upload_file(file_path="/path/to/file", is_need_min=True)` to get `fileId` and `minFileId`, then fill into the structure below.

```json
{
  "file_id": "6qxtWG0DsTVDSLjT1K0328",
  "min_file_id": "min6qxtWG0DsTVDSLjT1K0328"
}
```

### identify Object

```json
{
  "name": "Joe Lombardi",
  "phone_number": "8884561234",
  "phone_number_code": "1",
  "email": "rixmur2436@mailcupp.com",
  "birth": "1/1/1990",
  "address": "123 Main St",
  "relationship": "Brother"
}
```

### drivers_license / government_issued_id Object

> **Upload Process**: For front/back fields, first call `api.upload_file(file_path="/path/to/file", is_need_min=True)` separately to get `fileId` and `minFileId`, then fill into the structure below.

```json
{
  "back": {
    "file_id": "6qxtWG0DsTVDSLjT1K0328",
    "min_file_id": "min6qxtWG0DsTVDSLjT1K0328"
  },
  "front": {
    "file_id": "6qxtWG0DsTVDSLjT1K0328",
    "min_file_id": "min6qxtWG0DsTVDSLjT1K0328",
    "expire_date": "2024-08-30"
  }
}
```
