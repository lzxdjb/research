## Open Account File Upload and Collect

### Basic Info

**Path:** /api/oas/v1/application/file/collect

**Method:** POST

**Description:**
<p>Upload required files during account opening and automatically submit to the account opening application</p>

### Request Parameters

**Headers**

| Parameter | Value | Required | Example | Notes |
| ------------ | ------------ | ------------ | ------------ | ------------ |
| Content-Type | multipart/form-data | Yes | | |
| Cookie | userid:userid;sessionid:sessionid; | Yes | userid=3000015057;sessionid=2032b65d6d0734bd78d3e11c287b8a1bd; | Requires userid and sessionid issued by the authentication center |

**Body**

| Parameter | Type | Required | Example | Notes |
| ------------ | ------------ | ------------ | ------------ | ------------ |
| file_type | text | Yes | driving_licence/id_card/passport/affiliated_approval | Upload file type |
| front | file | Yes | Upload file stream | Upload front side of document (if no front/back distinction, upload front) |
| back | file | No | Upload file stream | Upload back side of document (optional if no back side) |
| is_need_min | text | No | 0 | Whether to generate thumbnail (0/1) |

### Response Data

| Field | Type | Required | Default | Notes |
| ------ | ------ | -------- | ------- | ----- |
| s | string | Yes | ok | Status code |
| d | boolean | Yes | true | Whether successful |

**Example Response:**
```json
{
  "s": "ok",
  "d": true
}
```