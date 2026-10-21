#!/usr/bin/env python3
"""Tiny terminal labeler for replay cases exported by export_replay_cases.py.

Usage::

    python tools/label_replay_cases.py data/replay/cases.jsonl \\
        --out data/replay/labeled.jsonl

For every case the tool prints the context, the trigger and the messages that
followed, then asks for one key:

    s  speak (the bot should answer)          S  speak, must
    n  silent (the bot should stay quiet)     N  silent, must
    e  either is fine
    b  bad case / unusable: recorded as category "bad", never asked again
    u  undo the previous answer of this session
    q  quit (everything is already saved)

``s``/``n`` set strength "should", ``S``/``N`` set "must", ``e`` sets "free".
After the action key an optional second key picks the addressee: ``b`` bot,
``m`` other_member, ``g`` group, ``n`` nobody, Enter = skip (stored as null).
Both keys may be typed together, e.g. ``sb`` or ``Ng``.

Every answer is written immediately with an atomic replace, so nothing is lost
if the terminal dies.  Running the tool again with the same ``--out`` resumes
at the first case without an answer.  The output only contains answered cases
(labeled ones carry ``expect``; bad ones have category "bad" and
``expect: null``); other fields are copied unchanged, ``category`` is only
touched when ``--category`` is given.  The tool refuses to resume when a case
already in the output differs from the case with the same id in the input.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

SCHEMA_VERSION = "replay-case/v1"

#: first key -> (action, strength)
ACTION_KEYS: dict[str, tuple[str, str]] = {
    "s": ("speak", "should"),
    "S": ("speak", "must"),
    "n": ("silent", "should"),
    "N": ("silent", "must"),
    "e": ("either", "free"),
    "E": ("either", "free"),
}
#: second key -> addressee
ADDRESSEE_KEYS: dict[str, str] = {
    "b": "bot",
    "m": "other_member",
    "g": "group",
    "n": "nobody",
}

ACTION_PROMPT = ("[s]peak [n]silent [e]ither [b]ad/skip [u]ndo [q]uit+save "
                 "(S/N = must)  > ")
ADDRESSEE_PROMPT = ("addressee: [b]ot [m]ember(other) [g]roup [n]obody "
                    "(Enter = skip, u = undo)  > ")

_LINE_BREAK_ESCAPES = {
    "\U00002028": "\\u2028",
    "\U00002029": "\\u2029",
    "\U00000085": "\\u0085",
}
_MAX_LINE_CHARS = 400

InputFn = Callable[[str], str]
PrintFn = Callable[[str], None]


class LabelError(Exception):
    """Fatal, user-facing error (reported without a traceback)."""


@dataclass
class LabelSummary:
    labeled: int = 0          # answered in this session (speak/silent/either)
    bad: int = 0              # marked bad in this session
    already_done: int = 0     # answered in an earlier session
    remaining: int = 0
    stopped: str = "finished"  # finished | quit | eof


# --------------------------------------------------------------------------
# File helpers
# --------------------------------------------------------------------------


def dumps_case(case: dict) -> str:
    text = json.dumps(case, ensure_ascii=False, separators=(",", ":"))
    for raw, escaped in _LINE_BREAK_ESCAPES.items():
        if raw in text:
            text = text.replace(raw, escaped)
    return text


def atomic_write_text(path: Path, text: str) -> None:
    """Write next to *path*, fsync, then atomically replace it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def read_cases(path: Path, what: str = "cases") -> list[dict]:
    """Parse a replay-case JSONL file; every line must be a case object with an id
    (``schema`` may be absent, but if present it must be ``replay-case/v1``)."""
    records: list[dict] = []
    seen: set[str] = set()
    try:
        handle = open(path, encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise LabelError(f"cannot read {what} file {path}: {exc}") from exc
    with handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LabelError(f"{what} {path}:{lineno}: invalid JSON ({exc.msg})") from exc
            if not isinstance(obj, dict):
                raise LabelError(f"{what} {path}:{lineno}: expected a JSON object")
            schema = obj.get("schema")
            if schema is not None and schema != SCHEMA_VERSION:
                raise LabelError(
                    f"{what} {path}:{lineno}: schema is {schema!r}, "
                    f"expected {SCHEMA_VERSION!r}"
                )
            case_id = obj.get("id")
            if not isinstance(case_id, str) or not case_id:
                raise LabelError(f"{what} {path}:{lineno}: missing string id")
            if not isinstance(obj.get("trigger"), dict):
                raise LabelError(f"{what} {path}:{lineno}: missing trigger object")
            if case_id in seen:
                raise LabelError(f"{what} {path}:{lineno}: duplicate id {case_id}")
            seen.add(case_id)
            records.append(obj)
    return records


def fingerprint(case: dict) -> str:
    """Identity of a case ignoring the fields the labeler writes."""
    body = {k: v for k, v in case.items() if k not in ("category", "expect")}
    return json.dumps(body, sort_keys=True, ensure_ascii=False)


def load_existing(out_path: Path, cases: Sequence[dict]) -> list[dict]:
    """Records already in *out_path* (empty list when the file is missing).

    Raises :class:`LabelError` when a record differs from the input case with
    the same id: resuming would attach old answers to a different message.
    """
    if not Path(out_path).exists():
        return []
    existing = read_cases(Path(out_path), "labeled")
    by_id = {c["id"]: c for c in cases}
    clashes = [r["id"] for r in existing
               if r["id"] in by_id and fingerprint(r) != fingerprint(by_id[r["id"]])]
    if clashes:
        shown = ", ".join(clashes[:5]) + (" ..." if len(clashes) > 5 else "")
        raise LabelError(
            f"{out_path} already holds answers for cases whose content differs "
            f"from the input ({shown}); the input was probably re-exported. "
            "Use a different --out (or move the old file) so labels cannot be "
            "attached to the wrong messages."
        )
    return existing


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _clip(text: str) -> str:
    if len(text) <= _MAX_LINE_CHARS:
        return text
    return text[:_MAX_LINE_CHARS] + f"…(+{len(text) - _MAX_LINE_CHARS} chars)"


def _message_lines(prefix: str, speaker: str, text: str, indent: str = "  ") -> list[str]:
    pieces = _clip(str(text)).split("\n")
    head = f"{indent}{prefix} {speaker}: {pieces[0]}"
    pad = " " * (len(indent) + len(prefix) + 1)
    return [head] + [f"{pad}{piece}" for piece in pieces[1:]]


def _fmt_offset(offset: object) -> str:
    try:
        value = int(offset)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return "?s"
    return f"{value:+d}s"


def render_case(case: dict, *, position: int, total: int, done: int,
                tz_offset_hours: float = 8.0) -> list[str]:
    """Terminal lines for one case (context, trigger, next messages)."""
    chat = case.get("chat") or {}
    trigger = case.get("trigger") or {}
    try:
        tz = timezone(timedelta(hours=tz_offset_hours))
        moment = datetime.fromtimestamp(int(case.get("now", 0)), tz).strftime(
            "%Y-%m-%d %H:%M:%S")
        moment += f" (UTC{tz_offset_hours:+g})"
    except (TypeError, ValueError, OverflowError, OSError):
        moment = "?"
    lines = ["=" * 64,
             f"[{position}/{total}] {case.get('id')} | {chat.get('group_name', '?')}"
             f" | {moment} | answered {done}"]
    if case.get("expect"):
        exp = case["expect"]
        lines.append(f"  (input already has expect: {exp.get('action')}/{exp.get('strength')})")
    context = case.get("context") or []
    lines.append(f"-- context ({len(context)} messages) --")
    if not context:
        lines.append("  (none)")
    for msg in context:
        speaker = msg.get("sender_name", "?")
        kind = msg.get("msg_type", 1)
        if kind not in (1, None):
            speaker += f" <type {kind}>"
        lines.extend(_message_lines(_fmt_offset(msg.get("offset_sec")).rjust(6),
                                    speaker, msg.get("content", "")))
    flags = []
    if trigger.get("is_at_mentioned"):
        flags.append("@bot")
    if trigger.get("is_reply_to_bot"):
        flags.append("reply-to-bot")
    flag_text = f" [{' '.join(flags)}]" if flags else ""
    lines.append(f">>> TRIGGER {trigger.get('sender_name', '?')}{flag_text}")
    for piece in _clip(str(trigger.get("content", ""))).split("\n"):
        lines.append(f"    {piece}")
    later = case.get("next_messages") or []
    lines.append(f"-- next {len(later)} message(s) --")
    if not later:
        lines.append("  (none)")
    for msg in later:
        lines.extend(_message_lines(_fmt_offset(msg.get("offset_sec")).rjust(6),
                                    msg.get("sender_name", "?"), msg.get("content", "")))
    return lines


# --------------------------------------------------------------------------
# Labeling loop
# --------------------------------------------------------------------------


def default_input(prompt: str) -> str:
    """``input()`` on a terminal; echoes piped answers so transcripts stay readable."""
    if sys.stdin.isatty():
        return input(prompt)
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    sys.stdout.write(line if line.endswith("\n") else line + "\n")
    return line.rstrip("\r\n")


def _ask(input_fn: InputFn, prompt: str) -> str | None:
    try:
        return input_fn(prompt)
    except EOFError:
        return None


def label_cases(cases: Sequence[dict], out_path: Path, *,
                input_fn: InputFn = default_input, print_fn: PrintFn = print,
                ask_addressee: bool = True, category: str | None = None,
                tz_offset_hours: float = 8.0) -> LabelSummary:
    """Interactive loop; returns when finished, on ``q`` or on end of input.

    The output file is rewritten (atomically) after every answer.  Cases that
    already have an answer in *out_path* are skipped.
    """
    out_path = Path(out_path)
    order = {case["id"]: i for i, case in enumerate(cases)}
    existing = load_existing(out_path, cases)
    records: dict[str, dict] = {r["id"]: r for r in existing if r["id"] in order}
    orphans = [r for r in existing if r["id"] not in order]  # never dropped
    summary = LabelSummary(already_done=len(records))
    session: list[str] = []

    def persist() -> None:
        ordered = [records[c["id"]] for c in cases if c["id"] in records] + orphans
        atomic_write_text(out_path, "".join(dumps_case(r) + "\n" for r in ordered))

    def undo_last() -> int | None:
        if not session:
            print_fn("nothing to undo (only answers given in this session can be undone)")
            return None
        last = session.pop()
        removed = records.pop(last, None)
        if removed is not None:
            if removed.get("category") == "bad" and removed.get("expect") is None:
                summary.bad -= 1
            else:
                summary.labeled -= 1
            persist()
        print_fn(f"undone: {last}")
        return order[last]

    pos = 0
    total = len(cases)
    show = True  # render the case once; a mistyped key only re-prompts
    while pos < total:
        case = cases[pos]
        case_id = case["id"]
        if case_id in records:
            pos += 1
            show = True
            continue
        if show:
            for line in render_case(case, position=pos + 1, total=total,
                                    done=len(records), tz_offset_hours=tz_offset_hours):
                print_fn(line)
            show = False
        raw = _ask(input_fn, ACTION_PROMPT)
        if raw is None:
            summary.stopped = "eof"
            break
        text = raw.strip()
        if not text:
            print_fn("please press one of: s n e b u q (S/N for 'must')")
            continue
        key = text[0]
        lowered = key.lower()
        if lowered == "q":
            summary.stopped = "quit"
            break
        if lowered == "u":
            back = undo_last()
            if back is not None:
                pos = back
                show = True
            continue
        if lowered == "b":
            bad = copy.deepcopy(case)
            bad["category"] = "bad"
            bad["expect"] = None
            records[case_id] = bad
            session.append(case_id)
            summary.bad += 1
            persist()
            pos += 1
            show = True
            continue
        if key not in ACTION_KEYS:
            print_fn(f"unknown key {key!r}: use s n e b u q (S/N for 'must')")
            continue

        action, strength = ACTION_KEYS[key]
        addressee = None
        if len(text) > 1 and text[1].lower() in ADDRESSEE_KEYS:
            addressee = ADDRESSEE_KEYS[text[1].lower()]
        labeled = copy.deepcopy(case)
        if category:
            labeled["category"] = category
        labeled["expect"] = {"action": action, "strength": strength,
                             "addressee": addressee, "note": ""}
        records[case_id] = labeled
        session.append(case_id)
        summary.labeled += 1
        persist()  # saved before the optional second question

        if addressee is None and ask_addressee:
            second = _ask(input_fn, ADDRESSEE_PROMPT)
            if second is None:
                summary.stopped = "eof"
                break
            choice = second.strip()[:1]
            if choice.lower() == "q":
                summary.stopped = "quit"
                break
            if choice.lower() == "u":
                back = undo_last()
                if back is not None:
                    pos = back
                    show = True
                continue
            if choice:
                if choice.lower() in ADDRESSEE_KEYS:
                    labeled["expect"]["addressee"] = ADDRESSEE_KEYS[choice.lower()]
                    persist()
                else:
                    print_fn(f"unknown addressee {choice!r}: kept as null")
        pos += 1
        show = True

    summary.remaining = sum(1 for c in cases if c["id"] not in records)
    return summary


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="label_replay_cases.py",
        description=(
            "Label replay cases in the terminal: one key per case "
            "(s=speak n=silent e=either b=bad/skip u=undo q=quit & save). "
            "Answers are saved after every case and the run can be resumed."
        ),
    )
    parser.add_argument("cases", help="input JSONL produced by export_replay_cases.py")
    parser.add_argument("--out", default=None,
                        help="labeled output JSONL (default: <cases>.labeled.jsonl "
                             "next to the input)")
    parser.add_argument("--no-addressee", action="store_true",
                        help="do not ask for the addressee (second key)")
    parser.add_argument("--category", default=None,
                        help="value to store in 'category' of labeled cases "
                             "(default: leave unchanged)")
    parser.add_argument("--tz-offset-hours", type=float, default=8.0,
                        help="UTC offset used when showing 'now' (default: %(default)s)")
    return parser


