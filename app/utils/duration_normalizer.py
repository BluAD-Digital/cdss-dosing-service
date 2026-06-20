import re

# Canonical case for common standalone terms
_CANONICAL = {
    "maintenance": "Maintenance",
    "initial": "Initial",
    "initial dose": "Initial dose",
    "starting dose": "Starting dose",
    "starting": "Starting",
    "long-term": "Long-term",
    "long term": "Long-term",
    "continuous": "Continuous",
    "ongoing": "Ongoing",
    "chronic": "Chronic",
    "daily": "Daily (ongoing)",
    "once daily": "Daily (ongoing)",
    "as needed": "As needed",
    "single dose": "Single dose",
    "as long as clinically indicated": "As long as clinically indicated",
    "as clinically indicated": "As clinically indicated",
    "until disease progression or unacceptable toxicity": "Until disease progression or unacceptable toxicity",
    "until tumor progression": "Until tumor progression",
    "until healing is complete": "Until healing is complete",
}

_UNIT_PLURAL = {
    "minute": "minutes", "minutes": "minutes", "min": "minutes",
    "hour": "hours", "hours": "hours",
    "day": "days", "days": "days",
    "week": "weeks", "weeks": "weeks",
    "month": "months", "months": "months",
    "year": "years", "years": "years",
}

_UNIT_SINGULAR = {
    "minute": "minute", "minutes": "minute", "min": "minute",
    "hour": "hour", "hours": "hour",
    "day": "day", "days": "day",
    "week": "week", "weeks": "week",
    "month": "month", "months": "month",
    "year": "year", "years": "year",
}


def _canonical_unit(n: float, raw_unit: str) -> str:
    base = _UNIT_SINGULAR.get(raw_unit.lower(), raw_unit.lower())
    plural = _UNIT_PLURAL.get(raw_unit.lower(), raw_unit.lower())
    return base if n == 1 else plural


def normalize_duration(raw: str | None) -> str | None:
    if raw is None:
        return None

    s = raw.strip()
    if not s:
        return None

    lower = s.lower()

    # Exact canonical lookup (case-insensitive)
    if lower in _CANONICAL:
        return _CANONICAL[lower]

    # Bare numbers with no unit — context is always days in this dataset
    # Apply range-separator normalisation first on a working copy, then match
    s_norm = re.sub(r'\s*(?:–|-)\s*', '–', s)
    s_norm = re.sub(r'(?<=\d)\s+to\s+(?=\d)', '–', s_norm, flags=re.I)

    if re.fullmatch(r'\d+(?:–\d+)?', s_norm.strip()):
        parts = s_norm.strip().split('–')
        nums = [int(p) for p in parts]
        unit = _canonical_unit(nums[-1], 'day')
        if len(nums) == 2:
            return f"{nums[0]}–{nums[1]} {unit}"
        return f"{nums[0]} {unit}"

    # Single value with unit: "1 day", "2 weeks", "10 minutes"
    m = re.fullmatch(
        r'(\d+(?:\.\d+)?)\s*(minutes?|min|hours?|days?|weeks?|months?|years?)',
        s_norm.strip(), re.I
    )
    if m:
        n = float(m.group(1))
        unit = _canonical_unit(n, m.group(2))
        n_str = int(n) if n == int(n) else n
        return f"{n_str} {unit}"

    # Range with unit: "7–14 days", "4–8 weeks", "7-14 days", "7 to 14 days"
    m = re.fullmatch(
        r'(\d+(?:\.\d+)?)\s*–\s*(\d+(?:\.\d+)?)\s*(minutes?|min|hours?|days?|weeks?|months?|years?)',
        s_norm.strip(), re.I
    )
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        unit = _canonical_unit(hi, m.group(3))
        lo_str = int(lo) if lo == int(lo) else lo
        hi_str = int(hi) if hi == int(hi) else hi
        return f"{lo_str}–{hi_str} {unit}"

    # "up to X unit"
    m = re.match(r'^up\s+to\s+(\d+(?:\.\d+)?)\s*(minutes?|min|hours?|days?|weeks?|months?|years?)(.*)$', s, re.I)
    if m:
        n = float(m.group(1))
        unit = _canonical_unit(n, m.group(2))
        suffix = re.sub(r'^[,;]\s*', '', m.group(3).strip())
        n_str = int(n) if n == int(n) else n
        base = f"Up to {n_str} {unit}"
        return f"{base}, {suffix}" if suffix else base

    # "at least X unit"
    m = re.match(r'^at\s+least\s+(\d+(?:\.\d+)?)\s*(minutes?|min|hours?|days?|weeks?|months?|years?)(.*)$', s, re.I)
    if m:
        n = float(m.group(1))
        unit = _canonical_unit(n, m.group(2))
        suffix = re.sub(r'^[,;]\s*', '', m.group(3).strip())
        n_str = int(n) if n == int(n) else n
        base = f"At least {n_str} {unit}"
        return f"{base}, {suffix}" if suffix else base

    # Sentence-case everything else: trim and capitalise first letter only
    return s[0].upper() + s[1:] if s else None
