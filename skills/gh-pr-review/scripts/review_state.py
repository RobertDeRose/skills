#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Persist resumable gh-pr-review state under the repository Git directory."""

# ruff: noqa: S607

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import review_packet

SCHEMA_VERSION = 1
PHASES = {
    "collecting",
    "awaiting_selection",
    "awaiting_clarification",
    "implementing",
    "validating",
    "awaiting_ci",
    "replying",
    "awaiting_rereview_decision",
    "requesting_rereview",
    "awaiting_rereview",
    "complete",
    "blocked",
    "clarification-blocked",
}
TERMINAL_PHASES = {
    "complete",
    "blocked",
    "clarification-blocked",
    # Retired with the cycle limit; still terminal when loaded from older state.
    "cycle-limit-reached",
}


def git_dir() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-dir"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


def state_path() -> Path:
    return git_dir() / "dstack" / "gh-pr-review-state.json"


def load() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        message = "No active gh-pr-review state exists"
        raise SystemExit(message)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        message = f"Unsupported gh-pr-review state in {path}"
        raise SystemExit(message)
    return value


def atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            temporary_path.chmod(stat.S_IMODE(path.stat().st_mode))
        temporary_path.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def save(value: dict[str, Any]) -> None:
    atomic_write(state_path(), json.dumps(value, indent=2, sort_keys=True) + "\n")


