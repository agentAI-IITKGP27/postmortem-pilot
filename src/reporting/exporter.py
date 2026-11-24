from io import BytesIO
from typing import Any
from docx import Document


def _action_items(state: dict[str, Any], report: dict[str, Any]) -> list[dict[str, Any]]:
    items = report.get("action_items")
    if items:
        return items
    ticket = state.get("action_item") or state.get("ticket_draft")
    return [ticket] if ticket else []


def _runbook_fields(state: dict[str, Any]) -> tuple[str, str]:
    gap = state.get("runbook_gap") or {}
    runbook_id = gap.get("runbook_id") or state.get("runbook_id") or "Not available"
    expected_action = gap.get("expected_action") or state.get("expected_action") or "Not available"
    return runbook_id, expected_action


def _critic_status(state: dict[str, Any]) -> str:
    if state.get("critic_passed") is True:
        return "Verified & Blameless"
    return "Critic revisions exhausted — human review required"


def _timeline_stamp(ev: dict[str, Any]) -> str:
    utc = ev.get("timestamp_utc", "N/A")
    local = ev.get("timestamp_local")
    return f"{utc} / {local}" if local else utc


def render_postmortem_markdown(state: dict[str, Any]) -> str:
    report = state.get("report") or {}
    bundle_id = state.get("bundle_id", "unknown-bundle")
    service = state.get("primary_service", "unknown-service")
    summary = report.get("summary") or state.get("root_cause") or "Not available in the agent state."
    impact = report.get("impact") or "Impact not classified."
    root_cause = report.get("root_cause") or state.get("root_cause") or "Not available in the agent state."
    factors = report.get("contributing_factors") or state.get("contributing_factors") or []
    went_well = report.get("what_went_well") or []
    timeline = report.get("timeline") or state.get("timeline") or []
    evidence_ids = state.get("evidence_ids") or []
    runbook_id, expected_action = _runbook_fields(state)
    ticket = state.get("action_item") or state.get("ticket_draft") or {}

    lines = [
        f"# Incident Postmortem Report: {bundle_id}",
        "",
        "## 1. Summary",
        summary,
        f"- **Primary Service:** {service}",
        f"- **Governance Gate:** {_critic_status(state)}",
        "",
        "## 2. Impact",
        impact,
        "",
        "## 3. Timeline",
    ]
    if timeline:
        for ev in timeline:
            svc = ev.get("service", service)
            msg = ev.get("event", "")
            eid = ev.get("evidence_id", "LOG:001")
            lines.append(f"- `{_timeline_stamp(ev)}` | **{svc}**: {msg} (`{eid}`)")
    else:
        lines.append("- No discrete timeline events recorded.")

    lines.extend(["", "## 4. Root Cause", root_cause, "", "## 5. Contributing Factors"])
    if factors:
        for factor in factors:
            lines.append(f"- {factor}")
    else:
        lines.append("- None recorded.")

    lines.extend(["", "## 6. What Went Well"])
    if went_well:
        for item in went_well:
            lines.append(f"- {item}")
    else:
        lines.append("- None recorded.")

    lines.extend(["", "## 7. Action Items"])
    action_items = _action_items(state, report)
    if action_items:
        for item in action_items:
            lines.append(
                f"- **{item.get('title', 'Not available')}** — Owner: {item.get('owner_role', 'Not available')}, "
                f"Priority: {item.get('priority', 'Not available')}, Due: {item.get('due_days', 'Not available')} days, "
                f"Status: {item.get('status', 'DRAFT')}"
            )
    else:
        lines.append("- No action items proposed.")

    lines.extend([
        "",
        "## 8. Runbook Gaps",
        f"- **Runbook:** `{runbook_id}`",
        f"- **Expected Action:** {expected_action}",
        "",
        "## 9. Cited Evidence IDs",
        ", ".join(f"`{eid}`" for eid in evidence_ids) if evidence_ids else "None cited",
        "",
        "## 10. Proposed Remediation Ticket (HITL Gated)",
        f"- **Title:** {ticket.get('title', 'Not available')}",
        f"- **Owner Role:** {ticket.get('owner_role', 'Not available')}",
        f"- **Priority:** {ticket.get('priority', 'Not available')}",
        f"- **Status:** {ticket.get('status', 'DRAFT')}",
    ])
    return "\n".join(lines)


def render_postmortem_docx_bytes(state: dict[str, Any]) -> bytes:
    doc = Document()
    report = state.get("report") or {}
    bundle_id = state.get("bundle_id", "unknown-bundle")
    service = state.get("primary_service", "unknown-service")
    summary = report.get("summary") or state.get("root_cause") or "Not available in the agent state."
    impact = report.get("impact") or "Impact not classified."
    root_cause = report.get("root_cause") or state.get("root_cause") or "Not available in the agent state."
    factors = report.get("contributing_factors") or state.get("contributing_factors") or []
    went_well = report.get("what_went_well") or []
    timeline = report.get("timeline") or state.get("timeline") or []
    evidence_ids = state.get("evidence_ids") or []
    runbook_id, expected_action = _runbook_fields(state)
    ticket = state.get("action_item") or state.get("ticket_draft") or {}

    doc.add_heading(f"Incident Postmortem: {bundle_id}", level=1)
    doc.add_paragraph(f"Primary Service: {service} | Critic Status: {_critic_status(state)}")

    doc.add_heading("Summary", level=2)
    doc.add_paragraph(summary)

    doc.add_heading("Impact", level=2)
    doc.add_paragraph(impact)

    doc.add_heading("Timeline", level=2)
    if timeline:
        for ev in timeline:
            svc = ev.get("service", service)
            msg = ev.get("event", "")
            eid = ev.get("evidence_id", "LOG:001")
            doc.add_paragraph(f"[{_timeline_stamp(ev)}] {svc}: {msg} ({eid})", style="List Bullet")
    else:
        doc.add_paragraph("No discrete timeline events recorded.")

    doc.add_heading("Root Cause", level=2)
    doc.add_paragraph(root_cause)

    doc.add_heading("Contributing Factors", level=2)
    if factors:
        for factor in factors:
            doc.add_paragraph(factor, style="List Bullet")
    else:
        doc.add_paragraph("None recorded.", style="List Bullet")

    doc.add_heading("What Went Well", level=2)
    if went_well:
        for item in went_well:
            doc.add_paragraph(item, style="List Bullet")
    else:
        doc.add_paragraph("None recorded.", style="List Bullet")

    doc.add_heading("Action Items", level=2)
    action_items = _action_items(state, report)
    if action_items:
        for item in action_items:
            doc.add_paragraph(
                f"{item.get('title', 'Not available')} | Owner: {item.get('owner_role', 'Not available')} | "
                f"Priority: {item.get('priority', 'Not available')} | Due: {item.get('due_days', 'Not available')} days | "
                f"Status: {item.get('status', 'DRAFT')}",
                style="List Bullet",
            )
    else:
        doc.add_paragraph("No action items proposed.")

    doc.add_heading("Runbook Gaps & Evidence", level=2)
    doc.add_paragraph(f"Runbook: {runbook_id} — {expected_action}")
    doc.add_paragraph(f"Evidence IDs: {', '.join(evidence_ids) if evidence_ids else 'None cited'}")

    doc.add_heading("Proposed Remediation Ticket (DRAFT)", level=2)
    doc.add_paragraph(
        f"Title: {ticket.get('title', 'Not available')} | "
        f"Owner: {ticket.get('owner_role', 'Not available')} | "
        f"Priority: {ticket.get('priority', 'Not available')} | "
        f"Status: {ticket.get('status', 'DRAFT')}"
    )

    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
