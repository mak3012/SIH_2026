"""Refusals, decided by rules before any retrieval or model call.

The assistant answers questions about the drilling records. It refuses:

- S-WELL-CONTROL  anything that would override, bypass or postpone well-control procedure or a
                  barrier (shut-in, BOP, flow check, kill, alarms), when asked for oneself
                  ("how do I", "can we", "is it ok to", an imperative). Questions about what a
                  crew did ("did they skip the flow check on SA-03?") are answered from records;
- S-LIVE-OPS      instructions for live operations ("should I raise the mud weight now?"):
                  those are the drilling team's decisions, made under their procedures;
- S-WRITE         requests to change, delete or add records (the assistant is read-only);
- S-INJECTION     attempts to change its instructions or reveal its prompt;
- S-ACCESS        attempts to reach data outside the caller's licences;
- S-SCOPE         questions with nothing to do with wells and drilling records.

A refusal says why and, where it can, what the assistant can do instead.
"""

import re
from dataclasses import dataclass

WC = (
    r"(shut[\s-]?in|bop|blow[\s-]?out preventer|well[\s-]?control|kill (line|sheet|procedure)|"
    r"flow[\s-]?check|barriers?|choke|kicks?|influx|pit (gain|drill)|alarms?|alerts?|"
    r"standing orders?|accumulator|annular preventer|rams?)"
)
OVERRIDE = (
    r"(ignore|bypass|override|skip|disable|defeat|silence|mute|postpone|circumvent|"
    r"turn off|switch off|get around|work around|cheat|fake|falsify)"
)
DOMAIN = re.compile(
    r"\b(wells?|wellbores?|drill\w*|formations?|tops?|casing|shoe|cement\w*|mud|mw|ecd|lot|fit|"
    r"leak[\s-]?off|integrity|kicks?|loss(es)?|lost|returns|stuck|pipe|npt|depths?|md|tvd\w*|"
    r"bbl|barrels?|rig|bit|events?|problems?|reports?|ddr|wcr|survey|field|block|spud|td|"
    r"mitigation\w*|lcm|pill|trip\w*|hole|pressure|voice|note|lessons?|emw|sg|ppg|grade|"
    r"liner|section|offset|hazard\w*|barail|tipam|kopili|girujan|sylhet|langpar|dhekiajuli|"
    r"overpull|fishing|sidetrack\w*|cause|root)\b",
    re.I,
)
# Asking for oneself, not about what a crew did.
INTENT = re.compile(
    r"\b(i|we|me|us|my|our|you)\b|\bhow (to|can|do|could|should)\b|"
    r"\bis it (ok|okay|safe|fine|acceptable)\b|^\s*(please\s+)?" + OVERRIDE + r"\b",
    re.I,
)


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern[str]
    message: str
    needs_intent: bool = False


RULES = [
    Rule(
        "S-WELL-CONTROL",
        re.compile(
            r"\b"
            + OVERRIDE
            + r"\b.{0,60}\b"
            + WC
            + r"\b|\b"
            + WC
            + r"\b.{0,40}\b"
            + OVERRIDE
            + r"\b|\b(keep|continue|carry on)\s+(drilling|tripping|pumping)\b.{0,40}\b(with|"
            r"through|despite|during|ignoring)\b.{0,20}\b(kicks?|influx|flow|gain|losses|"
            r"alarms?|alerts?)\b|\b(without|no need (to|for)|don'?t)\s+(shutting|shut|"
            r"(a |the )?flow[\s-]?check|checking for flow|closing the bop)",
            re.I,
        ),
        "I can't help with overriding or postponing well-control procedure, barriers or alarms. "
        "Follow your well-control procedure and the instructions of the driller, toolpusher and "
        "company representative: if in doubt, shut in. I can show what happened in offset "
        "wells, from the records.",
        needs_intent=True,
    ),
    Rule(
        "S-LIVE-OPS",
        re.compile(
            r"\bshould\s+(i|we|the driller|the rig)\s+(now\s+)?(increase|raise|reduce|lower|cut|"
            r"stop|continue|keep|pull|trip|pump|close|open|bleed|weight up|circulate)\b|"
            r"\b(tell|instruct)\s+(me|the driller|the rig)\s+(to|what to)\b",
            re.I,
        ),
        "Decisions on the live well belong to the drilling team under their procedures; I don't "
        "give operating instructions. I can show what offset wells recorded in the same "
        "formation, with the source pages.",
    ),
    Rule(
        "S-WRITE",
        re.compile(
            r"\b(delete|remove|drop|update|change|edit|modify|insert|add|overwrite|correct)\b"
            r".{0,40}\b(records?|rows?|tables?|events?|tops?|casing|facts?|data(base)?|"
            r"reports?)\b|^\s*(delete|update|insert|drop|alter|truncate|grant)\b",
            re.I,
        ),
        "I only read the records; I can't change them. Corrections go through the review queue "
        "with a named reviewer.",
    ),
    Rule(
        "S-INJECTION",
        re.compile(
            r"\b(ignore|disregard|forget)\b.{0,30}\b(previous|prior|above|your|all|the)\b.{0,20}"
            r"\b(instructions?|rules?|prompts?|guidelines?)\b|\bsystem prompt\b|\bjailbreak\b|"
            r"\bpretend (to be|you are)\b|\bdeveloper mode\b",
            re.I,
        ),
        "I can't change how I work or show my instructions. Ask me about the well records.",
    ),
    Rule(
        "S-ACCESS",
        re.compile(
            r"\b(bypass|disable|get around|ignore)\b.{0,30}\b(rls|row[\s-]level|access|"
            r"permissions?|licen[cs]es?|security|abac)\b|\b(all|other|every)\s+(blocks?|"
            r"operators?|jvs?|partners?)'?\s*(data|wells|records)\b.{0,30}\b(regardless|even if|"
            r"that i)",
            re.I,
        ),
        "I answer only from the wells your licences give you access to.",
    ),
]
SCOPE_MESSAGE = (
    "That is outside what I can answer. I answer questions about the drilling records: wells, "
    "formation tops, casing and shoe tests, drilling problems (losses, kicks, stuck pipe), mud "
    "weights, NPT and lessons, with the source pages."
)


def check(question: str, has_entities: bool) -> tuple[str, str] | None:
    """(rule id, message) if the question must be refused."""
    for r in RULES:
        if r.pattern.search(question) and (not r.needs_intent or INTENT.search(question)):
            return r.id, r.message
    if not has_entities and not DOMAIN.search(question):
        return "S-SCOPE", SCOPE_MESSAGE
    return None
