"""Explicit submission state machine. Each transition names who may perform it."""
from __future__ import annotations

SYSTEM, REVIEWER, ADMIN = "system", "reviewer", "admin"

TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "SUBMITTED": {"VALIDATING": {SYSTEM}},
    "VALIDATING": {"VALIDATION_FAILED": {SYSTEM}, "PENDING_REVIEW": {SYSTEM}, "ACCEPTED_FOR_EVALUATION": {SYSTEM}},
    "VALIDATION_FAILED": {"RESUBMISSION_ALLOWED": {SYSTEM}, "DROPPED": {SYSTEM}, "DEADLINE_MISSED": {SYSTEM}},
    "PENDING_REVIEW": {"REVIEW_CLEARED": {REVIEWER, ADMIN}, "CORRECTIONS_REQUESTED": {REVIEWER, ADMIN},
                       "REJECTED_BY_AUTHORIZED_REVIEW": {REVIEWER, ADMIN}},
    "REVIEW_CLEARED": {"ACCEPTED_FOR_EVALUATION": {SYSTEM}},
    "CORRECTIONS_REQUESTED": {"RESUBMISSION_ALLOWED": {SYSTEM}, "DROPPED": {SYSTEM}, "DEADLINE_MISSED": {SYSTEM}},
    "RESUBMISSION_ALLOWED": {"DEADLINE_MISSED": {SYSTEM}, "DROPPED": {ADMIN}},
    "ACCEPTED_FOR_EVALUATION": {},
    "REJECTED_BY_AUTHORIZED_REVIEW": {},
    "DROPPED": {},
    "DEADLINE_MISSED": {},
}
STATES = set(TRANSITIONS)
TERMINAL = {s for s, t in TRANSITIONS.items() if not t}
# States that mean "a submission is still being handled" -> a new attempt is not allowed yet.
IN_PROGRESS = {"SUBMITTED", "VALIDATING", "PENDING_REVIEW", "REVIEW_CLEARED"}


class InvalidTransition(ValueError):
    pass


def check(from_state: str, to_state: str, actor_kind: str) -> None:
    allowed = TRANSITIONS.get(from_state, {})
    if to_state not in allowed:
        raise InvalidTransition(f"Transition {from_state} -> {to_state} is not allowed")
    if actor_kind not in allowed[to_state]:
        raise InvalidTransition(f"Actor '{actor_kind}' may not perform {from_state} -> {to_state}")
