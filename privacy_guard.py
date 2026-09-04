import re


SENSITIVE_LINK_PATTERN = re.compile(r"https?://[^\s]+", re.I)


def contains_sensitive_voucher_data(value: object) -> bool:
    """Detect order/card credentials before they reach logs or the model."""
    text = str(value or "")
    if not text:
        return False
    return bool(
        re.search(r"(?:卡号|券码|密码)\s*[：:]", text, re.I)
        or re.search(r"https?://[^\s]*(?:kami|xgj|coupon|voucher|券)", text, re.I)
        or re.search(r"(?<!\d)\d{16,24}(?!\d)", text)
    )


def redact_sensitive_text(value: object) -> str:
    """Return a deterministic, readable representation safe for ordinary logs."""
    text = str(value or "")
    if not text:
        return ""
    text = re.sub(r"(?:卡号|券码)\s*[：:]\s*[^\r\n]+", "券码：[已隐藏]", text, flags=re.I)
    text = re.sub(r"密码\s*[：:]\s*[^\r\n\s]+", "密码：[已隐藏]", text, flags=re.I)
    text = SENSITIVE_LINK_PATTERN.sub("[链接已隐藏]", text)
    text = re.sub(r"(?<!\d)1\d{10}(?!\d)", "[手机号已隐藏]", text)
    text = re.sub(r"(?<!\d)\d{16,24}(?!\d)", "[订单号已隐藏]", text)
    return text
