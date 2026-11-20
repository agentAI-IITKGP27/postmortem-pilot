from io import BytesIO
from typing import Any
from docx import Document


def render_postmortem_markdown(state: dict[str, Any]) -> str:
    bundle_id = state.get("bundle_id", "unknown-bundle")
    service = state.get("primary_service", "unknown-service")
    root_cause = state.get("root_cause", "No root cause synthesized.")
    factors = state.get("contributing_factors") or ["Systemic process or configuration gap"]
    timeline = state.get("timeline") or []
    evidence_ids = state.get("evidence_ids") or []
    runbook_id = state.get("runbook_id", "RUNBOOK:001")
    expected_action = state.get("expected_action", "Follow documented verification checklist.")
    ticket = state.get("ticket_draft") or {}

    lines = [
        f"# Incident Postmortem Report: {bundle_id}",
        "",
        "## 1. Executive Summary & Impact",
        f"- **Primary Service:** {service}",
        "- **Governance Gate:** Critic-verified, blameless review",
        "",
        "## 2. Root Cause Analysis",
        root_cause,
        "",
        "## 3. Contributing Factors",
    ]
    for factor in factors:
        lines.append(f"- {factor}")

    lines.extend(["", "## 4. Incident Timeline"])
    if timeline:
        for ev in timeline:
            ts = ev.get("timestamp_utc", "N/A")
            svc = ev.get("service", service)
            msg = ev.get("event", "")
            eid = ev.get("evidence_id", "LOG:001")
            lines.append(f"- `{ts}` | **{svc}**: {msg} (`{eid}`)")
    else:
        lines.append("- No discrete timeline events recorded.")

    lines.extend([
        "",
        "## 5. Runbook Gaps",
        f"- **Runbook:** `{runbook_id}`",
        f"- **Expected Action:** {expected_action}",
        "",
        "## 6. Cited Evidence IDs",
        ", ".join(f"`{eid}`" for eid in evidence_ids) if evidence_ids else "None cited",
        "",
        "## 7. Proposed Remediation Ticket (HITL Gated)",
        f"- **Title:** {ticket.get('title', 'Remediate service fault')}",
        f"- **Owner Role:** {ticket.get('owner_role', 'SRE-OnCall')}",
        f"- **Priority:** {ticket.get('priority', 'P1')}",
        f"- **Status:** {ticket.get('status', 'DRAFT')}",
    ])
    return "\n".join(lines)


def render_postmortem_docx_bytes(state: dict[str, Any]) -> bytes:
    doc = Document()
    bundle_id = state.get("bundle_id", "unknown-bundle")
    service = state.get("primary_service", "unknown-service")
    root_cause = state.get("root_cause", "No root cause synthesized.")
    factors = state.get("contributing_factors") or ["Systemic process or configuration gap"]
    timeline = state.get("timeline") or []
    evidence_ids = state.get("evidence_ids") or []
    runbook_id = state.get("runbook_id", "RUNBOOK:001")
    expected_action = state.get("expected_action", "Follow documented verification checklist.")
    ticket = state.get("ticket_draft") or {}

    doc.add_heading(f"Incident Postmortem: {bundle_id}", level=1)
    doc.add_paragraph(f"Primary Service: {service} | Status: Blameless & Critic-Verified")

    doc.add_heading("Root Cause Analysis", level=2)
    doc.add_paragraph(root_cause)

    doc.add_heading("Contributing Factors", level=2)
    for factor in factors:
        doc.add_paragraph(factor, style="List Bullet")

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
