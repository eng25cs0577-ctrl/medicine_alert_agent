"""CrewAI = alert-drafting crew (runs only for confirmed or likely matches).

  Risk Assessor -> how serious is this failure for patients (severity + why)
  Notice Writer -> recommended actions + a ready-to-send note to the supplier

A rule-based draft is always made first (no AI needed) and is used whenever the AI
is not configured or fails. The pharmacist reviews every draft before acting.
"""

from __future__ import annotations

import json
import re

from config import call_with_fallback, get_logger, settings, with_retries
from models import AlertDraft, InventoryItem, NSQEntry

log = get_logger("CrewAI")

_STERILE_FORMS = ("injection", "inj", "infusion", "eye drop", "ophthalmic", "vial", "ampoule", "iv ")
_CRITICAL_TESTS = ("sterility", "endotoxin", "pyrogen", "particulate", "microbial", "contamina")
_HIGH_TESTS = ("assay", "dissolution", "content", "related substance", "impurit", "disintegration",
               "uniformity of content", "identification", "potency", "ph")


def rule_severity(entry: NSQEntry, spurious: bool = False) -> tuple[str, str]:
    """Severity from the failed test and the dosage form (deterministic)."""
    name, result = entry.drug_name.lower(), entry.result.lower()
    if spurious:
        return "CRITICAL", "The product is listed as spurious (fake or falsified)."
    sterile = any(f in name for f in _STERILE_FORMS)
    if any(t in result for t in _CRITICAL_TESTS):
        if sterile:
            return "CRITICAL", f"An injectable/sterile product failed '{entry.result}' - risk of infection or harm."
        return "HIGH", f"Failed '{entry.result}' - possible contamination."
    if any(t in result for t in _HIGH_TESTS):
        return "HIGH", f"Failed '{entry.result}' - the dose or release may be wrong, so treatment may not work."
    return "MODERATE", f"Failed '{entry.result}' - a quality defect (appearance, weight or labelling)."


def rule_draft(item: InventoryItem, entry: NSQEntry, level: str, spurious: bool = False) -> AlertDraft:
    severity, why = rule_severity(entry, spurious)
    if level == "HIGH":
        summary = (f"Our stock of {item.name} (batch {item.batch}) matches a batch reported as "
                   f"{'spurious' if spurious else 'Not of Standard Quality'}. {why}")
        actions = ["Stop dispensing this batch immediately and quarantine all units.",
                   "Check whether any units were already dispensed and inform the treating doctors.",
                   "Return the batch to the supplier and record the return.",
                   "Report to the State Drugs Controller if required by your hospital's policy."]
    else:
        summary = (f"{item.name} appears on the list with batch {entry.batch} "
                   f"({entry.manufacturer.split(',')[0] or 'maker unknown'}). {why} "
                   "Check the batch number printed on our packs.")
        actions = [f"Check the batch number on every pack of {item.name} against {entry.batch}.",
                   "If it matches: stop dispensing, quarantine and return to the supplier.",
                   "If it does not match: dismiss this alert and record the batch checked."]
    note = (f"Subject: Quality alert - {entry.drug_name}, batch {entry.batch}\n\n"
            f"Dear Supplier,\nThe CDSCO list reports {entry.drug_name} batch {entry.batch} "
            f"(manufactured by {entry.manufacturer.split(',')[0]}) as "
            f"{'spurious' if spurious else 'Not of Standard Quality'} (failed: {entry.result}). "
            f"Please confirm whether the stock supplied to us belongs to this batch and arrange its "
            "return and replacement.\n\nRegards,\nPharmacy")
    return AlertDraft(severity=severity, summary=summary, actions=actions, supplier_note=note)


def _build_llm():
    from crewai import LLM
    return LLM(model=f"openai/{settings.api_model}", base_url=settings.api_base_url,
               api_key=settings.api_key, temperature=settings.temperature,
               timeout=settings.request_timeout, **settings.reasoning_kwargs())


