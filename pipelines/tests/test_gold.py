"""Acceptance on the synthetic gold set: field-level F1 >= 0.85 on typed pages, and every
low-confidence field is routed to review (and nothing else is)."""

import json
from pathlib import Path

from pipelines.evaluate import prf, score
from pipelines.extract import PARSERS
from pipelines.layout import text_layer
from pipelines.process import Settings, decide, process

RAW = Path(__file__).resolve().parents[2] / "data" / "raw"


def test_typed_pages_f1_and_review_routing(gold_path: Path, tmp_path: Path) -> None:
    docs = [d for d in json.loads(gold_path.read_text())["documents"] if d["render"] == "typed"]
    assert len(docs) >= 10
    tp = fp = fn = 0
    s = Settings(engines=("text_layer",))
    for d in docs:
        r = process(RAW / d["path"], settings=s, cache=tmp_path / Path(d["path"]).stem)
        c = score(d["fields"], r)
        tp += sum(v for (_, k), v in c.items() if k == "tp")
        fp += sum(v for (_, k), v in c.items() if k == "fp")
        fn += sum(v for (_, k), v in c.items() if k == "fn")
        for f in r.fields.values():
            if f.confidence < s.threshold:
                assert not f.accepted and f in r.review()
            else:
                assert f.accepted
    p, rec, f1 = prf(tp, fp, fn)
    assert f1 >= 0.85, (p, rec, f1)


def test_second_pass_agreement_accepts_and_disagreement_reviews(gold_path: Path) -> None:
    d = next(
        x
        for x in json.loads(gold_path.read_text())["documents"]
        if x["render"] == "typed" and x["doc_type"] == "wcr"
    )
    pages = text_layer(RAW / d["path"])
    a, b = PARSERS["wcr"](pages), PARSERS["wcr"](pages)
    b.source = "llm"
    b.values["td_md"].value = 1.0  # the second pass read something else
    r = decide("wcr", 1.0, a, b, ["text_layer"], Settings())
    assert r.fields["well_name"].accepted and r.fields["well_name"].confidence == 1.0
    assert not r.fields["td_md"].accepted
    assert r.fields["td_md"].components["consistency"] == 0.0
