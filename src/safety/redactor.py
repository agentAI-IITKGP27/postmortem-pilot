import re
from typing import Dict, List, Tuple

PATTERNS = [
    ("API_KEY", re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b")),
    ("BEARER_TOKEN", re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{12,}", re.IGNORECASE)),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("IPV4", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b")),
]

INJECTION_PATTERNS = re.compile(
    r"(SYSTEM OVERRIDE|Ignore all prior|ignore previous instructions|DNS_RESOLVED_NO_ACTION_NEEDED)",
    re.IGNORECASE,
)

def redact_text(text: str) -> Tuple[str, Dict[str, int]]:
    counts: Dict[str, int] = {}
    cleaned = text
    for label, pattern in PATTERNS:
        matches = pattern.findall(cleaned)
        if matches:
            counts[label] = counts.get(label, 0) + len(matches)
            if label == "BEARER_TOKEN":
                cleaned = pattern.sub(r"\1[REDACTED_TOKEN]", cleaned)
            else:
                cleaned = pattern.sub(f"[REDACTED_{label}]", cleaned)
    return cleaned, counts

def filter_prompt_injections(lines: List[str]) -> Tuple[List[str], List[str]]:
    safe_lines = []
    quarantined = []
    for line in lines:
        if INJECTION_PATTERNS.search(line):
            quarantined.append(line)
        else:
            safe_lines.append(line)
    return safe_lines, quarantined

if __name__ == "__main__":
    sample = "User admin@corp.com connected from 10.24.1.99 using sk-moLgioCnQ0R9cfqtJVF0zaTv"
    cleaned, stats = redact_text(sample)
    print("Redacted:", cleaned, "| Counts:", stats)
