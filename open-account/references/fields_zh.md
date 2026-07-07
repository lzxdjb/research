# 开户信息字段参考

## 字段输入说明

以下字段为**枚举类型**，必须从预设选项中选择，禁止自由输入：

| 字段 | 可选值 |
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

以下字段为**自由输入**类型：
- 文本字段：given_name, middle_name, family_name, employer, position_employed, firm_name, immediate_family, political_organization, commodity, agent_name
- 数字字段：years_employed, num_dependents
- 日期字段：date_of_birth, visa_expiration_date
- 特殊字段：social_security_number, company_symbols, other_source
- 对象字段：home_address, identify
- **文件对象字段**（需先调用 `api.upload_file()` 上传文件，获取 `fileId` 和 `minFileId` 后映射到此字段，再调用 `api.collect_information()` 提交）：
  - passport_photo、card_photo、affiliated_approval、drivers_license、government_issued_id

以下字段为**范围枚举类型**（年收入、流动资产、总资产的范围枚举，成对使用，共用同一组枚举选项）：

| 枚举选项 | *_usd_min | *_usd_max |
|----------|-----------|-----------|
| 0-10万 USD | 0 | 100000 |
| 10万-20万 USD | 100000 | 200000 |
| 20万-50万 USD | 200000 | 500000 |
| 50万-100万 USD | 500000 | 1000000 |
| 100万-500万 USD | 1000000 | 5000000 |
| 大于500万 USD | 5000000 | 999999999 |

> 注：annual_income_usd_min/max、liquid_net_worth_usd_min/max、total_net_worth_usd_min/max 均属于范围枚举类型，必须成对使用。用户选择一个枚举选项后，同时设置对应的 min 和 max 值。

---

## 账户类型（第1批）

| 字段 | 说明 | 可选值 |
|------|------|--------|
| account_type | 账户类型 | CASH, MARGIN |
| is_open_crypto | 是否开通加密货币 | true, false |

### JSON 示例

```json
{
  "account_type": "CASH",
  "is_open_crypto": true
}
```

---

## 个人信息（第2批）

| 字段 | 说明 | 可选值 |
|------|------|--------|
| given_name | 名 | （自由输入） |
| middle_name | 中间名 | （自由输入） |
| family_name | 姓 | （自由输入） |
| date_of_birth | 出生日期 | （自由输入，格式：YYYY-MM-DD） |
| gender | 性别 | MALE, FEMALE, OTHER |
| home_address | 住址 | 格式参见home_address对象,包括（国家、州/省、城市、邮编、街道信息）这些信息都要必填 |
| social_security_number | 社会安全号 | （自由输入） |
| citizenship_country | 国籍 | （自由输入，如：USA, CHN） |
| birth_country | 出生国家 | （自由输入，如：USA, CHN） |
| permanent_resident | 是否永久居民 | true, false |
| visa_type | 签证类型 | （自由输入，如：E1, E2, F1, B1） |
| visa_expiration_date | 签证过期日期 | （自由输入，格式：YYYY-MM-DD） |
| passport_photo | 护照照片 | 见下方对象 |
| card_photo | 身份证照片 | 见下方对象 |
| marital_status | 婚姻状态 | MARRIED, SINGLE, DIVORCED, WIDOWED |
| num_dependents | 供养人数 | （自由输入，正整数） |

### home_address 对象

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

### passport_photo / card_photo 对象

> **上传流程**：先调用 `api.upload_file(file_path="/path/to/file", is_need_min=True)` 获取 `fileId` 和 `minFileId`，再填入下方结构。

```json
{
  "min_file_id": "min0TJGLA0B30gJUIMEW20609",
  "file_id": "0TJGLA0B30gJUIMEW20609"
}
```

---

## 雇佣和财务信息（第3批）

