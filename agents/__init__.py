"""Screening agents that run after the format checks on every submission and draft check.

  guardrail   instructions aimed at the evaluator, hidden text, obfuscation        -> human review + admin alert
  content     stage 2 after the keyword check: reads the whole text (AI when keys are set, otherwise the local
              reader) and requires at least 75 % technical content                 -> rejected (or review, if set)
  assigner    proposes reviewers for accepted abstracts; the admin approves         (see agents/assigner.py)

Manipulation attempts always go to a person; the content analyser rejects (with the reasons) unless the admin chose review."""
from __future__ import annotations

from validation.checks import CheckResult

from . import content, guardrail

GUARDRAIL_IDS = {"GUARDRAIL_INSTRUCTIONS", "GUARDRAIL_HIDDEN_TEXT", "GUARDRAIL_OBFUSCATION"}


def screen(text: str, data: bytes | None, file_name: str, ps: dict, track: dict | None, policy: dict, ai_client=None,
           keywords_ok: bool = True) -> list[CheckResult]:
    cfg = policy.get("agents", {})
    if not text:
        return []
    out: list[CheckResult] = []
    if cfg.get("guardrail", {}).get("enabled", True):
        out += guardrail.scan(text, data, file_name)
    out += content.analyse(text, ps, track, policy, ai_client, keywords_ok)
    return out


def guardrail_hit(results) -> bool:
    return any(r.check_id in GUARDRAIL_IDS and r.review_flag for r in results)


def keywords_ok(results) -> bool:
    """Stage 1 result: did the keyword (domain relevance) check pass?"""
    return all(r.result == "pass" for r in results if r.check_id == "DOMAIN_RELEVANCE")