def _crew(context: str):
    from crewai import Agent, Crew, Process, Task

    llm = _build_llm()
    risk = Agent(role="Pharmacovigilance Risk Assessor",
                 goal="Judge how serious a failed-quality medicine is for patients",
                 backstory=("You are a clinical pharmacist. You know that sterility or particulate "
                            "failures in injectables are critical, assay or dissolution failures make "
                            "treatment unreliable, and appearance/weight failures are usually minor."),
                 llm=llm, allow_delegation=False, verbose=settings.crew_verbose, max_iter=2)
    writer = Agent(role="Pharmacy Notice Writer",
                   goal="Write clear, practical alerts for pharmacists and suppliers",
                   backstory=("You write short, factual notices. You never invent facts that are not "
                              "in the case details, and you never tell anyone to stop a patient's "
                              "treatment without consulting the doctor."),
                   llm=llm, allow_delegation=False, verbose=settings.crew_verbose, max_iter=2)
    assess = Task(description=("Assess this case and give the severity (CRITICAL, HIGH, MODERATE or "
                               "LOW) with a one-sentence reason.\n\n" + context),
                  expected_output="Severity and a one-sentence reason.", agent=risk)
    write = Task(description=("Using the assessment, write the alert for the pharmacist. Return JSON "
                              "with keys: severity, summary (1-2 sentences), actions (3-5 short steps), "
                              "supplier_note (a short polite email asking the supplier to confirm the "
                              "batch and arrange return).\n\n" + context),
                 expected_output="JSON matching the AlertDraft schema.", agent=writer,
                 context=[assess], output_pydantic=AlertDraft)
    return Crew(agents=[risk, writer], tasks=[assess, write], process=Process.sequential,
                verbose=settings.crew_verbose)


def _parse(result) -> AlertDraft:
    if getattr(result, "pydantic", None) is not None:
        return AlertDraft.model_validate(result.pydantic.model_dump())
    raw = getattr(result, "raw", "") or str(result)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("crew returned no JSON")
    return AlertDraft.model_validate(json.loads(m.group(0)))


def draft_alert(item: InventoryItem, entry: NSQEntry, level: str, spurious: bool = False,
                review_reasons: list[str] | None = None, use_ai: bool = True) -> AlertDraft:
    base = rule_draft(item, entry, level, spurious)
    if not (use_ai and settings.llm_enabled):
        return base
    context = (f"Our stock: {item.name} | maker: {item.manufacturer or 'not recorded'} | batch: "
               f"{item.batch or 'not recorded'} | qty {item.quantity} at {item.location or 'pharmacy'}\n"
               f"Listed product: {entry.drug_name} | batch {entry.batch} | maker: {entry.manufacturer} | "
               f"expiry {entry.exp_date} | failed test: {entry.result} | tested by {entry.reported_by}\n"
               f"Match confidence: {level} ({'exact batch' if level == 'HIGH' else 'batch not confirmed'})\n"
               f"List type: {'SPURIOUS (fake) drug' if spurious else 'Not of Standard Quality'}\n"
               f"Rule-based severity: {base.severity}\n"
               + (f"Reviewer notes: {'; '.join(review_reasons)}\n" if review_reasons else ""))

    @with_retries("CrewAI alert crew", logger=log)
    def run() -> AlertDraft:
        return _parse(_crew(context).kickoff())

    try:
        draft = call_with_fallback(run, log)
    except Exception as exc:  # noqa: BLE001 - the rule-based draft is always available
        log.warning("crew failed (%s); using the rule-based draft", str(exc)[:160])
        return base
    # Never let the AI downgrade a rule-based CRITICAL (e.g. sterility failure in an injection).
    order = ["LOW", "MODERATE", "HIGH", "CRITICAL"]
    if order.index(draft.severity) < order.index(base.severity):
        draft.severity = base.severity
    if not draft.actions:
        draft.actions = base.actions
    if not draft.supplier_note:
        draft.supplier_note = base.supplier_note
    log.info("drafted alert for %r: %s", item.name, draft.severity)
    return draft
