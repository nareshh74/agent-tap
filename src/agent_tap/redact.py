import re
from typing import Any

_SECRET_PATTERNS = [
    (re.compile(r"(?i)(authorization\s*[:=]\s*['\"]?bearer\s+)[A-Za-z0-9._\-+/=]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]{16,}"), r"\1[REDACTED]"),
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), r"\1_[REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "github_pat_[REDACTED]"),
    (re.compile(r"\bsk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_\-]{16,}\b"), "sk-[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"), "AIza[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"), "eyJ[REDACTED_JWT]"),
    (
        re.compile(
            r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret|cookie|set-cookie|x-api-key)\b\s*['\"]?\s*[:=]\s*['\"]?)([^\s'\",;}{]{8,})"
        ),
        r"\1[REDACTED]",
    ),
]


def redact_text(value: str) -> str:
    if not value or not isinstance(value, str):
        return value
    out = value
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if any(s in kl for s in ("api_key", "apikey", "access_token", "refresh_token", "client_secret", "password", "cookie", "authorization")) and isinstance(v, str) and len(v) >= 6:
                out[k] = "[REDACTED]"
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(x) for x in obj)
    return obj
