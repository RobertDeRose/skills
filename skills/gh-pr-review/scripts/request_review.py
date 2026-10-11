#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Find, request, and await the review bot most recently used on the current PR."""

# ruff: noqa: S607

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

import fetch_comments
import review_state

# Supported review bots: author logins, GitHub App slugs, and the trigger command.
BOTS: dict[str, dict[str, Any]] = {
    "copilot": {
        "logins": {
            "copilot-pull-request-reviewer",
            "copilot-pull-request-reviewer[bot]",
        },
        "apps": set(),
    },
    "coderabbit": {
        "logins": {"coderabbitai", "coderabbitai[bot]"},
        "apps": {"coderabbitai"},
    },
    "greptile": {
        "logins": {"greptile-apps", "greptile-apps[bot]"},
        "apps": {"greptile-apps"},
    },
}


# Human comments that explicitly ask a bot to review.
MENTIONS = {
    "coderabbit": re.compile(r"(?<![\w.])@coderabbitai\b", re.IGNORECASE),
    "greptile": re.compile(r"(?<![\w.])@greptile(?:ai)?\b", re.IGNORECASE),
}
COPILOT_REVIEWERS = {"copilot", *BOTS["copilot"]["logins"]}


def bot_name(value: str) -> str:
    """Normalize a user-supplied bot name, accepting logins and @mentions."""
    key = value.strip().lstrip("@").lower().replace(" ", "").replace("-", "")
    key = key.removesuffix("[bot]").removesuffix("ai").removesuffix("apps")
    if key in BOTS:
        return key
    message = f"unsupported review bot {value!r}; choose from {', '.join(sorted(BOTS))}"
    raise argparse.ArgumentTypeError(message)


def bot_for_login(login: str | None) -> str | None:
    login = (login or "").lower()
    return next((name for name, bot in BOTS.items() if login in bot["logins"]), None)


def _login(node: dict[str, Any], key: str = "user") -> str | None:
    return (node.get(key) or {}).get("login")


