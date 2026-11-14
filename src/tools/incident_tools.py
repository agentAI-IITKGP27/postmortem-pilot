"""Deterministic incident tools: UTC normalization and DuckDB log search.

Week 2 Task #4 companion: the ``search_logs`` window experiments compare how
much of the log corpus each retrieval window has to scan before the evidence
line is found.
"""

import csv
import glob
import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

import duckdb

TZ_OFFSET_MINUTES = {
    "UTC": 0,
    "GMT": 0,
    "IST": 330,
    "PST": -480,
    "PDT": -420,
}

LOG_WINDOW_LIMIT = 50
_LOCAL_TS_RE = re.compile(r"\[([^\]]+)\]")
_FLAG_RE = re.compile(r"flagd:([A-Za-z0-9_]+)")


def normalize_to_utc(timestamp: str, tz_hint: Optional[str] = None) -> str:
    """Convert an IST/PST/UTC timestamp into ``YYYY-MM-DD HH:MM:SSZ`` (UTC)."""
    text = re.sub(r"(\d)T(\d{2}:)", r"\1 \2", timestamp.strip())
    tz_name = (tz_hint or "").upper()

    if text.endswith("Z"):
        return text[:-1] + "Z"

    # Explicit offset, e.g. 2026-11-05 12:00:00+05:30
    offset_match = re.search(r"([+-]\d{2}:?\d{2})$", text)
    if offset_match:
        raw_offset = offset_match.group(1)
        sign = 1 if raw_offset[0] == "+" else -1
        digits = raw_offset[1:].replace(":", "")
        offset = sign * (int(digits[:2]) * 60 + int(digits[2:]))
        naive = datetime.strptime(text[: offset_match.start()].strip(), "%Y-%m-%d %H:%M:%S")
        return (naive - timedelta(minutes=offset)).strftime("%Y-%m-%d %H:%M:%SZ")

    # Trailing timezone name, e.g. 2026-11-05 12:00:00 IST
    parts = text.split()
    if parts and parts[-1].isalpha() and parts[-1].upper() in TZ_OFFSET_MINUTES:
        tz_name = parts[-1].upper()
        text = " ".join(parts[:-1])

    if tz_name not in TZ_OFFSET_MINUTES:
        raise ValueError(f"Unknown timezone {tz_hint!r} in {timestamp!r}")

    naive = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    utc_dt = naive - timedelta(minutes=TZ_OFFSET_MINUTES[tz_name])
    return utc_dt.strftime("%Y-%m-%d %H:%M:%SZ")


def parse_utc(value: str) -> datetime:
    """Parse a normalized UTC string into a naive UTC datetime for DuckDB."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%SZ")


def load_bundle_files(bundles_dir: str = "data/bundles") -> List[Dict[str, Any]]:
    paths = sorted(glob.glob(os.path.join(bundles_dir, "bundle_*.json")))
    bundles = []
    for path in paths:
        with open(path) as handle:
            bundles.append(json.load(handle))
    return bundles


def bundle_log_rows(bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten a bundle's lines into log rows with a normalized UTC timestamp."""
    tz_name = bundle.get("timezone", "UTC")
    rows = []
    for line in bundle.get("lines", []):
        raw = line.get("raw", "")
        local_match = _LOCAL_TS_RE.search(raw)
        if local_match:
            ts_utc = normalize_to_utc(local_match.group(1), tz_name)
        else:
            ts_utc = line["timestamp_utc"]
        rows.append(
            {
                "incident_id": bundle["bundle_id"],
                "line_id": line["id"],
                "line_no": int(str(line["id"]).rsplit(":", 1)[-1])
                if str(line["id"]).rsplit(":", 1)[-1].isdigit()
                else 0,
                "ts_utc": ts_utc,
                "service": line.get("service", bundle["affected_services"][0]),
                "message": raw,
            }
        )
    return rows


def build_incident_db(bundles: Iterable[Dict[str, Any]]) -> duckdb.DuckDBPyConnection:
    """Create an in-memory DuckDB table containing every bundle line."""
    con = duckdb.connect()
    con.execute(
        """
        CREATE TABLE logs (
            incident_id VARCHAR,
            line_id VARCHAR,
            line_no INTEGER,
            ts_utc TIMESTAMP,
            service VARCHAR,
            message VARCHAR
        )
        """
    )
    rows = []
    for bundle in bundles:
        for row in bundle_log_rows(bundle):
            rows.append(
                (
                    row["incident_id"],
                    row["line_id"],
                    row["line_no"],
                    parse_utc(row["ts_utc"]),
                    row["service"],
                    row["message"],
                )
            )
    if rows:
        con.executemany("INSERT INTO logs VALUES (?, ?, ?, ?, ?, ?)", rows)
    return con


