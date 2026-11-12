"""AutoGen = second opinion on UNCERTAIN matches (Matcher vs Sceptic debate).

Only LOW-confidence matches come here (same drug name, maker unknown or unclear),
grouped per inventory item, so each item costs one short debate.
API: autogen-agentchat v0.4+ (0.7.x) with OpenAIChatCompletionClient.
"""

from __future__ import annotations

import asyncio
import re

from config import call_with_fallback, get_logger, settings, with_retries
from models import InventoryItem, NSQEntry, Review

log = get_logger("AutoGen")

MARKER = "FINAL DECISION"

AGENTS = {
    "Matcher": (
        "You are the Matcher, a hospital pharmacist. You get one medicine from our stock and "
        "candidate entries from a government list of medicines that FAILED quality tests. For each "
        "candidate, say whether our stock could be that same product: compare drug and strength, "
        "brand, manufacturer, batch number and expiry. If our stock record lacks a detail (e.g. no "
        "manufacturer), say so - it means we cannot rule the candidate out. Be concise."
    ),
    "Sceptic": (
        "You are the Sceptic. Challenge the Matcher to avoid false alarms AND missed risks: a "
        "different manufacturer or strength means a different product; but missing details in our "
        "stock record mean the pharmacist must check the physical pack. Then end with EXACTLY:\n\n"
        f"{MARKER}: MATCH (candidates 1, 3)  or  {MARKER}: NOT_MATCH  or  {MARKER}: UNSURE "
        "(candidates 2)\nREASONS:\n- <reason>\n- <reason>\n\n"
        "Use MATCH only when the evidence shows it is the same product and batch. Use UNSURE when "
        "our record is missing the detail needed - the pharmacist will check the pack."
    ),
}


def _describe(item: InventoryItem, candidates: list[NSQEntry]) -> str:
    lines = [f"OUR STOCK: {item.name} | maker: {item.manufacturer or '(not recorded)'} | "
             f"batch: {item.batch or '(not recorded)'} | expiry: {item.expiry or '(not recorded)'}",
             "", "CANDIDATES FROM THE FAILED-QUALITY LIST:"]
    for i, e in enumerate(candidates, 1):
        lines.append(f"{i}. {e.drug_name} | maker: {e.manufacturer[:120]} | batch: {e.batch} | "
                     f"expiry: {e.exp_date} | failed: {e.result}")
    return "\n".join(lines)


_DECISION_RE = re.compile(rf"{MARKER}\W*(MATCH|NOT[_\s-]?MATCH|UNSURE)([^\n]*)", re.I)


def parse_review(messages: list[tuple[str, str]], n: int) -> Review:
    transcript = "\n\n".join(f"### {who}\n{text}" for who, text in messages)
    for who, text in reversed(messages):
        m = _DECISION_RE.search(text)
        if not m:
            continue
        word = m.group(1).upper().replace(" ", "_").replace("-", "_")
        decision = "NOT_MATCH" if word.startswith("NOT") else word
        nums = sorted({int(x) for x in re.findall(r"\d+", m.group(2)) if 1 <= int(x) <= n})
        reasons = [re.sub(r"\*\*|`", "", ln).strip(" -*\t") for ln in text[m.end():].splitlines()
                   if ln.strip().startswith(("-", "*")) and not re.match(r"^\W*reasons?\b", ln.strip(" -*"), re.I)]
        if decision in ("MATCH", "UNSURE") and not nums:
            nums = list(range(1, n + 1))
        return Review(decision=decision, matched=nums, reasons=[r for r in reasons if r][:5],
                      transcript=transcript)
    return Review(decision="UNSURE", matched=list(range(1, n + 1)),
                  reasons=["The reviewers gave no clear decision - check the physical pack."],
                  transcript=transcript)


def build_model_client():
    from autogen_ext.models.openai import OpenAIChatCompletionClient
    return OpenAIChatCompletionClient(
        model=settings.api_model, base_url=settings.api_base_url, api_key=settings.api_key,
        temperature=settings.temperature, timeout=settings.request_timeout,
        model_info=settings.model_info, **settings.reasoning_kwargs())


async def _debate(item: InventoryItem, candidates: list[NSQEntry], client) -> Review:
    from autogen_agentchat.agents import AssistantAgent
    from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
    from autogen_agentchat.teams import RoundRobinGroupChat

    team = RoundRobinGroupChat(
        [AssistantAgent(n, model_client=client, system_message=p) for n, p in AGENTS.items()],
        termination_condition=TextMentionTermination(MARKER, sources=["Sceptic"])
        | MaxMessageTermination(settings.review_max_messages + 1))
    result = await team.run(task=_describe(item, candidates) + "\n\nMatcher, give your assessment.")
    messages = [(m.source, m.content) for m in result.messages
                if isinstance(getattr(m, "content", None), str) and m.source in AGENTS]
    for who, text in messages:
        log.info("%s: %s", who, text.strip().splitlines()[0][:110] if text.strip() else "")
    return parse_review(messages, len(candidates))


def review_candidates(item: InventoryItem, candidates: list[NSQEntry], model_client=None) -> Review:
    """Debate whether `item` is any of `candidates`. Without an AI key: UNSURE."""
    if model_client is None and not settings.llm_enabled:
        return Review(decision="UNSURE", matched=list(range(1, len(candidates) + 1)),
                      reasons=["AI review is not configured (no LLM_API_KEY) - check the pack manually."])

    @with_retries("AutoGen review", logger=log)
    def run() -> Review:
        client = model_client or build_model_client()

        async def go():
            try:
                return await _debate(item, candidates, client)
            finally:
                if model_client is None:
                    await client.close()
        return asyncio.run(go())

    log.info("reviewing %r against %d candidate(s)", item.name, len(candidates))
    review = call_with_fallback(run, log) if model_client is None else run()
    log.info("decision for %r: %s %s", item.name, review.decision, review.matched or "")
    return review
