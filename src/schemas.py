from typing import List, Literal, Optional
from pydantic import BaseModel, Field

class TriageOutput(BaseModel):
    services: List[str] = Field(description="List of affected microservice names identified in the bundle")
    severity: Literal["SEV-1", "SEV-2", "SEV-3", "UNKNOWN"] = Field(description="Incident severity level")
    window_start_utc: Optional[str] = Field(default=None, description="ISO-8601 UTC start timestamp if known")
    window_end_utc: Optional[str] = Field(default=None, description="ISO-8601 UTC end timestamp if known")
    timezone_detected: Optional[str] = Field(default=None, description="Original timezone found in chat/logs, e.g., IST, UTC, PST")
    action: Literal["PROCEED", "CLARIFY", "REFUSE"] = Field(description="Next workflow routing action")
    message: Optional[str] = Field(default=None, description="Clarification question or refusal reason if action is not PROCEED")

class TimelineEvent(BaseModel):
    timestamp_utc: str = Field(description="Normalized UTC timestamp in YYYY-MM-DD HH:MM:SSZ format")
    timestamp_local: str = Field(description="Local timestamp with timezone label")
    service: str = Field(description="Microservice or component involved")
    event: str = Field(description="Objective description of what occurred")
    evidence_id: str = Field(description="Source line reference, e.g., CHAT:12, LOG:payment:402, TICKET:3")

class Hypothesis(BaseModel):
    cause: str = Field(description="Proposed technical root cause")
    evidence_ids: List[str] = Field(description="List of supporting evidence IDs from logs, chat, or metrics")
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = Field(description="Confidence derived from evidence count and directness")
    is_supported: bool = Field(default=True, description="True only if backed by verified evidence IDs")

class RunbookGap(BaseModel):
    runbook_id: str = Field(description="Runbook identifier, e.g., RUNBOOK:payment:failover")
    step_number: int = Field(description="Step number in the runbook that was missed or delayed")
    expected_action: str = Field(description="Action required by the runbook")
    observed_gap: str = Field(description="What actually occurred or was skipped during the incident")
    evidence_id: str = Field(description="Evidence ID showing the gap or omission")

class ActionItemTicket(BaseModel):
    title: str = Field(description="Concise operational mitigation title")
    owner_role: str = Field(description="Responsible role, e.g., SRE-OnCall, Payment-Service-Owner, DBA")
    priority: Literal["P0", "P1", "P2"] = Field(description="Ticket priority level")
    due_days: int = Field(description="Target completion window in days")
    description: str = Field(description="Granular remediation details linked to root cause or runbook gap")
    status: Literal["DRAFT"] = Field(default="DRAFT", description="Must remain DRAFT until human confirmation")
    requires_human_confirmation: bool = Field(default=True, description="Mandatory human-in-the-loop gate")

class PostmortemReport(BaseModel):
    summary: str = Field(description="High-level executive summary of the outage and resolution")
    impact: str = Field(description="User, traffic, and revenue/SLA impact with evidence IDs")
    timeline: List[TimelineEvent] = Field(description="Chronological UTC+local timeline with evidence IDs")
    root_cause: str = Field(description="Verified root cause statement citing evidence IDs")
    contributing_factors: List[str] = Field(description="Systemic factors and flagged runbook gaps with evidence IDs")
    what_went_well: List[str] = Field(description="Effective detection, alerting, or mitigation steps with evidence IDs")
    action_items: List[ActionItemTicket] = Field(description="Follow-up remediation tickets in DRAFT status")

class CriticVerdict(BaseModel):
    passed: bool = Field(description="True only if all claims have valid evidence, no blame words exist, and all sections are complete")
    invalid_evidence_ids: List[str] = Field(default_factory=list, description="Cited evidence IDs that do not exist in the bundle")
    unsupported_claims: List[str] = Field(default_factory=list, description="Assertions lacking evidence ID backing")
    blame_words_detected: List[str] = Field(default_factory=list, description="Blame phrases violating the blameless policy")
    missing_sections: List[str] = Field(default_factory=list, description="Required template sections that are empty or missing")
    revision_instructions: Optional[str] = Field(default=None, description="Specific instructions for the Drafter to fix failures")

if __name__ == "__main__":
    from src.llm import get_llm_client
    _, structured_client, model_name = get_llm_client()

    sample_input = (
        "CHAT:1 [2026-11-05 14:02:00 UTC] @oncall: Checkout errors spiking to 45%!\n"
        "LOG:payment:104 [2026-11-05 14:01:55 UTC] ERROR Connection refused to payment-db:5432"
    )
    res = structured_client.chat.completions.create(
        model=model_name,
        response_model=TriageOutput,
        messages=[
            {"role": "system", "content": "Extract incident triage metadata from the bundle snippet."},
            {"role": "user", "content": sample_input}
        ],
        max_tokens=256,
        temperature=0.1,
    )
    print("Validated Pydantic TriageOutput:")
    print(res.model_dump_json(indent=2))