import re
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import duckdb
from src.safety.redactor import redact_text, filter_prompt_injections

TZ_OFFSETS = {
    "UTC": timezone.utc,
    "Z": timezone.utc,
    "IST": timezone(timedelta(hours=5, minutes=30)),
    "PST": timezone(timedelta(hours=-8)),
    "PDT": timezone(timedelta(hours=-7)),
    "EST": timezone(timedelta(hours=-5)),
}

# Static dependency graph for OpenTelemetry Astronomy Shop services (Tool 7: get_service_deps)
OTEL_SERVICE_DEPS: Dict[str, List[str]] = {
    "frontend": ["checkout", "product-catalog", "cart", "recommendation", "ad", "currency", "shipping"],
    "checkout": ["payment", "cart", "shipping", "currency", "email", "kafka", "product-catalog"],
    "payment": ["payment-db", "flagd"],
    "product-catalog": ["catalog-db", "flagd"],
    "recommendation": ["product-catalog", "redis-cache", "flagd"],
    "ad": ["flagd"],
    "kafka": ["checkout", "accounting", "fraud-detection", "flagd"],
    "cart": ["redis-cart", "flagd"],
    "shipping": ["quote", "flagd"],
    "currency": ["flagd"],
    "email": ["flagd"],
    "accounting": ["kafka", "postgres- accounting"],
    "fraud-detection": ["kafka", "flagd"],
    "image-provider": ["frontend", "flagd"],
    "load-generator": ["frontend", "flagd"],
}

LINE_REGEX = re.compile(
    r"^(?P<ev_id>(?:LOG:[a-zA-Z0-9_-]+:\d+|TICKET:\d+|CHAT:\d+))\s+"
    r"\[(?P<ts>[^\]]+)\]\s+(?P<msg>.*)$"
)

# Tool 1: redact_secrets
def redact_secrets(text: str) -> Dict[str, Any]:
    cleaned, counts = redact_text(text)
    return {"redacted_text": cleaned, "redaction_counts": counts}

# Tool 3: normalise_time
def normalise_time(ts_str: str) -> Optional[str]:
    ts_clean = ts_str.strip()
    parts = ts_clean.split()
    tz_label = "UTC"
    if len(parts) == 3 and parts[2].upper() in TZ_OFFSETS:
        dt_part = f"{parts[0]} {parts[1]}"
        tz_label = parts[2].upper()
    elif ts_clean.endswith("Z"):
        dt_part = ts_clean[:-1].replace("T", " ")
        tz_label = "UTC"
    else:
        return None

    try:
        dt_naive = datetime.strptime(dt_part, "%Y-%m-%d %H:%M:%S")
        dt_local = dt_naive.replace(tzinfo=TZ_OFFSETS[tz_label])
        dt_utc = dt_local.astimezone(timezone.utc)
        return dt_utc.strftime("%Y-%m-%d %H:%M:%SZ")
    except Exception:
        return None

# Backward-compatible alias
parse_to_utc = normalise_time

# Tool 2: parse_incident_bundle
def parse_incident_bundle(raw_text: str) -> Dict[str, Any]:
    raw_lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
    safe_lines, quarantined = filter_prompt_injections(raw_lines)

    events: List[Dict[str, str]] = []
    missing_tz = False

    for line in safe_lines:
        if line.startswith("RUNBOOK_CONTEXT:") or line.startswith("PAST_PM_CONTEXT:"):
            continue
        m = LINE_REGEX.match(line)
        if not m:
            continue
        ev_id = m.group("ev_id")
        ts_local = m.group("ts")
        msg = redact_secrets(m.group("msg"))["redacted_text"]

        ts_utc = normalise_time(ts_local)
        if not ts_utc:
            missing_tz = True
            ts_utc = ts_local

        svc = "frontend"
        if ev_id.startswith("LOG:"):
            svc = ev_id.split(":")[1]
        elif "upstream:" in msg:
            svc_match = re.search(r"upstream:\s*([a-zA-Z0-9_-]+)", msg)
            if svc_match:
                svc = svc_match.group(1)

        events.append({
            "timestamp_utc": ts_utc,
            "timestamp_local": ts_local,
            "service": svc,
            "event": msg,
            "evidence_id": ev_id,
        })

    events.sort(key=lambda x: x["timestamp_utc"])
    primary_svc = next((e["service"] for e in events if e["service"] != "frontend"), "unknown")

    return {
        "events": events,
        "primary_service": primary_svc,
        "missing_timezone": missing_tz,
        "quarantined_injections": quarantined,
    }

parse_bundle_lines = parse_incident_bundle

# Tool 4: search_logs (DuckDB SQL over time window, service, and pattern; max 50 lines)
def search_logs(
    events: List[Dict[str, str]],
    service: Optional[str] = None,
    pattern: Optional[str] = None,
    start_utc: Optional[str] = None,
    end_utc: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, str]]:
    if not events:
        return []
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE logs (timestamp_utc VARCHAR, timestamp_local VARCHAR, service VARCHAR, event VARCHAR, evidence_id VARCHAR)")
    for e in events:
        con.execute("INSERT INTO logs VALUES (?, ?, ?, ?, ?)", [e["timestamp_utc"], e["timestamp_local"], e["service"], e["event"], e["evidence_id"]])

    query = "SELECT timestamp_utc, timestamp_local, service, event, evidence_id FROM logs WHERE 1=1"
    params = []
    if service:
        query += " AND (lower(service) = lower(?) OR lower(event) LIKE lower(?))"
        params.extend([service, f"%{service}%"])
    if pattern:
        query += " AND lower(event) LIKE lower(?)"
        params.append(f"%{pattern}%")
    if start_utc:
        query += " AND timestamp_utc >= ?"
        params.append(start_utc)
    if end_utc:
        query += " AND timestamp_utc <= ?"
        params.append(end_utc)
    query += f" ORDER BY timestamp_utc ASC LIMIT {min(limit, 50)}"

    rows = con.execute(query, params).fetchall()
    return [
        {"timestamp_utc": r[0], "timestamp_local": r[1], "service": r[2], "event": r[3], "evidence_id": r[4]}
        for r in rows
    ]

search_logs_duckdb = search_logs

# Tool 5: search_runbooks
def search_runbooks(retriever, query: str, service: Optional[str] = None, top_k: int = 3) -> List[Dict[str, Any]]:
    return retriever.search(query=query, doc_type="runbook", service_filter=service, top_k=top_k)

# Tool 6: search_past_postmortems
def search_past_postmortems(retriever, query: str, service: Optional[str] = None, top_k: int = 3) -> List[Dict[str, Any]]:
    return retriever.search(query=query, doc_type="postmortem", service_filter=service, top_k=top_k)

# Tool 7: get_service_deps
def get_service_deps(service: str) -> Dict[str, Any]:
    svc = service.lower().strip()
    downstream = OTEL_SERVICE_DEPS.get(svc, [])
    upstream = [k for k, v in OTEL_SERVICE_DEPS.items() if svc in v]
    return {"service": svc, "depends_on": downstream, "called_by": upstream}

# Tool 8: create_ticket_draft
def create_ticket_draft(title: str, owner_role: str, priority: str, description: str) -> Dict[str, Any]:
    return {
        "title": title,
        "owner_role": owner_role,
        "priority": priority,
        "due_days": 7 if priority == "P0" else 14,
        "description": description,
        "status": "DRAFT",
        "requires_human_confirmation": True,
    }

if __name__ == "__main__":
    print("Service deps for checkout:", get_service_deps("checkout"))
