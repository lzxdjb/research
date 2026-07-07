---
name: open-account
description: |
  交易开户技能。当用户提到「开户」、「开通交易账户」、「注册交易账号」、「新用户开户」或需要帮助完成交易系统开户流程时触发。
  该技能引导用户完成从账号注册到开户提交的全流程。
  适用于希望开通交易账户但不知道如何操作的客户，由客服或AI助手被动触发协助。
triggers:
  - "我要开户"
  - "帮助我开户"
  - "开通交易账户"
  - "如何注册交易账号"
  - "新用户开户流程"
  - "交易账户注册"
compatibility:
  - bash
environment:
  test: "https://test-lighthorse-trade.touzime.cn"
  production: "https://interface.lighthorse.io"
bundled_resources:
  - path: scripts/api.py
    description: |
      API封装模块。技能执行时直接调用以下方法，无需手动构造请求：
      - api.send_verification_code(contact, contact_type, area_code) - 发送验证码
      - api.login(contact, verification_code, contact_type, area_code) - 登录
      - api.get_trading_token() - 获取交易access_token
      - api.get_user_info() - 获取用户信息
      - api.update_email(email, auth_code) - 更新邮箱
      - api.update_mobile(phone, area_code, auth_code) - 更新手机号
      - api.query_progress() - 查询开户进度
      - api.collect_information(data) - 提交收集的开户信息
      - api.submit_application() - 提交开户申请
      - api.upload_file(file_path, is_need_min) - 开户文件上传
  - path: references/fields.md
    description: |
      开户信息字段参考。包含所有批次的字段说明、JSON示例，以及枚举类型的可选值列表。技能在2.2节和2.3节中引用的详细示例在此文件中。
---

# 交易开户技能

本技能帮助用户完成交易系统的开户流程。技能采用**渐进式引导**方式，每次只询问用户当前步骤所需的信息。

> **注意**：所有API调用已封装在 `scripts/api.py` 的 `TradeAPI` 类中，通过 `scripts/api.py` 中定义的固定请求头和认证机制工作。技能执行时直接调用封装好的方法即可。

## 开户流程总览

```
1. 注册账号（认证中心）
   └─ 发送短信/邮箱验证码 → 登录 → 补全手机号/邮箱（如需要） → 获取交易访问token
2. 填写开户信息（交易开户）
   └─ 查询进度 → 收集个人信息/资金来源（增量多次） → 提交开户申请
```

---

## 第一步：账号注册

### 1.1 询问用户登录方式

请询问用户想使用哪种方式注册/登录：

- **手机号登录**：需要用户输入区号+手机号（如：+1 2503202313），区号和手机号使用空格分隔，区号必填，手机号必填
- **邮箱登录**：需要用户输入邮箱，邮箱格式做验证，邮箱必填

### 1.2 发送验证码

根据用户选择，调用 `api.send_verification_code()`:

```python
# 手机号
api.send_verification_code(contact="用户手机号", contact_type="MOBILE", area_code="用户输入的区号")
# 邮箱
api.send_verification_code(contact="用户邮箱", contact_type="EMAIL")
```

### 1.3 用户输入验证码

请将收到的验证码（通常6位数字）告知用户，并询问用户输入。

### 1.4 完成登录

调用 `api.login()`:

```python
# 手机号登录
api.login(contact="用户手机号", verification_code="用户输入的验证码", contact_type="MOBILE", area_code="用户输入的区号")
# 邮箱登录
api.login(contact="用户邮箱", verification_code="用户输入的验证码", contact_type="EMAIL")
```

登录成功后，`TradeAPI` 实例会自动记录 `userId` 和 `token`。

### 1.5 补全用户信息

登录后需确认用户手机号和邮箱是否已绑定。调用 `api.get_user_info()` 获取用户信息：

```python
api.get_user_info()
```

**返回数据中的关键字段：**
| 字段 | 说明 |
|------|------|
| phone | 手机号（为空则需要绑定） |
| emailVerify  | 邮箱验证状态（已验证为 1，未验证为 0） |

**判断逻辑：**
- 只判断 phone 和 emailVerify 两个字段，其他字段不做判断
- 如果 `phone` 为空，需要用户输入手机号，区号，短信验证码，调用 `api.update_mobile()` 绑定手机号
- 如果 `emailVerify` 为 0，需要用户输入邮箱，邮件验证码，调用 `api.update_email()` 绑定邮箱
- 上面两个条件都不满足，说明手机号和邮箱都已绑定，无需补全信息，直接继续下一步获取交易token

**绑定手机号示例：**
```python
# 1. 先发送验证码到新手机号
api.send_verification_code(contact="新手机号", contact_type="MOBILE", area_code="用户输入的区号")
# 2. 用户输入验证码后更新
api.update_mobile(phone="新手机号", area_code="用户输入的区号", auth_code="用户输入的验证码")
```

