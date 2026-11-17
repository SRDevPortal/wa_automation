"""Pure scheduling, filter and rendering rules (no database or network access)."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import hashlib
import json
import re

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
SUCCESS = {"Accepted", "Sent", "Delivered", "Read"}
OPERATORS = {"=", "!=", ">", "<", ">=", "<=", "like", "not like", "in", "not in", "is", "between"}


def utcnow():
    return datetime.now(timezone.utc)


def as_utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def schedule_next(config, after=None):
    """UTC instant; skip DST gaps, choose the first occurrence during a DST fold."""
    after = as_utc(after or utcnow())
    zone = ZoneInfo(config.get("timezone") or "Asia/Kolkata")
    mode = config.get("delivery_mode")
    if mode == "Now":
        return None
    start = date.fromisoformat(str(config["start_date"])[:10])
    end = date.fromisoformat(str(config["end_date"])[:10]) if config.get("end_date") else None
    send_time = time.fromisoformat(str(config.get("send_time") or "10:00:00"))
    selected = {i for i, day in enumerate(WEEKDAYS) if config.get(day.lower())}
    if mode == "Weekly" and not selected:
        raise ValueError("Select at least one weekday.")
    first = max(start, after.astimezone(zone).date()) if mode == "Weekly" else start
    for offset in range(15 if mode == "Weekly" else 1):
        day = first + timedelta(days=offset)
        if end and day > end:
            return None
        if mode == "Weekly" and day.weekday() not in selected:
            continue
        local = datetime.combine(day, send_time).replace(tzinfo=zone, fold=0)
        instant = local.astimezone(timezone.utc)
        if instant.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            continue
        if instant > after:
            return instant
    return None


def compile_filters(rows, fields, anchor, campaign_timezone, site_timezone):
    result = []
    local = as_utc(anchor).astimezone(ZoneInfo(campaign_timezone))
    for row in rows:
        field = row.get("field_name")
        if field not in fields:
            raise ValueError(f"Field is unavailable or not permitted: {field}")
        op = row.get("operator") or "="
        value = row.get("value")
        fieldtype = fields[field].get("fieldtype")
        if op in {"last days", "next days"}:
            if fieldtype not in {"Date", "Datetime"}:
                raise ValueError("Relative date filters require a Date or Datetime field.")
            days = int(value)
            if not 1 <= days <= 36500:
                raise ValueError("Relative days must be between 1 and 36500.")
            left, right = (local - timedelta(days=days), local) if op == "last days" else (local, local + timedelta(days=days))
            if fieldtype == "Date":
                value = [left.date().isoformat(), right.date().isoformat()]
            else:
                value = [v.astimezone(ZoneInfo(site_timezone)).replace(tzinfo=None).isoformat(" ") for v in (left, right)]
            op = "between"
        elif op in {"contains", "starts with"}:
            value = str(value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            value = f"%{value}%" if op == "contains" else f"{value}%"
            op = "like"
        elif op in {"in", "not in", "between"}:
            value = [v.strip() for v in str(value or "").split(",")]
            if not all(value) or (op == "between" and len(value) != 2):
                raise ValueError("Use comma-separated values; between requires exactly two values.")
        elif op == "is":
            if value not in {"set", "not set"}:
                raise ValueError("The is operator requires set or not set.")
        if op not in OPERATORS:
            raise ValueError(f"Unsupported filter operator: {op}")
        if fieldtype in {"Check", "Int", "Float", "Currency", "Percent"} and op in {"=", "!=", ">", "<", ">=", "<="}:
            value = float(value)
        result.append([field, op, value])
    return result


def normalize_number(value, country_code="+91"):
    import phonenumbers
    raw = str(value or "").strip()
    if not raw or re.search(r"[A-Za-z]", raw):
        raise ValueError("Missing or invalid phone number")
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    code = int(str(country_code).lstrip("+"))
    region = phonenumbers.region_code_for_country_code(code)
    digits = re.sub(r"\D", "", raw)
    # Stored WhatsApp numbers often contain the country prefix without a plus.
    try:
        if not raw.startswith("+") and digits.startswith(str(code)):
            candidate = phonenumbers.parse("+" + digits, None)
            if phonenumbers.is_valid_number(candidate):
                raw = "+" + digits
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException as exc:
        raise ValueError("Invalid phone number") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("Invalid phone number")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164), "+" + str(parsed.country_code), str(parsed.national_number)


def render_values(mappings, record, permitted):
    groups = {"body": {}, "header": {}, "button": {}}
    for row in mappings:
        component = (row.get("component") or "Body").lower()
        position = int(row.get("position") or 0)
        if component not in groups or position < 1 or position in groups[component]:
            raise ValueError("Each template component needs unique positions starting at 1.")
        if row.get("source_type") == "Field":
            field = row.get("field_name")
            if field not in permitted:
                raise ValueError(f"Variable field is unavailable or not permitted: {field}")
            value = record.get(field)
        else:
            value = row.get("fixed_value")
        if value is None or str(value).strip() == "":
            value = row.get("fallback_value")
        if value is None or str(value).strip() == "":
            raise ValueError(f"Missing required {component} variable {position}")
        groups[component][position] = str(value)
    result = {}
    for component in ("body", "header"):
        slots = groups[component]
        if sorted(slots) != list(range(1, len(slots) + 1)):
            raise ValueError(f"{component.title()} positions must be consecutive from 1.")
        result[f"{component}_values"] = [slots[i] for i in sorted(slots)]
    result["button_values"] = {str(i - 1): [v] for i, v in groups["button"].items()}
    return result


def preview_text(text, values):
    return re.sub(r"\{\{\s*(\d+)\s*\}\}", lambda m: values[int(m[1]) - 1] if 0 < int(m[1]) <= len(values) else m[0], text or "")


def stable_key(*parts):
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()


def merge_status(current, incoming):
    ranks = {"Pending": 0, "Sending": 1, "Unknown": 1, "Accepted": 2, "Sent": 3, "Failed": 4, "Delivered": 5, "Read": 6}
    if incoming not in {"Sent", "Delivered", "Read", "Failed"}:
        return current
    return incoming if ranks.get(incoming, 0) > ranks.get(current, 0) else current