def collect(snapshot: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    """Load before continuation; preserve assessments only for unchanged evidence."""
    pr = snapshot["pull_request"]
    value = load() if persist and state_path().exists() else {}
    if value and value.get("phase") not in TERMINAL_PHASES:
        if (value.get("pr_number"), value.get("head_sha")) != (
            pr["number"],
            pr["head_sha"],
        ):
            raise ValueError(
                "Active review PR/head changed. Use accept-head only after your own intentional push; otherwise reconcile state explicitly."
            )
        if value.get("pr_url") and value["pr_url"] != pr["url"]:
            raise ValueError("Active review belongs to another repository")
    else:
        value = {
            "schema_version": SCHEMA_VERSION,
            "pr_number": pr["number"],
            "head_sha": pr["head_sha"],
            "review_bot": None,
            "requested_after": None,
            "phase": "collecting",
            "ledger": [],
            "selected": [],
            "clarifications": {},
        }
    if "items" not in value and value["ledger"]:
        value["legacy_ledger"] = value["ledger"]
        value.update(ledger=[], history=[], selected=[], phase="collecting")
    previous = value.get("history", []) + value["ledger"]
    ledger = review_packet.make_ledger(snapshot["items"], previous)
    review_packet.validate_ledger(ledger, snapshot["items"])
    current_ids = {row["id"] for row in ledger}
    history = [row for row in previous if row["id"] not in current_ids]
    selected = [
        number
        for number in value["selected"]
        if any(
            row["number"] == number
            and (
                row["classification"] in ("Valid", "Requires clarification")
                or (
                    number in value.get("user_requested", [])
                    and row["disposition"] == "fix, user requested"
                    and row["classification"] in ("Invalid", "Context only")
                )
            )
            for row in ledger
        )
    ]
    value.update(
        pr_url=pr["url"],
        ledger=ledger,
        history=history,
        items=snapshot["items"],
        selected=selected,
        user_requested=[n for n in value.get("user_requested", []) if n in selected],
    )
    if any(row["classification"] == "Pending assessment" for row in ledger):
        value["phase"] = "collecting"
    if persist:
        save(value)
    return value


def parse_numbers(raw: str) -> list[int]:
    values: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        number = int(token)
        if number <= 0:
            message = "Ledger numbers must be positive"
            raise ValueError(message)
        values.append(number)
    return sorted(set(values))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--pr", type=int, required=True)
    init.add_argument("--head", required=True)

    requested_after = sub.add_parser("requested-after")
    requested_after.add_argument("value")

    phase = sub.add_parser("phase")
    phase.add_argument("value", choices=sorted(PHASES))

    selection = sub.add_parser("selection")
    selection.add_argument("numbers", help="Comma-separated ledger row numbers")
    selection.add_argument(
        "--user-requested",
        default="",
        help="Selected invalid/context rows explicitly requested by the user anyway",
    )

    ledger = sub.add_parser("ledger")
    ledger.add_argument(
        "path", type=Path, help="JSON file containing the current ledger array"
    )

    update = sub.add_parser(
        "update", help="Assess a ledger row without writing JSON or a parser"
    )
    update.add_argument("number", type=int)
    update.add_argument("--classification", choices=review_packet.CLASSIFICATIONS)
    update.add_argument("--disposition", choices=review_packet.DISPOSITIONS)
    update.add_argument("--summary")
    update.add_argument("--action")
    update.add_argument("--group")
    render = sub.add_parser(
        "render", help="Print the required Markdown disposition table"
    )
    render.add_argument(
        "--history",
        action="store_true",
        help="Include previously collected items for the final audit",
    )
    item = sub.add_parser(
        "item", help="Show a row's evidence and exact reply/resolve targets"
    )
    item.add_argument("number", type=int)
    sub.add_parser(
        "validate", help="Require complete coverage and assessment of every row"
    )
    sub.add_parser(
        "accept-head",
        help="Accept the current PR head after an intentional workflow push",
    )
    sub.add_parser("show", aliases=["status"])
    sub.add_parser("clear")
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "init":
        if args.pr <= 0 or not args.head.strip():
            message = "PR and head SHA must be valid"
            raise SystemExit(message)
        value = {
            "schema_version": SCHEMA_VERSION,
            "pr_number": args.pr,
            "head_sha": args.head.strip(),
            "review_bot": None,
            "requested_after": None,
            "phase": "collecting",
            "ledger": [],
            "selected": [],
            "clarifications": {},
        }
        save(value)
    elif args.command == "requested-after":
        value = load()
        value["requested_after"] = args.value.strip() or None
        save(value)
    elif args.command == "phase":
        value = load()
        value["phase"] = args.value
        save(value)
    elif args.command == "selection":
        value = load()
        review_packet.validate_ledger(
            value["ledger"], value.get("items", []), assessed=True
        )
        selected = parse_numbers(args.numbers)
        user_requested = parse_numbers(args.user_requested)
        rows = {row["number"]: row for row in value["ledger"]}
        if not set(user_requested).issubset(selected) or any(
            number not in rows
            or rows[number]["classification"] not in ("Invalid", "Context only")
            for number in user_requested
        ):
            raise ValueError(
                "User-requested overrides must be selected invalid/context rows; clarify unresolved scope first"
            )
        if any(
            number not in rows
            or (
                rows[number]["classification"]
                not in ("Valid", "Requires clarification")
                and number not in user_requested
            )
            for number in selected
        ):
            raise ValueError(
                "Select existing valid/clarification rows, or use --user-requested for explicit user changes"
            )
        for number in user_requested:
            rows[number]["disposition"] = "fix, user requested"
        value["selected"] = selected
        value["user_requested"] = user_requested
        value["phase"] = (
            "awaiting_clarification"
            if any(
                rows[n]["classification"] == "Requires clarification" for n in selected
            )
            else "implementing"
        )
        save(value)
    elif args.command == "ledger":
        value = load()
        ledger = json.loads(args.path.read_text(encoding="utf-8"))
        if not isinstance(ledger, list):
            message = "Ledger JSON must contain an array"
            raise SystemExit(message)
        review_packet.validate_ledger(ledger, value.get("items", []), assessed=True)
        value["ledger"] = ledger
        save(value)
    elif args.command == "update":
        value = load()
        row = next(
            (row for row in value["ledger"] if row["number"] == args.number), None
        )
        if row is None:
            raise ValueError("Unknown ledger row number")
        for argument, field in (
            ("classification", "classification"),
            ("disposition", "disposition"),
            ("summary", "summary"),
            ("action", "proposed_action"),
            ("group", "group"),
        ):
            if getattr(args, argument) is not None:
                row[field] = getattr(args, argument)
        review_packet.validate_ledger(value["ledger"], value.get("items", []))
        save(value)
    elif args.command in ("render", "validate"):
        value = load()
        review_packet.validate_ledger(
            value["ledger"], value.get("items", []), assessed=args.command == "validate"
        )
        rows = value["ledger"]
        if args.command == "render" and args.history:
            rows = sorted(
                rows + value.get("history", []), key=lambda row: row["number"]
            )
        print(
            review_packet.render_ledger(rows)
            if args.command == "render"
            else "Ledger coverage and assessments complete"
        )
    elif args.command == "item":
        value = load()
        row = next(
            (row for row in value["ledger"] if row["number"] == args.number), None
        )
        if row is None:
            raise ValueError("Unknown current ledger row number")
        item = next(item for item in value["items"] if item["id"] == row["id"])
        print(json.dumps(item, indent=2))
    elif args.command == "accept-head":
        value = load()
        pr = json.loads(
            subprocess.run(
                ["gh", "pr", "view", "--json", "number,url,headRefOid"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        if pr["number"] != value["pr_number"] or (
            value.get("pr_url") and pr["url"] != value["pr_url"]
        ):
            raise ValueError("Cannot accept the head of a different PR")
        value["head_sha"] = pr["headRefOid"]
        save(value)
    elif args.command in ("show", "status"):
        value = load()
        print(json.dumps(value, indent=2, sort_keys=True))
    elif args.command == "clear":
        path = state_path()
        if path.exists():
            value = load()
            if value.get("phase") not in TERMINAL_PHASES:
                message = "Refusing to clear non-terminal gh-pr-review state"
                raise SystemExit(message)
            path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