**绑定邮箱示例：**
```python
# 1. 先发送验证码到新邮箱
api.send_verification_code(contact="新邮箱", contact_type="EMAIL")
# 2. 用户输入验证码后更新
api.update_email(email="新邮箱", auth_code="用户输入的验证码")
```

### 1.6 获取交易访问token

调用 `api.get_trading_token()`:

```python
api.get_trading_token()
```

获取成功后，`TradeAPI` 实例会自动记录 `access_token`。

---

## 第二步：填写开户信息

### 2.1 查询当前开户进度

调用 `api.query_progress()`:

```python
api.query_progress()
```

**返回数据解读：**

| status | 含义 | 下一步 |
|--------|------|--------|
| NOT_APPLIED | 未申请开户 | 引导用户开始填写信息 |
| COLLECTING | 开户信息收集中 | 继续填写信息 |
| AUDITING | 开户审核中 | 告知用户等待审核 |
| SUPPLEMENTARY_REQUIRED | 需要补充信息 | 告知用户需要补充信息或材料，通过 audit_comment 提示用户补全 |
| REJECTED | 开户被拒绝 | 告知用户开户被拒绝 |
| OPENED | 开户成功 | 开户已完成 |

**增量续填字段（支持第二次进入继续填写）：**

当 status 为 COLLECTING 时，返回数据中还会包含以下两个字段，用于支持用户第二次进入开户时无需从头填写：

| 字段 | 说明 |
|------|------|
| collected_fields | 用户已填写的字段集合（如 `["given_name", "family_name", "date_of_birth"]`） |
| missing_fields | 用户还未填写的字段集合（如 `["gender", "visa_type", "passport_photo"]`） |

**判断逻辑：**
- 如果 `missing_fields` 为空，说明所有必填信息已填写完毕，可直接调用 `api.submit_application()` 提交开户申请
- 如果 `missing_fields` 非空，说明还有未填写的字段，**从 `missing_fields` 中按批次选取1-3个字段继续引导用户填写**，已填写的字段（`collected_fields` 中的字段）**不要重复询问**
- 用户第二次进入开户时，直接从 `missing_fields` 中的字段开始继续，无需从头填写

### 2.2 分批收集信息

当状态为 COLLECTING 时，渐进式引导用户填写信息，**每次只询问1-3个字段**，用户填写完毕后立即调用 `api.collect_information()` 提交。

> **重要**：
> - 枚举类型字段必须从预设选项中选择，禁止自由输入
> - 提示用户时只展示字段说明，不展示字段名
> - 详细字段说明和可选值请参考 `references/fields.md`
> - **仅仅第一次**调用 `api.collect_information()` 时，需在提交的数据对象中额外增加一个 `application_source` 字段来标识开户申请来源。后续调用则不需要添加此字段。

示例（仅第一次提交）：
```python
api.collect_information(data={
    "application_source": {
        "source": "AI_OPEN_ACCOUNT_SKILL",
        "packageName": "open-account-skill",
        "appVersion": "1.0.0"
    },
    # 其他要提交的字段
    "given_name": "li"
})
```

**文件上传流程**：部分字段为文件对象，需先上传文件再提交：
1. 用户准备好文件（PDF、JPG、PNG 等格式）
2. 调用 `api.upload_file(file_path="/path/to/file", is_need_min=True)` 上传
3. 获取返回的 `fileId` 和 `minFileId`
4. 填入对应文件对象字段，再调用 `api.collect_information()` 提交



---

#### 账户类型（2个字段，可一次填写）

| 字段说明 | 可选值 |
|---------|--------|
| 账户类型 | CASH, MARGIN |
| 是否开通加密货币 | true, false |

#### 个人信息（每次1-3个字段）

| 步骤 | 字段说明 | 可选值/格式 |
|------|---------|-------------|
| 1 | 姓名（名、中间名、姓） | 自由输入 |
| 2 | 出生日期、性别 | 格式：YYYY-MM-DD；可选值：MALE, FEMALE, OTHER |
| 3 | 婚姻状态、供养人数 | 可选值：MARRIED, SINGLE, DIVORCED, WIDOWED；供养人数为正整数 |
| 4 | 国籍、出生国家、是否永久居民 | 自由输入国家代码；可选值：true, false |
| 5 | 住址 | 需包含国家、州/省、城市、邮编、街道信息；格式参见fields.md |
| 6 | 社会安全号 | 格式：XXX-XX-XXXX |
| 7 | 签证类型、签证过期日期 | 自由输入签证类型（如E1, E2, F1, B1）；格式：YYYY-MM-DD |
| 8 | 护照照片 | 需先上传文件获取ID |
| 9 | 身份证照片 | 需先上传文件获取ID |

#### 雇佣和财务信息（每次1-3个字段）

| 步骤 | 字段说明 | 可选值/格式 |
|------|---------|-------------|
| 1 | 就业状态 | 可选值全部参见fields.md |
| 2 | 雇主名称、职位、工作年限、行业 | 工作年限为正整数；行业可选值见fields.md |
| 3 | 资金来源、其他来源 | 资金来源可选值见fields.md；其他来源为自由输入 |
| 4 | 年收入范围（USD） | 可选值全部参见fields.md |
| 5 | 流动资产范围（USD） | 同上 |
| 6 | 总资产范围（USD） | 同上 |

