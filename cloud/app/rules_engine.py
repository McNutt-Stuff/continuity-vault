"""Declarative ingestion rules — the compliance engine.

Rules are evaluated on ingestion, BEFORE a SearchDocument is written, against the
normalized fields of each object (doc_type, category, title, source_type, labels,
and every connector-declared metadata key). A rule is ``IF <conditions> THEN
<actions>`` where conditions combine with AND (``match="all"``) or OR
(``match="any"``) and actions can label, tag as restricted, obfuscate the preview,
decide indexing, or discard the object entirely.

Rules take precedence over the basic Data Map logic. The engine is pure/stateless
so it runs identically on the control plane and on every customer-tenant node
(the Rule rows are federated to nodes), keeping evaluation local to ingestion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

# Plan entitlement ranking — a rule (or an individual action) tagged with a higher
# plan than the tenant holds is skipped, so the same rule set degrades gracefully
# across Personal / Family / Business.
PLAN_RANK: Dict[str, int] = {"personal": 0, "family": 1, "business": 2}

# Exposed to the builder UI (via GET /rules/options).
OPERATORS = [
    {"id": "equals", "label": "equals"},
    {"id": "not_equals", "label": "does not equal"},
    {"id": "contains", "label": "contains"},
    {"id": "not_contains", "label": "does not contain"},
    {"id": "starts_with", "label": "starts with"},
    {"id": "ends_with", "label": "ends with"},
    {"id": "regex", "label": "matches regex"},
    {"id": "exists", "label": "is present"},
    {"id": "not_exists", "label": "is empty"},
    {"id": "gt", "label": "greater than"},
    {"id": "lt", "label": "less than"},
]

# Action types + the minimum plan that unlocks each (bifurcated entitlement).
ACTION_TYPES = [
    {"id": "label", "label": "Add label", "needs_value": True, "min_plan": "personal"},
    {"id": "tag", "label": "Add tag", "needs_value": True, "min_plan": "personal"},
    {"id": "restrict", "label": "Mark restricted", "needs_value": False, "min_plan": "family"},
    {"id": "obfuscate", "label": "Obfuscate preview", "needs_value": False, "min_plan": "family"},
    {"id": "no_index", "label": "Don't index (store only)", "needs_value": False, "min_plan": "business"},
    {"id": "index", "label": "Force index", "needs_value": False, "min_plan": "business"},
    {"id": "discard", "label": "Discard (don't back up)", "needs_value": False, "min_plan": "business"},
]

# Common fields surfaced as builder suggestions (any meta key also works).
FIELD_SUGGESTIONS = [
    "from", "to", "subject", "folder", "path", "account", "doc_type",
    "category", "title", "source_type", "label", "kind", "mime", "headline",
]


@dataclass
class RuleOutcome:
    """The combined effect of every matching rule for one object."""
    discard: bool = False
    no_index: bool = False
    restricted: bool = False
    obfuscate: bool = False
    add_labels: List[str] = field(default_factory=list)
    matched: List[Dict[str, Any]] = field(default_factory=list)  # [{id,name,actions}]

    @property
    def applied(self) -> bool:
        return bool(self.matched)

    @property
    def rule_names(self) -> List[str]:
        return [m.get("name", "") for m in self.matched if m.get("name")]


def _plan_rank(plan: Optional[str]) -> int:
    return PLAN_RANK.get((plan or "personal").strip().lower(), 0)


def _field_value(fields: Dict[str, Any], field_name: str) -> Any:
    """Resolve a condition field to the object's value. ``meta.<k>`` reads a
    metadata key; a bare name matches a top-level attribute or a metadata key
    (case-insensitively); ``label`` returns the labels list."""
    if not field_name:
        return None
    fl = field_name.strip().lower()
    meta = fields.get("meta") or {}
    if fl.startswith("meta."):
        return meta.get(field_name.split(".", 1)[1])
    if fl in ("label", "labels"):
        return fields.get("labels") or []
    if fl in ("doc_type", "category", "source_type", "title"):
        return fields.get(fl)
    if field_name in meta:
        return meta[field_name]
    for k, v in meta.items():
        if str(k).lower() == fl:
            return v
    return fields.get(field_name)


def _apply_op(op: str, actual: Any, val: Any) -> bool:
    op = (op or "equals").strip().lower()
    if op == "exists":
        return actual not in (None, "", [], {})
    if op == "not_exists":
        return actual in (None, "", [], {})
    # List-valued fields (labels): match if any element satisfies the comparison.
    if isinstance(actual, (list, tuple, set)):
        items = [str(x).lower() for x in actual]
        v = "" if val is None else str(val).lower()
        hit = any(v == it or v in it for it in items)
        if op in ("not_contains", "not_equals"):
            return not hit
        return hit
    a = "" if actual is None else str(actual)
    b = "" if val is None else str(val)
    al, bl = a.lower(), b.lower()
    if op in ("equals", "is"):
        return al == bl
    if op == "not_equals":
        return al != bl
    if op == "contains":
        return bl in al
    if op == "not_contains":
        return bl not in al
    if op == "starts_with":
        return al.startswith(bl)
    if op == "ends_with":
        return al.endswith(bl)
    if op == "regex":
        try:
            return re.search(str(val), a, re.IGNORECASE) is not None
        except re.error:
            return False
    if op == "gt":
        try:
            return float(a) > float(b)
        except (TypeError, ValueError):
            return al > bl
    if op == "lt":
        try:
            return float(a) < float(b)
        except (TypeError, ValueError):
            return al < bl
    return False


def _eval_condition(cond: Dict[str, Any], fields: Dict[str, Any]) -> bool:
    return _apply_op(cond.get("op"), _field_value(fields, cond.get("field") or ""),
                     cond.get("value"))


def _rule_matches(rule: Any, fields: Dict[str, Any]) -> bool:
    conds = list(getattr(rule, "conditions", None) or [])
    if not conds:
        return False  # a rule with no conditions never matches (no accidental catch-all)
    match = (getattr(rule, "match", "all") or "all").strip().lower()
    results = (_eval_condition(c, fields) for c in conds)
    return any(results) if match == "any" else all(_eval_condition(c, fields) for c in conds)


def object_fields(*, doc_type: str, category: str, title: str, source_type: str,
                  labels: Optional[Iterable[str]], meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the flat field view the engine evaluates against."""
    return {
        "doc_type": doc_type or "",
        "category": category or "",
        "title": title or "",
        "source_type": source_type or "",
        "labels": list(labels or []),
        "meta": dict(meta or {}),
    }