def main(argv: Sequence[str] | None = None, *, input_fn: InputFn = default_input,
         print_fn: PrintFn = print) -> int:
    args = build_parser().parse_args(argv)
    in_path = Path(args.cases).expanduser()
    out_path = (Path(args.out).expanduser() if args.out
                else in_path.with_name(in_path.stem + ".labeled.jsonl"))
    try:
        if in_path.resolve() == out_path.resolve():
            raise LabelError("--out must differ from the input file "
                             "(it would overwrite the unlabeled cases)")
        cases = read_cases(in_path, "cases")
        if not cases:
            raise LabelError(f"no cases in {in_path}")
        print_fn(f"{len(cases)} cases from {in_path}; answers go to {out_path}")
        print_fn("keys: s=speak n=silent e=either b=bad/skip u=undo q=quit+save; "
                 "S/N = 'must'; then optional addressee b/m/g/n")
        summary = label_cases(
            cases, out_path, input_fn=input_fn, print_fn=print_fn,
            ask_addressee=not args.no_addressee, category=args.category,
            tz_offset_hours=args.tz_offset_hours,
        )
    except LabelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: could not write {out_path}: {exc}; the file keeps the "
              "answers saved before this one", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print_fn("\ninterrupted: every answer given so far is already saved")
        return 130
    print_fn(
        f"saved {out_path}: {summary.labeled} labeled + {summary.bad} bad this "
        f"session, {summary.already_done} from before, {summary.remaining} "
        f"remaining ({summary.stopped})"
    )
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if callable(_reconfigure):
            try:
                _reconfigure(errors="replace")
            except Exception:  # pragma: no cover - exotic stream types
                pass
    sys.exit(main())