| 字段 | 说明 | 可选值 |
|------|------|--------|
| employment_status | 就业状态 | EMPLOYED, SELF_EMPLOYED, UNEMPLOYED, RETIRED, STUDENT |
| employer | 雇主 | （自由输入） |
| position_employed | 职位 | （自由输入） |
| years_employed | 工作年限 | （自由输入，正整数） |
| industry | 行业 | Agriculture, Finance, Technology, Healthcare, Real Estate, Retail, Manufacturing, Energy, Transportation, Construction, Education, Legal, Other |
| funding_source | 资金来源 | Savings,Inheritance,Pension,Rental Income,Social Security,Other |
| other_source | 其他来源 | （自由输入） |
| annual_income_usd_min | 年收入下限（范围枚举，成对使用，见上方枚举选项表） | 见上方枚举选项表 |
| annual_income_usd_max | 年收入上限（范围枚举，成对使用，见上方枚举选项表） | 见上方枚举选项表 |
| liquid_net_worth_usd_min | 流动资产下限（范围枚举，成对使用，见上方枚举选项表） | 见上方枚举选项表 |
| liquid_net_worth_usd_max | 流动资产上限（范围枚举，成对使用，见上方枚举选项表） | 见上方枚举选项表 |
| total_net_worth_usd_min | 总资产下限（范围枚举，成对使用，见上方枚举选项表） | 见上方枚举选项表 |
| total_net_worth_usd_max | 总资产上限（范围枚举，成对使用，见上方枚举选项表） | 见上方枚举选项表 |

### JSON 示例

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

## 投资经验信息（第4批）

| 字段 | 说明 | 可选值 |
|------|------|--------|
| investment_experience | 投资经验 | EXTENSIVE, INTERMEDIATE, BASIC, NONE |
| investment_objective | 投资目标 | GROWTH, INCOME, PRESERVATION, SPECULATION |
| time_horizon | 投资期限 | LONGEST, AVERAGE, SHORT |
| risk_tolerance | 风险承受能力 | HIGH, MEDIUM, LOW |
| liquidity_needs | 流动性需求 | VERY_IMPORTANT, SOMEWHAT_IMPORTANT, NOT_IMPORTANT |
| experience_equity | 股票投资经验 | Less than 5 years, 5-10 years, More than 10 years |
| experience_margin | 保证金交易经验 | Less than 5 years, 5-10 years, More than 10 years |

### JSON 示例

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

## 披露和协议（第5批）

| 字段 | 说明 | 可选值 |
|------|------|--------|
| is_control_person | 是否为控制人 | true, false |
| company_symbols | 关联公司股票代码 | （自由输入，如：AAPL） |
| is_affiliated_exchange_or_finra | 是否关联交易所或FINRA | true, false |
| firm_name | 机构名称 | （自由输入） |
| affiliated_approval | 关联审批文件 | 见下方对象 |
| is_politically_exposed | 是否政治曝光人物 | true, false |
| immediate_family | 直系亲属 | （自由输入） |
| political_organization | 政治组织 | （自由输入） |
| is_commodity | 是否交易商品 | true, false |
| commodity | 商品描述 | （自由输入） |
| is_trade_authorization | 是否需要交易授权 | true, false |
| agent_name | 代理姓名 | （自由输入） |
| is_identify | 是否需要身份验证 | true, false |
| identify | 联系人信息 | 见下方对象 |
| agreements_accepted | 协议接受状态 | true, false |
| drivers_license | 驾照 | 见下方对象 |
| government_issued_id | 政府颁发的身份证件 | 见下方对象 |

### affiliated_approval 对象

> **上传流程**：先调用 `api.upload_file(file_path="/path/to/file", is_need_min=True)` 获取 `fileId` 和 `minFileId`，再填入下方结构。

```json
{
  "file_id": "6qxtWG0DsTVDSLjT1K0328",
  "min_file_id": "min6qxtWG0DsTVDSLjT1K0328"
}
```

### identify 对象

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

### drivers_license / government_issued_id 对象

> **上传流程**：front/back 字段需先分别调用 `api.upload_file(file_path="/path/to/file", is_need_min=True)` 获取 `fileId` 和 `minFileId`，再填入下方结构。

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