def pr_activity(
    reviews: Iterable[dict[str, Any]],
    issue_comments: Iterable[dict[str, Any]],
    review_comments: Iterable[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Return (bot, timestamp) pairs for every known review-bot action on the PR."""
    activity = []
    for review in reviews:
        if (bot := bot_for_login(_login(review))) and review.get("submitted_at"):
            activity.append((bot, review["submitted_at"]))
    for comment in [*issue_comments, *review_comments]:
        if bot := bot_for_login(_login(comment)):
            activity.append((bot, comment.get("updated_at") or comment["created_at"]))
    return activity


def explicit_requests(
    issue_comments: Iterable[dict[str, Any]], timeline: Iterable[dict[str, Any]]
) -> list[tuple[str, str]]:
    """Return (bot, timestamp) pairs for reviews someone explicitly asked for.

    Counts human comments mentioning a bot and Copilot reviewer requests. Bot
    comments are ignored because bots quote their own trigger commands.
    """
    requests = []
    for comment in issue_comments:
        if (
            bot_for_login(_login(comment))
            or (comment.get("user") or {}).get("type") == "Bot"
        ):
            continue
        for bot, pattern in MENTIONS.items():
            if pattern.search(comment.get("body") or ""):
                requests.append((bot, comment["created_at"]))
    for event in timeline:
        reviewer = (_login(event, "requested_reviewer") or "").lower()
        if event.get("event") == "review_requested" and reviewer in COPILOT_REVIEWERS:
            requests.append(("copilot", event["created_at"]))
    return requests


def last_used_bot(
    activity: Iterable[tuple[str, str]],
    recorded: tuple[str, str] | None = None,
    requests: Iterable[tuple[str, str]] = (),
) -> str | None:
    """Pick the most recently requested bot, else the bot with the newest activity.

    Explicit requests (including one recorded in workflow state) win, because
    automatic reviews and summary edits do not show which bot the user chose.
    """
    chosen = [*requests, *([recorded] if recorded and all(recorded) else [])]
    candidates = chosen or list(activity)
    return max(candidates, key=lambda pair: pair[1])[0] if candidates else None


def completion_signal(
    bot: str,
    after: str,
    reviews: Iterable[dict[str, Any]],
    issue_comments: Iterable[dict[str, Any]],
    statuses: Iterable[dict[str, Any]],
    check_runs: Iterable[dict[str, Any]],
) -> str | None:
    """Describe the first evidence that `bot` finished a review requested at `after`.

    A submitted review, a finished commit status or check run from the bot counts.
    Greptile may update its summary comment instead, so its comment edits count too.
    CodeRabbit's trigger acknowledgement is a comment, so its comments do not.
    """
    for review in reviews:
        if (
            bot_for_login(_login(review)) == bot
            and (review.get("submitted_at") or "") >= after
        ):
            return f"review submitted at {review['submitted_at']}"
    for status in statuses:
        if (
            bot_for_login(_login(status, "creator")) == bot
            and status.get("state") != "pending"
            and (status.get("created_at") or "") >= after
        ):
            return f"status {status.get('context')!r} {status['state']}: {status.get('description')}"
    for run in check_runs:
        if (
            (run.get("app") or {}).get("slug") in BOTS[bot]["apps"]
            and run.get("status") == "completed"
            and (run.get("completed_at") or "") >= after
        ):
            return f"check run {run.get('name')!r} {run.get('conclusion')}"
    if bot == "greptile":
        for comment in issue_comments:
            if (
                bot_for_login(_login(comment)) == bot
                and (comment.get("updated_at") or "") >= after
            ):
                return f"summary comment updated at {comment['updated_at']}"
    return None


def _api_list(path: str, key: str | None = None) -> list[dict[str, Any]]:
    pages = json.loads(
        fetch_comments._run(["gh", "api", "--paginate", "--slurp", path])
    )
    return [item for page in pages for item in (page[key] if key else page)]


def _pr() -> tuple[str, int, str]:
    owner, repo, number = fetch_comments.get_current_pr_ref()
    head = fetch_comments.gh_pr_view_json("headRefOid")["headRefOid"]
    return f"repos/{owner}/{repo}", number, head


def detect(base: str, number: int) -> str | None:
    issue_comments = _api_list(f"{base}/issues/{number}/comments")
    activity = pr_activity(
        _api_list(f"{base}/pulls/{number}/reviews"),
        issue_comments,
        _api_list(f"{base}/pulls/{number}/comments"),
    )
    requests = explicit_requests(
        issue_comments, _api_list(f"{base}/issues/{number}/timeline")
    )
    recorded = None
    if review_state.state_path().exists():
        value = review_state.load()
        if value.get("pr_number") == number:
            recorded = (value.get("review_bot"), value.get("requested_after"))
    return last_used_bot(activity, recorded, requests)


def trigger(bot: str, number: int) -> None:
    if bot == "copilot":
        fetch_comments._run(
            ["gh", "pr", "edit", str(number), "--add-reviewer", "@copilot"]
        )
    else:
        body = {"coderabbit": "@coderabbitai review", "greptile": "@greptileai"}[bot]
        fetch_comments._run(["gh", "pr", "comment", str(number), "--body", body])


def wait(base: str, number: int, head: str, timeout: int, interval: int) -> int:
    value = review_state.load()
    bot, after = value.get("review_bot"), value.get("requested_after")
    if bot not in BOTS or not after:
        raise SystemExit(
            "No recorded review request; run `request_review.py request` first"
        )
    print(
        f"Waiting for {bot} review on PR #{number} (requested {after})...", flush=True
    )
    deadline = time.monotonic() + timeout
    while True:
        signal = completion_signal(
            bot,
            after,
            _api_list(f"{base}/pulls/{number}/reviews"),
            _api_list(f"{base}/issues/{number}/comments"),
            _api_list(f"{base}/commits/{head}/statuses"),
            _api_list(f"{base}/commits/{head}/check-runs", "check_runs"),
        )
        if signal:
            print(f"{bot} finished: {signal}")
            return 0
        if time.monotonic() >= deadline:
            print(
                f"{bot} did not finish a review within {timeout} seconds",
                file=sys.stderr,
            )
            return 1
        time.sleep(interval)


def main(argv: Sequence[str] | None = None) -> int:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("last", help="Print the review bot most recently used on this PR")
    request = sub.add_parser("request", help="Trigger a review and record it in state")
    request.add_argument(
        "--bot",
        type=bot_name,
        help="Override the detected bot: copilot, coderabbit, or greptile",
    )
    waiter = sub.add_parser(
        "wait", help="Block until the recorded review request finishes"
    )
    waiter.add_argument("--timeout", type=int, default=1800)
    waiter.add_argument("--interval", type=int, default=15)
    args = root.parse_args(argv)

    base, number, head = _pr()
    if args.command == "wait":
        return wait(base, number, head, args.timeout, args.interval)
    bot = getattr(args, "bot", None) or detect(base, number)
    if bot is None:
        print(
            "No Copilot, CodeRabbit, or Greptile activity found on this PR",
            file=sys.stderr,
        )
        return 2
    if args.command == "request":
        requested_after = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        trigger(bot, number)
        value = review_state.load()
        value.update(
            review_bot=bot, requested_after=requested_after, phase="awaiting_rereview"
        )
        review_state.save(value)
    print(bot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
