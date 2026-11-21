from io import BytesIO
from typing import Any
from docx import Document


def _action_items(state: dict[str, Any], report: dict[str, Any]) -> list[dict[str, Any]]:
    items = report.get("action_items")
    if items:
        return items
    ticket = state.get("action_item") or state.get("ticket_draft")
    return [ticket] if ticket else []


def render_postmortem_markdown(state: dict[str, Any]) -> str:
    report = state.get("report") or {}
    bundle_id = state.get("bundle_id", "unknown-bundle")
    service = state.get("primary_service", "unknown-service")
    summary = report.get("summary") or state.get("root_cause", "No summary synthesized.")
    impact = report.get("impact") or "Impact not classified."
    root_cause = report.get("root_cause") or state.get("root_cause", "No root cause synthesized.")
    factors = report.get("contributing_factors") or state.get("contributing_factors") or [
        "Systemic process or configuration gap"
    ]
    went_well = report.get("what_went_well") or [
        "Human review validated the report before any operational action."
    ]
    timeline = report.get("timeline") or state.get("timeline") or []
    evidence_ids = state.get("evidence_ids") or []
    runbook_id = state.get("runbook_id", "RUNBOOK:001")
    expected_action = state.get("expected_action", "Follow documented verification checklist.")
    ticket = state.get("ticket_draft") or {}

    lines = [
        f"# Incident Postmortem Report: {bundle_id}",
        "",
        "## 1. Summary",
        summary,
        f"- **Primary Service:** {service}",
        "- **Governance Gate:** Critic-verified, blameless review",
        "",
        "## 2. Impact",
        impact,
        "",
        "## 3. Timeline",
    ]
    if timeline:
        for ev in timeline:
            ts = ev.get("timestamp_utc", "N/A")
            svc = ev.get("service", service)
            msg = ev.get("event", "")
            eid = ev.get("evidence_id", "LOG:001")
            lines.append(f"- `{ts}` | **{svc}**: {msg} (`{eid}`)")
    else:
        lines.append("- No discrete timeline events recorded.")

    lines.extend(["", "## 4. Root Cause", root_cause, "", "## 5. Contributing Factors"])
    for factor in factors:
        lines.append(f"- {factor}")

    lines.extend(["", "## 6. What Went Well"])
    for item in went_well:
        lines.append(f"- {item}")

    lines.extend(["", "## 7. Action Items"])
    action_items = _action_items(state, report)
    if action_items:
        for item in action_items:
            lines.append(
                f"- **{item.get('title', 'Remediation')}** — Owner: {item.get('owner_role', 'SRE-OnCall')}, "
                f"Priority: {item.get('priority', 'P1')}, Due: {item.get('due_days', 14)} days, "
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
        f"- **Title:** {ticket.get('title', 'Remediate service fault')}",
        f"- **Owner Role:** {ticket.get('owner_role', 'SRE-OnCall')}",
        f"- **Priority:** {ticket.get('priority', 'P1')}",
        f"- **Status:** {ticket.get('status', 'DRAFT')}",
    ])
    return "\n".join(lines)


def render_postmortem_docx_bytes(state: dict[str, Any]) -> bytes:
    doc = Document()
    report = state.get("report") or {}
    bundle_id = state.get("bundle_id", "unknown-bundle")
    service = state.get("primary_service", "unknown-service")
    summary = report.get("summary") or state.get("root_cause", "No summary synthesized.")
    impact = report.get("impact") or "Impact not classified."
    root_cause = report.get("root_cause") or state.get("root_cause", "No root cause synthesized.")
    factors = report.get("contributing_factors") or state.get("contributing_factors") or [
        "Systemic process or configuration gap"
    ]
    went_well = report.get("what_went_well") or [
        "Human review validated the report before any operational action."
    ]
    timeline = report.get("timeline") or state.get("timeline") or []
    evidence_ids = state.get("evidence_ids") or []
    runbook_id = state.get("runbook_id", "RUNBOOK:001")
    expected_action = state.get("expected_action", "Follow documented verification checklist.")
    ticket = state.get("ticket_draft") or {}

    doc.add_heading(f"Incident Postmortem: {bundle_id}", level=1)
    doc.add_paragraph(f"Primary Service: {service} | Status: Blameless & Critic-Verified")

    doc.add_heading("Summary", level=2)
    doc.add_paragraph(summary)

    doc.add_heading("Impact", level=2)
    doc.add_paragraph(impact)

    doc.add_heading("Timeline", level=2)
    if timeline:
        for ev in timeline:
            ts = ev.get("timestamp_utc", "N/A")
            svc = ev.get("service", service)
            msg = ev.get("event", "")
            eid = ev.get("evidence_id", "LOG:001")
            doc.add_paragraph(f"[{ts}] {svc}: {msg} ({eid})", style="List Bullet")
    else:
        doc.add_paragraph("No discrete timeline events recorded.")

    doc.add_heading("Root Cause", level=2)
    doc.add_paragraph(root_cause)

    doc.add_heading("Contributing Factors", level=2)
    for factor in factors:
        doc.add_paragraph(factor, style="List Bullet")

    doc.add_heading("What Went Well", level=2)
    for item in went_well:
        doc.add_paragraph(item, style="List Bullet")

    doc.add_heading("Action Items", level=2)
    action_items = _action_items(state, report)
    if action_items:
        for item in action_items:
            doc.add_paragraph(
                f"{item.get('title', 'Remediation')} | Owner: {item.get('owner_role', 'SRE-OnCall')} | "
                f"Priority: {item.get('priority', 'P1')} | Due: {item.get('due_days', 14)} days | "
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
        f"Title: {ticket.get('title', 'Remediate service fault')} | "
        f"Owner: {ticket.get('owner_role', 'SRE-OnCall')} | "
        f"Priority: {ticket.get('priority', 'P1')} | "
        f"Status: {ticket.get('status', 'DRAFT')}"
    )

    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