def evaluate(rules: Iterable[Any], fields: Dict[str, Any], plan: str = "business") -> RuleOutcome:
    """Evaluate ``rules`` (already scoped to the collection, enabled) against one
    object's ``fields``. Rules run in caller order (priority); a ``discard`` action
    short-circuits the rest for that object."""
    outcome = RuleOutcome()
    prank = _plan_rank(plan)
    for rule in rules:
        if not getattr(rule, "enabled", True):
            continue
        if _plan_rank(getattr(rule, "min_plan", "personal")) > prank:
            continue
        if not _rule_matches(rule, fields):
            continue
        applied: List[str] = []
        for act in (getattr(rule, "actions", None) or []):
            atype = str(act.get("type") or "").strip().lower()
            amin = act.get("min_plan") or getattr(rule, "min_plan", "personal")
            if _plan_rank(amin) > prank:
                continue  # action not entitled on this plan
            if atype in ("label", "tag") and act.get("value"):
                lbl = str(act["value"]).strip()
                if lbl and lbl not in outcome.add_labels:
                    outcome.add_labels.append(lbl)
                applied.append(atype)
            elif atype == "restrict":
                outcome.restricted = True
                applied.append(atype)
            elif atype == "obfuscate":
                outcome.obfuscate = True
                applied.append(atype)
            elif atype == "no_index":
                outcome.no_index = True
                applied.append(atype)
            elif atype == "index":
                outcome.no_index = False
                applied.append(atype)
            elif atype == "discard":
                outcome.discard = True
                applied.append(atype)
        if applied:
            outcome.matched.append({"id": getattr(rule, "id", None),
                                    "name": getattr(rule, "name", ""),
                                    "actions": applied})
        if outcome.discard:
            break
    return outcome


def mask(text: Optional[str]) -> str:
    """Obfuscate a preview string — keep length + first char, redact the rest."""
    s = "" if text is None else str(text)
    if not s:
        return s
    if len(s) <= 2:
        return "•" * len(s)
    return s[0] + "•" * min(len(s) - 1, 24)