#### 投资经验信息（每次1-3个字段）

| 步骤 | 字段说明 | 可选值 |
|------|---------|--------|
| 1 | 投资经验、投资目标 | 可选值全部参见fields.md |
| 2 | 投资期限、风险承受能力、流动性需求 | 可选值全部参见fields.md |
| 3 | 股票投资经验、保证金交易经验 | 可选值全部参见fields.md |

#### 披露和协议（每次1-3个字段）

| 步骤 | 字段说明 | 可选值/格式 |
|------|---------|-------------|
| 1 | 是否为控制人、关联公司股票代码 | true, false；股票代码自由输入 |
| 2 | 是否关联交易所或FINRA、机构名称 | true, false；机构名称自由输入 |
| 3 | 关联审批文件 | 需先上传文件获取ID |
| 4 | 是否政治曝光人物、直系亲属、政治组织 | true, false；其他为自由输入 |
| 5 | 是否交易商品、商品描述 | true, false；描述自由输入 |
| 6 | 是否需要交易授权、代理姓名 | true, false；姓名自由输入 |
| 7 | 是否需要身份验证、联系人信息 | true, false；联系人需包含姓名、电话、邮箱、出生日期、地址、关系 |
| 8 | 协议接受状态 | true, false |
| 9 | 驾照 | 需分别上传正面和背面文件 |
| 10 | 政府颁发的身份证件 | 需分别上传正面和背面文件 |

**字段输入限制，格式，可选值请严格参考：** `references/fields.md`

### 2.3 提交收集的信息

每次用户填写完当前步骤的字段后，立即调用 `api.collect_information()`:

```python
api.collect_information(data=收集的信息字典)
```
**传入接口的json格式请严格参考：** `references/fields.md`

如果返回 `{"s": "ok"}` 表示提交成功，继续引导用户填写下一步。否则，根据返回对象中的`s`和`errmsg`，告知用户填写错误重新填写或者重试。

---

## 第三步：提交开户申请

当所有必填信息填写完毕后，调用 `api.submit_application()`:

```python
api.submit_application()
```

**返回数据解读：**

| s | 含义 | 下一步 |
|---|------|--------|
| ok | 提交成功，等待审核 | 告知用户审核中 |
| 其他 | 提交失败 | 查看 missing_fields，告知用户还缺哪些信息 |

**提交成功后告知用户：**
> 开户申请已提交，交易系统会在1-3个工作日内审核。审核通过后，您即可使用交易系统进行交易。审核结果会通过短信/邮箱通知您。

---

## 错误处理

### 认证中心错误码

| code | msg | 处理方式 |
|------|-----|----------|
| -100005 | Google recaptcha V3 验证失败 | 重试或联系客服 |
| -100006 | Google recaptcha V2 验证失败 | 重试或联系客服 |
| -100007 | captcha-sider 风险拦截 | 操作频繁，请稍后再试 |
| -100008 | 发送验证码被禁止 | 今日验证码发送次数已用尽 |

### 交易开户错误

- **status 不是 ok**：查看 `i18nMsg` 或接口文档中的错误说明
- **missing_fields 返回非空数组**：告知用户还缺少哪些必填字段

---

## 环境切换

如无特殊说明，默认使用**测试环境**。

- 测试环境：`https://test-lighthorse-trade.touzime.cn`
- 生产环境：`https://interface.lighthorse.io`

如需切换，请在技能开始时询问用户，或根据上下文判断。

## API调用示例

```python
from scripts.api import TradeAPI

# 初始化API（默认测试环境）
api = TradeAPI(environment="test")

# 发送验证码
api.send_verification_code(contact="13800138000", contact_type="MOBILE", area_code="+86")

# 用户输入验证码后登录
api.login(contact="13800138000", verification_code="123456", contact_type="MOBILE", area_code="+86")

# 获取用户信息，检查手机号和邮箱是否已绑定
user_info = api.get_user_info()
if not user_info.get("data", {}).get("phone"):
    # 手机号为空，需要绑定
    api.send_verification_code(contact="新手机号", contact_type="MOBILE", area_code="手机区号")
    api.update_mobile(phone="新手机号", area_code="手机区号", auth_code="验证码")

if user_info.get("data", {}).get("emailVerify") == 0:
    # 邮箱为空，需要绑定
    api.send_verification_code(contact="新邮箱", contact_type="EMAIL")
    api.update_email(email="新邮箱", auth_code="验证码")

# 获取交易token
api.get_trading_token()

# 查询进度
api.query_progress()

# 提交收集的信息（示例：个人信息）
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

# 提交开户申请
api.submit_application()

# 上传开户文件
api.upload_file(file_path="/path/to/document.pdf", is_need_min=False)
```