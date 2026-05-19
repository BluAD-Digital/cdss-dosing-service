import re

_EXACT: dict[str, str] = {
    # once daily
    "qd": "once daily", "od": "once daily", "q24h": "once daily", "q24h": "once daily",
    "daily": "once daily", "once daily": "once daily", "once a day": "once daily",
    # twice daily
    "bid": "twice daily", "bd": "twice daily", "twice daily": "twice daily",
    "twice a day": "twice daily", "two times daily": "twice daily",
    # three times daily
    "tid": "three times daily", "td": "three times daily", "three times daily": "three times daily",
    "three times a day": "three times daily",
    # four times daily
    "qid": "four times daily", "four times daily": "four times daily",
    "4 times daily": "four times daily", "4-6 times daily": "four to six times daily",
    # every N hours
    "q4h": "every 4 hours", "q4-6h": "every 4-6 hours", "q4-6h as needed": "every 4-6 hours as needed",
    "q6h": "every 6 hours", "q6-8h": "every 6-8 hours",
    "q8h": "every 8 hours", "q8-12h": "every 8-12 hours", "q8h or q12h": "every 8 or 12 hours",
    "q12h": "every 12 hours", "q12-24h": "every 12-24 hours",
    "q12h or q8h": "every 8 or 12 hours", "q12h or q6-8h": "every 6-12 hours",
    "q8h to q12h": "every 8-12 hours",
    "q1h": "every hour", "q2h": "every 2 hours", "q3h": "every 3 hours",
    "q3-4h": "every 3-4 hours", "q3-6h": "every 3-6 hours",
    "q2-3h": "every 2-3 hours", "q2-4h": "every 2-4 hours",
    "q4-5h": "every 4-5 hours", "q8-10h": "every 8-10 hours",
    "q16h": "every 16 hours", "q18h": "every 18 hours",
    "q18-24h": "every 18-24 hours", "q24-36h": "every 24-36 hours",
    "q24-48h": "every 24-48 hours", "q24h to q36h": "every 24-36 hours",
    "q24h to q48h": "every 24-48 hours",
    "q36h": "every 36 hours", "q36-48h": "every 36-48 hours",
    "q48h": "every 48 hours", "q48h or q60h": "every 48-60 hours",
    "q48h to q96h": "every 48-96 hours", "q60h": "every 60 hours",
    "q72h": "every 72 hours", "q96h": "every 96 hours",
    # every N days / weeks / months
    "q7d": "once weekly", "q7days": "once weekly", "weekly": "once weekly",
    "once weekly": "once weekly", "every 7 days": "once weekly", "q1w": "once weekly",
    "q14d": "every 2 weeks", "q10d": "every 10 days",
    "q10-14d": "every 10-14 days", "q21d": "every 3 weeks",
    "q2w": "every 2 weeks", "every 2 weeks": "every 2 weeks",
    "q3w": "every 3 weeks", "every 3 weeks": "every 3 weeks",
    "q4w": "every 4 weeks", "every 4 weeks": "every 4 weeks", "monthly": "once monthly",
    "once monthly": "once monthly",
    "every 24 hours": "once daily",
    "q3m": "every 3 months", "q6m": "every 6 months", "q6mo": "every 6 months",
    "biweekly": "twice weekly", "twice weekly": "twice weekly",
    "tiw": "three times weekly", "three times weekly": "three times weekly",
    "three times per week": "three times per week",
    # special
    "qod": "every other day", "every other day": "every other day",
    "once": "single dose", "single dose": "single dose", "single": "single dose",
    "prn": "as needed", "as needed": "as needed", "as_needed": "as needed",
    "on demand": "as needed",
    "continuous": "continuous infusion",
    "loading": "loading dose",
    "hourly": "every hour",
    # minutes
    # variable / compound
    "bid or tid": "two to three times daily", "qd or bid": "once to twice daily",
    "twice weekly or weekly": "once to twice weekly",
    "2-3 times per week": "two to three times weekly",
    "once a year": "once yearly",
    "q1-2min prn": "every 1-2 minutes as needed",
    "q2min prn": "every 2 minutes as needed",
    "q3-5min as_needed": "every 3-5 minutes as needed",
    # minutes
    "q5min": "every 5 minutes", "q10min": "every 10 minutes",
    "q15min": "every 15 minutes", "q30min": "every 30 minutes",
    "q20min": "every 20 minutes", "q60min": "every hour",
    "q90min": "every 90 minutes",
    "q5-10min": "every 5-10 minutes", "q3-5min": "every 3-5 minutes",
    "q10-15min": "every 10-15 minutes", "q15-25min": "every 15-25 minutes",
    "q1-2min": "every 1-2 minutes", "q2min": "every 2 minutes",
    "q2-3min": "every 2-3 minutes", "q5-15min": "every 5-15 minutes",
}

def _days_canonical(n: int) -> str:
    if n == 1:
        return "once daily"
    if n % 7 == 0:
        weeks = n // 7
        return "once weekly" if weeks == 1 else f"every {weeks} weeks"
    return f"every {n} days"


def _weeks_canonical(n: int) -> str:
    return "once weekly" if n == 1 else f"every {n} weeks"


_PATTERN: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^q(\d+)-(\d+)\s*h$", re.I),      lambda m: f"every {m.group(1)}-{m.group(2)} hours"),
    (re.compile(r"^q(\d+)\s*h$", re.I),             lambda m: f"every {m.group(1)} hours"),
    (re.compile(r"^q(\d+)-(\d+)\s*min$", re.I),     lambda m: f"every {m.group(1)}-{m.group(2)} minutes"),
    (re.compile(r"^q(\d+)\s*min$", re.I),            lambda m: f"every {m.group(1)} minutes"),
    (re.compile(r"^q(\d+)\s*d$", re.I),             lambda m: _days_canonical(int(m.group(1)))),
    (re.compile(r"^q(\d+)-(\d+)\s*d$", re.I),       lambda m: f"every {m.group(1)}-{m.group(2)} days"),
    (re.compile(r"^q(\d+)\s*w$", re.I),             lambda m: _weeks_canonical(int(m.group(1)))),
    (re.compile(r"^q(\d+)-(\d+)\s*w$", re.I),       lambda m: f"every {m.group(1)}-{m.group(2)} weeks"),
    (re.compile(r"^(\d+)\s*times\s*(daily|a day|per day)$", re.I), lambda m: f"{m.group(1)} times daily"),
    (re.compile(r"^(\d+)\s*times\s*(weekly|a week|per week)$", re.I), lambda m: f"{m.group(1)} times weekly"),
    (re.compile(r"^every\s+(\d+)\s+hours?$", re.I), lambda m: f"every {m.group(1)} hours"),
    (re.compile(r"^every\s+(\d+)\s+days?$", re.I),  lambda m: _days_canonical(int(m.group(1)))),
    (re.compile(r"^every\s+(\d+)\s+weeks?$", re.I), lambda m: _weeks_canonical(int(m.group(1)))),
    (re.compile(r"^every\s+(\d+)\s+minutes?$", re.I), lambda m: f"every {m.group(1)} minutes"),
]


def resolve_frequency(frequency: str | None) -> str | None:
    if not frequency:
        return None
    key = frequency.strip().lower()
    if key in _EXACT:
        return _EXACT[key]
    for pattern, fmt in _PATTERN:
        m = pattern.match(key)
        if m:
            return fmt(m)
    return None
