from __future__ import annotations

import re
import unicodedata


def normalize_text(text: str | None) -> str:
    """Normalize Unicode and whitespace while retaining paragraph content."""
    if text is None:
        return ""
    value = unicodedata.normalize("NFKC", text)
    value = re.sub(r"[\u00a0\u2007\u202f\u3000]", " ", value)
    lines = []
    for line in value.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"\d{1,4}", stripped):
            continue
        lines.append(stripped)
    value = "\n".join(lines)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()
