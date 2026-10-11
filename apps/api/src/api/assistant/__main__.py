"""Assistant command line.

    python -m api.assistant index                 embed chunks that have no embedding yet
    python -m api.assistant ask "What is the Barail top in SA-07?" --blocks SA-BLK-1
    python -m api.assistant eval --modes rules noisy [llm] [--write-docs]

Database: DATABASE_URL (owner for `index`; questions run under the given blocks with RLS).
The evaluation expects the synthetic truth loaded:
    cd data && uv run python -m synthetic_assam.load_truth
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import psycopg

from api.assistant import evaluate as ev
from api.assistant.retrieval import embedder_from_env, index
from api.assistant.service import PROMPTS, Assistant
from api.auth import User
from core.llm import LLM, LLMConfig, PgLog

DOCS = Path(__file__).resolve().parents[5] / "docs" / "assistant.md"


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"])


def _llm() -> LLM:
    return LLM(
        LLMConfig.from_env(),
        PgLog(lambda: psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)),
        prompts=PROMPTS,
    )


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    p = argparse.ArgumentParser(prog="python -m api.assistant")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index", help="embed chunks without an embedding from the current model")
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--blocks", nargs="+", required=True)
    a.add_argument("--jvs", nargs="*", default=[])
    a.add_argument("--no-llm", action="store_true")
    e = sub.add_parser("eval")
    e.add_argument(
        "--modes", nargs="+", default=["rules", "noisy"], choices=["rules", "noisy", "llm"]
    )
    e.add_argument("--set", type=Path, default=ev.EVAL_SET)
    e.add_argument("--write-docs", action="store_true")
    args = p.parse_args()

    emb = embedder_from_env()
    if args.cmd == "index":
        with _connect() as conn:
            print(f"embedded {index(conn, emb)} chunks with {emb.name}")
        return
    if args.cmd == "ask":
        user = User("cli", frozenset({"planner"}), tuple(args.blocks), tuple(args.jvs))
        res = Assistant(_connect, None if args.no_llm else _llm(), emb).ask(user, args.question)
        print(json.dumps(res.json(), indent=1, default=str))
        return
    meta, questions = ev.load_set(args.set)
    user = User("eval", frozenset({"planner"}), tuple(meta["user_blocks"]))
    reports = {}
    for mode in args.modes:
        log = None
        if mode == "rules":
            asst = Assistant(_connect, None, emb)
        elif mode == "noisy":
            drafter, log = ev.noisy_drafter()
            asst = Assistant(_connect, None, emb, drafter=drafter)
        else:
            asst = Assistant(_connect, _llm(), emb)
        reports[mode] = ev.run(asst, questions, user, log)
    md = ev.report(reports)
    print(md)
    if args.write_docs:
        text = DOCS.read_text(encoding="utf-8")
        DOCS.write_text(
            re.sub(
                r"(<!-- eval:start -->\n).*?(\n<!-- eval:end -->)",
                lambda m: m.group(1) + md + m.group(2),
                text,
                flags=re.S,
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
