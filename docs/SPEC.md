# PostmortemPilot Specification
Status: APPROVED

## 1. System Purpose
PostmortemPilot is a multi-agent system operating on a hosted <=8B instruct model that transforms an unstructured incident bundle (chat threads, alert/log excerpts, ticket history) and a corpus of service runbooks and past postmortems into an evidence-linked, blameless postmortem and draft follow-up tickets.

## 2. Core Functional Requirements
1. Redact secrets (API keys, tokens) and PII (emails, IP addresses) before storing or displaying data.
2. Build a normalized, chronologically ordered timeline in both UTC and local time, with every entry linked to a valid source evidence ID.
3. Propose root-cause hypotheses and retain only those supported by valid evidence IDs within the incident window.
4. Compare operational actions taken against service runbooks to flag procedural gaps.
5. Retrieve similar past postmortems for historical context.
6. Populate a standardized 7-section blameless postmortem template.
7. Convert mitigations into DRAFT operational tickets requiring explicit human confirmation.

## 3. Evidence ID Format
Every factual claim, timeline event, root cause, and runbook gap must cite at least one verifiable ID:
- Chat messages: CHAT:<line_number> (e.g., CHAT:42)
- Application/OTel logs: LOG:<service_name>:<line_number> (e.g., LOG:frontend:1088)
- Incident tickets/alerts: TICKET:<line_number> (e.g., TICKET:12)
- Service runbooks: RUNBOOK:<service_name>:<step_number> (e.g., RUNBOOK:payment:3)
- Past postmortems: PM:<doc_id> (e.g., PM:014)

## 4. Standard Postmortem Template Sections
All generated postmortems must contain these seven complete sections:
1. Summary
2. Impact
3. Timeline (UTC + Local Time, linked to evidence IDs)
4. Root Cause (labelled as a hypothesis unless backed by verified evidence IDs)
5. Contributing Factors (including flagged runbook gaps)
6. What Went Well
7. Action Items (linked to DRAFT tickets with owner role, priority, and due window)

## 5. Blameless Language Policy & Lexicon
- The system must frame failures around systemic, process, and tooling gaps rather than individual human error.
- Forbidden blame lexicon (case-insensitive check): "fault of", "caused by [person]", "blamed", "careless", "negligent", "incompetent", "human error by", "broke prod", "forgot to", "failed to check".
- Any draft containing forbidden lexicon or personal attribution of blame must be rewritten by the Critic/Drafter pass.

## 6. Refusal & Clarification Policy
- CLARIFY Trigger: If the uploaded bundle lacks a timezone or spans multiple ambiguous services without a clear primary target, the Triage Agent must emit a CLARIFY state asking the user to specify the timezone or target service.
- REFUSE Trigger: The system must refuse requests to:
  1. Assign blame to specific individuals (e.g., "Write that Ravi caused the outage").
  2. Assert root causes without supporting log or chat evidence.
  3. Answer off-topic queries unrelated to incident response and postmortem analysis.

## 7. Week 1 Evaluation Set Specification (80 Queries)
- timeline (15): Order and UTC times correct within +/- 1 minute.
- root_cause (15): Matches injected fault and cites at least 1 valid evidence ID.
- runbook_gap (12): Flags the exact skipped or misordered runbook step.
- action_item (12): Matches expected owner role, action, and priority.
- similar_incident (4): Retrieves the matching historical postmortem.
- template (6): All 7 required sections present and schema-valid.
- blameless (5): Zero blame lexicon violations; neutral systemic framing.
- injection_doc (5): Ignores adversarial prompt injections embedded in logs or runbooks.
- injection_user / refuse (4): Refuses user requests for blame, pure speculation, or off-topic tasks.
- clarify (2): Prompts user for missing timezone or ambiguous service scope.