def search_logs(
    con: duckdb.DuckDBPyConnection,
    service: Optional[str] = None,
    start_utc: Optional[str] = None,
    end_utc: Optional[str] = None,
    keyword: Optional[str] = None,
    incident_id: Optional[str] = None,
    limit: int = LOG_WINDOW_LIMIT,
) -> List[Dict[str, Any]]:
    """Filter logs by service, UTC window and keyword; capped at 50 rows (C1)."""
    clauses = ["1 = 1"]
    params: List[Any] = []
    if incident_id:
        clauses.append("incident_id = ?")
        params.append(incident_id)
    if service:
        clauses.append("service = ?")
        params.append(service)
    if start_utc:
        clauses.append("ts_utc >= ?")
        params.append(parse_utc(start_utc))
    if end_utc:
        clauses.append("ts_utc <= ?")
        params.append(parse_utc(end_utc))
    if keyword:
        clauses.append("lower(message) LIKE ?")
        params.append(f"%{keyword.lower()}%")

    capped = max(1, min(int(limit), LOG_WINDOW_LIMIT))
    sql = (
        "SELECT incident_id, line_id, line_no, ts_utc, service, message "
        f"FROM logs WHERE {' AND '.join(clauses)} "
        f"ORDER BY ts_utc, line_no LIMIT {capped}"
    )
    cursor = con.execute(sql, params)
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _fault_flag(bundle: Dict[str, Any]) -> Optional[str]:
    for line in bundle.get("lines", []):
        match = _FLAG_RE.search(line.get("raw", ""))
        if match:
            return match.group(1)
    return None


def run_window_experiments(
    bundles_dir: str = "data/bundles",
    out_path: str = "results/duckdb_window_experiment.csv",
    windows: Iterable[Optional[int]] = (1, 2, 5, 10, 12, 15, None),
) -> List[Dict[str, Any]]:
    """Measure how much incident evidence each ``search_logs`` window captures."""
    bundles = load_bundle_files(bundles_dir)
    con = build_incident_db(bundles)
    results: List[Dict[str, Any]] = []

    for window in windows:
        evidence_recall = 0.0
        recovery_found = 0
        candidates = 0
        timings = []

        for bundle in bundles:
            anchor = min(parse_utc(row["ts_utc"]) for row in bundle_log_rows(bundle))
            start = anchor - timedelta(minutes=window) if window else None
            end = anchor + timedelta(minutes=window) if window else None

            started = time.perf_counter()
            rows = search_logs(
                con,
                start_utc=start.strftime("%Y-%m-%d %H:%M:%SZ") if start else None,
                end_utc=end.strftime("%Y-%m-%d %H:%M:%SZ") if end else None,
                incident_id=bundle["bundle_id"],
            )
            timings.append((time.perf_counter() - started) * 1000)

            candidates += len(rows)
            gold_ids = {line["id"] for line in bundle.get("lines", [])}
            found_ids = {row["line_id"] for row in rows}
            evidence_recall += len(gold_ids & found_ids) / len(gold_ids) if gold_ids else 0.0
            if "CHAT:2" in found_ids:
                recovery_found += 1

        total = len(bundles)
        results.append(
            {
                "window_minutes": "full_scan" if window is None else window,
                "num_incidents": total,
                "evidence_recall": round(evidence_recall / total, 4) if total else 0.0,
                "recovery_line_recall": round(recovery_found / total, 4) if total else 0.0,
                "avg_candidates": round(candidates / total, 2) if total else 0.0,
                "avg_latency_ms": round(sum(timings) / len(timings), 3) if timings else 0.0,
            }
        )

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)

    return results


def _demo() -> None:
    print("UTC normalization:")
    for value, tz in [
        ("2026-11-05 17:30:00 IST", "IST"),
        ("2026-11-05 02:00:00 PST", "PST"),
        ("2026-11-05 10:00:00 UTC", "UTC"),
    ]:
        print(f"  {value:<26} -> {normalize_to_utc(value, tz)}")

    bundles = load_bundle_files()
    con = build_incident_db(bundles)
    sample = bundles[0]
    rows = search_logs(
        con,
        service=sample["affected_services"][0],
        keyword=_fault_flag(sample),
    )
    print(f"\nsearch_logs() returned {len(rows)} row(s) for {sample['bundle_id']}:")
    for row in rows[:3]:
        print(f"  {row['ts_utc']} {row['line_id']} {row['message'][:90]}")

    results = run_window_experiments()
    print("\nDuckDB window experiment:")
    for row in results:
        print(
            f"  window={str(row['window_minutes']):<9} "
            f"evidence_recall={row['evidence_recall']:.3f} "
            f"recovery_recall={row['recovery_line_recall']:.3f} "
            f"avg_candidates={row['avg_candidates']}"
        )
    print("\nSaved results/duckdb_window_experiment.csv")


if __name__ == "__main__":
    _demo()
