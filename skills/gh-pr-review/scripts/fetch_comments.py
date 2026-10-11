#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.13"
# dependencies = [
# ]
# ///
# ruff: noqa: EM101, S603

"""
Collect every author's PR comments, reviews, and unresolved inline threads,
including outdated threads and summary-only findings from supported providers.
Unknown formats remain inspection-required inputs, never silently empty reviews.
SonarQube CLI enrichment is optional and does not control comment inclusion.

Requires:
  - `gh auth login` already set up
  - current branch has an associated (open) PR

Usage (Python 3.13+; standard library only, no package cache needed):
  python3 <skill-dir>/scripts/fetch_comments.py
  python3 <skill-dir>/scripts/fetch_comments.py --format json

Alternatively, use `uv run` with the same script and arguments.

The default prints an agent-ready Markdown packet and saves snapshot.json,
ledger.json, and packet.md under the worktree Git directory. No GitHub writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import review_packet
import review_state

QUERY = """\
query(
  $owner: String!,
  $repo: String!,
  $number: Int!,
  $commentsCursor: String,
  $reviewsCursor: String,
  $threadsCursor: String,
  $includeComments: Boolean!,
  $includeReviews: Boolean!,
  $includeThreads: Boolean!
) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      number
      url
      title
      state
      headRefOid
      baseRefName

      # Top-level "Conversation" comments (issue comments on the PR)
      comments(first: 100, after: $commentsCursor) @include(if: $includeComments) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          url
          databaseId
          body
          createdAt
          updatedAt
          author { __typename login }
        }
      }

      # Review submissions (Approve / Request changes / Comment), with body if present
      reviews(first: 100, after: $reviewsCursor) @include(if: $includeReviews) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          state
          body
          submittedAt
          url
          commit { oid }
          author { __typename login }
        }
      }

      # Inline review threads (grouped), includes resolved state
      reviewThreads(first: 100, after: $threadsCursor) @include(if: $includeThreads) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          diffSide
          startLine
          startDiffSide
          originalLine
          originalStartLine
          resolvedBy { login }
          comments(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              url
              databaseId
              pullRequestReview { id commit { oid } }
              body
              createdAt
              updatedAt
              author { __typename login }
            }
          }
        }
      }
    }
  }
}
"""

THREAD_COMMENTS_QUERY = """\
query($threadId: ID!, $commentsCursor: String) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $commentsCursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          url
          databaseId
          pullRequestReview { id commit { oid } }
          body
          createdAt
          updatedAt
          author { __typename login }
        }
      }
    }
  }
}
"""

# Provider identities select parsers only; they never filter collected authors.
MAX_BODY_CHARS = 20_000
MAX_DIAGNOSTIC_CHARS = 2_000
SONAR_PAGE_SIZE = 500
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SONAR_URL = re.compile(r"https://(?:www\.)?sonarcloud\.io/[^\s<>()]+", re.IGNORECASE)
SONAR_PROJECT_KEY = re.compile(r"[A-Za-z0-9_.:-]+")
ALLOWED_COMMANDS = {
    ("gh", "auth"),
    ("gh", "pr"),
    ("gh", "api"),
    ("sonar", "list"),
}


SONAR_BOT_LOGINS = {
    "sonarqube",
    "sonarcloud",
    "sonarcloud[bot]",
    "sonarqubecloud",
    "sonarqubecloud[bot]",
}
COPILOT_REVIEW_LOGINS = {
    "copilot-pull-request-reviewer",
    "copilot-pull-request-reviewer[bot]",
}
ZERO_WIDTH_CHARS = re.compile(r"[\u200b\u200c\u200d\ufeff]")
FINDING_LOCATION = re.compile(
    r"(?m)^\s*(?:\*\*)?`?([^`\n*]+?)`?\s*:\s*(\d+)(?:-\d+)?(?:`|\*\*)?\s*$"
)
FLAT_FINDING_MARKER = re.compile(
    r"(?im)^\s*\*{0,2}(Previously missed|Suppressed comments)\s*\(\d+\).*?$"
)
FILES_REVIEWED_MARKER = re.compile(r"(?im)^\s*[-*]\s+\*{0,2}Files reviewed\b.*?$")
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
CODERABBIT_LOGINS = {"coderabbitai", "coderabbitai[bot]"}
GREPTILE_LOGINS = {"greptile-apps", "greptile-apps[bot]"}


def _author_login(node: dict[str, Any]) -> str:
    author = review_packet.author_info(node.get("author"))
    return str(author.get("login", "")).lower()


def _is_sonar_bot(node: dict[str, Any]) -> bool:
    return _author_login(node) in SONAR_BOT_LOGINS


def _is_copilot_review(node: dict[str, Any]) -> bool:
    return _author_login(node) in COPILOT_REVIEW_LOGINS


def _sonar_cli_available() -> bool:
    return shutil.which("sonar") is not None


def _sonar_project_keys(node: dict[str, Any]) -> set[str]:
    """Extract safe SonarCloud project keys from links in a Sonar bot comment."""
    if not _is_sonar_bot(node):
        return set()
    keys: set[str] = set()
    for raw_url in SONAR_URL.findall(str(node.get("body") or "")):
        try:
            query = parse_qs(urlsplit(raw_url.rstrip(".,;")).query)
        except ValueError:
            continue
        for parameter in ("id", "project"):
            for value in query.get(parameter, []):
                if SONAR_PROJECT_KEY.fullmatch(value):
                    keys.add(value)
    return keys


def _clean_external_text(
    value: object, *, limit: int = MAX_BODY_CHARS
) -> tuple[str, bool]:
    text = CONTROL_CHARS.sub("", str(value or ""))
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n[truncated by dstack review collector]", True


def _normalize_external_node(
    node: dict[str, Any], *, source_type: str
) -> dict[str, Any]:
    normalized = dict(node)
    raw_body = str(normalized.get("body") or "")
    body, truncated = _clean_external_text(raw_body, limit=len(raw_body))
    normalized["body"] = body
    normalized["body_truncated"] = truncated
    normalized["trust"] = "untrusted_external_content"
    normalized["source_type"] = source_type
    return normalized


class _ReviewDetailsParser(HTMLParser):
    """Extract Copilot's nested review-summary details without trusting HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.roots: list[dict[str, Any]] = []
        self.stack: list[dict[str, Any]] = []
        self.in_summary = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "details":
            node: dict[str, Any] = {
                "summary": [],
                "body": [],
                "children": [],
                "links": [],
            }
            if self.stack:
                self.stack[-1]["children"].append(node)
            else:
                self.roots.append(node)
            self.stack.append(node)
        elif tag == "summary" and self.stack:
            self.in_summary = True
        elif tag == "a" and self.stack:
            href = dict(attrs).get("href")
            if href:
                self.stack[-1]["links"].append(str(href))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "summary":
            self.in_summary = False
        elif tag == "details" and self.stack:
            self.stack.pop()
            self.in_summary = False

    def handle_data(self, data: str) -> None:
        if not self.stack:
            return
        key = "summary" if self.in_summary else "body"
        self.stack[-1][key].append(data)


def _compact_text(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _body_text(parts: list[str]) -> str:
    return re.sub(r"[ \t]+\n", "\n", "".join(parts)).strip()


def _walk_details(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for node in nodes:
        result.append(node)
        result.extend(_walk_details(node["children"]))
    return result


def _finding_location(text: str) -> tuple[str | None, int | None]:
    normalized = ZERO_WIDTH_CHARS.sub("", text)
    match = FINDING_LOCATION.search(normalized)
    if not match:
        return None, None
    return match.group(1).strip(), int(match.group(2))


def _finding_title(text: str, path: str | None, line: int | None) -> str:
    for raw_line in text.splitlines():
        line_text = raw_line.strip().strip("*- ")
        if (
            line_text
            and " | " not in line_text
            and not FINDING_LOCATION.fullmatch(line_text)
        ):
            return line_text[:300]
    location = f"{path}:{line}" if path and line is not None else "unknown location"
    return f"Previously missed Copilot finding at {location}"


def _make_review_finding(
    review: dict[str, Any],
    *,
    category: str,
    title: str,
    body: str,
    discussion_url: str | None = None,
) -> dict[str, Any]:
    body, truncated = _clean_external_text(body, limit=len(body))
    path, line = _finding_location(body)
    identity = json.dumps(
        [review.get("id"), category, title, body, discussion_url], ensure_ascii=False
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    finding = {
        "id": f"{review.get('id', 'review')}:finding:{digest}",
        "review_id": review.get("id"),
        "source_id": review.get("id"),
        "url": review.get("url"),
        "commit": review.get("commit"),
        "submittedAt": review.get("submittedAt") or review.get("updatedAt"),
        "author": review.get("author"),
        "category": category,
        "title": title,
        "body": body,
        "body_truncated": truncated,
        "path": path,
        "line": line,
        "discussion_url": discussion_url,
        "resolvable": False,
        "trust": "untrusted_external_content",
        "source_type": "summary_finding",
    }
    return finding


def _extract_flat_previously_missed(
    text: str, review: dict[str, Any]
) -> list[dict[str, Any]]:
    marker = FLAT_FINDING_MARKER.search(text)
    if not marker:
        return []
    section = text[marker.end() :]
    end = FILES_REVIEWED_MARKER.search(section)
    if end:
        section = section[: end.start()]
    locations = list(FINDING_LOCATION.finditer(ZERO_WIDTH_CHARS.sub("", section)))
    findings = []
    for index, location in enumerate(locations):
        stop = (
            locations[index + 1].start() if index + 1 < len(locations) else len(section)
        )
        body = section[location.start() : stop].strip()
        path = location.group(1).strip()
        line = int(location.group(2))
        title = _finding_title(section[location.end() : stop], path, line)
        findings.append(
            _make_review_finding(
                review,
                category="suppressed"
                if marker.group(1).lower().startswith("suppressed")
                else "previously_missed",
                title=title,
                body=body,
            )
        )
    return findings


def _extract_copilot_findings(review: dict[str, Any]) -> list[dict[str, Any]]:
    if not _is_copilot_review(review):
        return []
    body = str(review.get("body") or "")
    parser = _ReviewDetailsParser()
    try:
        parser.feed(body)
        parser.close()
    except (AssertionError, ValueError):
        # The fallback still handles the older flat markdown summary format.
        parser.roots = []

    findings: list[dict[str, Any]] = []
    for node in _walk_details(parser.roots):
        summary = _compact_text(node["summary"])
        category = next(
            (
                value
                for label, value in (
                    ("previously missed", "previously_missed"),
                    ("suppressed comments", "suppressed"),
                )
                if label in summary.lower()
            ),
            None,
        )
        if category is None:
            continue
        children = node["children"]
        if children:
            for child in children:
                child_body = _body_text(child["body"])
                if not child_body:
                    continue
                title = _compact_text(child["summary"]) or _finding_title(
                    child_body, *_finding_location(child_body)
                )
                links = child.get("links") or []
                findings.append(
                    _make_review_finding(
                        review,
                        category=category,
                        title=title,
                        body=child_body,
                        discussion_url=next(
                            (link for link in links if "discussion_" in link), None
                        ),
                    )
                )
        findings.extend(
            _extract_flat_previously_missed(_body_text(node["body"]), review)
        )

    if not findings:
        findings.extend(_extract_flat_previously_missed(body, review))

    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for finding in findings:
        key = (
            finding["review_id"],
            finding["category"],
            finding["path"],
            finding["line"],
            finding["title"],
            finding["body"],
        )
        unique[key] = finding
    return list(unique.values())


def _extract_summary_findings(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Provider adapters split known summaries; the original is always retained."""
    if _is_copilot_review(source):
        return _extract_copilot_findings(source)
    login = _author_login(source)
    body = str(source.get("body") or "")
    if login in GREPTILE_LOGINS:
        # Greptile includes a plain-text issue list in a fenced agent prompt.
        # Extract it as evidence, never as executable workflow instructions.
        issues = re.findall(
            r"(?ms)^### Issue \d+\s*\n(.*?)(?=^### Issue \d+|^---\s*$|^`{3,}|\Z)", body
        )
        references = list(
            dict.fromkeys(re.findall(r'https://[^\s<>"\\]+#discussion_r\d+', body))
        )
        findings = []
        for index, text in enumerate(issues):
            path, line = _finding_location(text)
            findings.append(
                _make_review_finding(
                    source,
                    category="greptile_finding",
                    title=_finding_title(text, path, line),
                    body=text.strip(),
                    discussion_url=references[index]
                    if index < len(references)
                    else None,
                )
            )
        return findings
    if login not in CODERABBIT_LOGINS:
        return []
    parser = _ReviewDetailsParser()
    try:
        parser.feed(body)
        parser.close()
    except (AssertionError, ValueError):
        return []
    findings = []

    def visit(nodes: list[dict[str, Any]], category: str | None = None) -> None:
        for node in nodes:
            summary = _compact_text(node["summary"])
            section = category
            if "nitpick comments" in summary.lower():
                section = "nitpick"
            elif "outside diff range" in summary.lower():
                section = "outside_diff"
            location = re.fullmatch(r"(.+?)\s*\(\d+\)", summary)
            if section and location and re.search(r"[/\\.]", location.group(1)):
                path = location.group(1).strip().strip("`")
                text = ZERO_WIDTH_CHARS.sub("", _body_text(node["body"]))
                markers = list(re.finditer(r"(?m)^\s*`(\d+)(?:-(\d+))?`\s*:", text))
                for index, marker in enumerate(markers):
                    stop = (
                        markers[index + 1].start()
                        if index + 1 < len(markers)
                        else len(text)
                    )
                    claim = text[marker.end() : stop].strip()
                    line = int(marker.group(1))
                    finding = _make_review_finding(
                        source,
                        category=section,
                        title=_finding_title(claim, path, line),
                        body=f"`{path}:{line}`\n\n{claim}",
                    )
                    finding["end_line"] = int(marker.group(2) or marker.group(1))
                    findings.append(finding)
            visit(node["children"], section)

    visit(parser.roots)
    return findings


def _summary_metadata(
    source: dict[str, Any], findings: list[dict[str, Any]]
) -> dict[str, Any]:
    login = _author_login(source)
    adapter = (
        "copilot"
        if login in COPILOT_REVIEW_LOGINS
        else "coderabbit"
        if login in CODERABBIT_LOGINS
        else "greptile"
        if login in GREPTILE_LOGINS
        else "generic"
    )
    return {
        "adapter": adapter,
        "finding_count": len(findings),
        "status": "empty"
        if not source.get("body")
        else "split_findings"
        if findings
        else "full_body_requires_inspection",
    }


def _validate_command(cmd: list[str]) -> None:
    if len(cmd) < 2 or tuple(cmd[:2]) not in ALLOWED_COMMANDS:
        msg = f"Unsupported command for review collector: {cmd[:2]!r}"
        raise RuntimeError(msg)
    if any("\n" in value or "\x00" in value for value in cmd):
        msg = "Command arguments contain prohibited control characters"
        raise RuntimeError(msg)


def _run(cmd: list[str], stdin: str | None = None) -> str:
    _validate_command(cmd)
    completed = subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        diagnostic, _ = _clean_external_text(
            completed.stderr, limit=MAX_DIAGNOSTIC_CHARS
        )
        msg = f"Command failed ({completed.returncode}): {' '.join(cmd[:3])}"
        if diagnostic.strip():
            msg += f"\n{diagnostic.strip()}"
        raise RuntimeError(msg)
    return completed.stdout


def _run_json(cmd: list[str], stdin: str | None = None) -> dict[str, Any]:
    out = _run(cmd, stdin=stdin)
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        msg = f"Failed to parse JSON from {' '.join(cmd[:3])}: {exc}"
        raise RuntimeError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"Expected a JSON object from {' '.join(cmd[:3])}"
        raise RuntimeError(msg)
    return payload


def _ensure_gh_authenticated() -> None:
    try:
        _run(["gh", "auth", "status"])
    except RuntimeError as error:
        msg = (
            f"GitHub CLI authentication check failed:\n{error}\n"
            "Check credentials and sandbox/network/keychain access. For permission restrictions, "
            "request the host's approved permissions and retry the same collector. "
            "Run `gh auth login` only if credentials are actually missing or invalid."
        )
        raise RuntimeError(msg) from None


def gh_pr_view_json(fields: str) -> dict[str, Any]:
    return _run_json(["gh", "pr", "view", "--json", fields])


def get_current_pr_ref() -> tuple[str, str, int]:
    # A PR belongs to the base repository, not necessarily the fork hosting its head.
    pr = gh_pr_view_json("number,url")
    parts = urlsplit(pr["url"]).path.strip("/").split("/")
    if len(parts) != 4 or parts[2] != "pull" or parts[3] != str(pr["number"]):
        raise RuntimeError("GitHub returned an invalid pull-request URL")
    return parts[0], parts[1], int(pr["number"])


def _collect_sonar_issues(number: int, project_keys: list[str]) -> list[dict[str, Any]]:
    if not project_keys:
        raise RuntimeError(
            "could not derive a SonarQube project key from the SonarCloud comment"
        )

    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for project in project_keys:
        page = 1
        while True:
            payload = _run_json(
                [
                    "sonar",
                    "list",
                    "issues",
                    "--project",
                    project,
                    "--pull-request",
                    str(number),
                    "--page-size",
                    str(SONAR_PAGE_SIZE),
                    "--page",
                    str(page),
                    "--format",
                    "json",
                ]
            )
            issues = payload.get("issues")
            if not isinstance(issues, list):
                raise RuntimeError(
                    "SonarQube CLI returned JSON without an issues array"
                )
            for issue in issues:
                if not isinstance(issue, dict):
                    raise RuntimeError("SonarQube CLI returned a non-object issue")
                key = str(issue.get("key") or "")
                if not key:
                    raise RuntimeError("SonarQube CLI returned an issue without a key")
                identity = (project, key)
                if identity in seen:
                    continue
                seen.add(identity)
                item = dict(issue)
                message = str(item.get("message") or "")
                body, truncated = _clean_external_text(message, limit=len(message))
                item["body"] = body
                item["body_truncated"] = truncated
                item["project"] = project
                item["trust"] = "untrusted_external_content"
                item["source_type"] = "sonarqube_issue"
                normalized.append(item)

            paging = payload.get("paging")
            total = paging.get("total") if isinstance(paging, dict) else None
            has_next_page = (
                isinstance(paging, dict) and paging.get("hasNextPage") is True
            )
            if not has_next_page and not (
                isinstance(total, int) and page * SONAR_PAGE_SIZE < total
            ):
                break
            page += 1
    return normalized


def gh_api_graphql(
    owner: str,
    repo: str,
    number: int,
    comments_cursor: str | None = None,
    reviews_cursor: str | None = None,
    threads_cursor: str | None = None,
    *,
    include_comments: bool = True,
    include_reviews: bool = True,
    include_threads: bool = True,
) -> dict[str, Any]:
    cmd = [
        "gh",
        "api",
        "graphql",
        "-F",
        "query=@-",
        "-F",
        f"owner={owner}",
        "-F",
        f"repo={repo}",
        "-F",
        f"number={number}",
        "-F",
        f"includeComments={'true' if include_comments else 'false'}",
        "-F",
        f"includeReviews={'true' if include_reviews else 'false'}",
        "-F",
        f"includeThreads={'true' if include_threads else 'false'}",
    ]
    if comments_cursor:
        cmd += ["-F", f"commentsCursor={comments_cursor}"]
    if reviews_cursor:
        cmd += ["-F", f"reviewsCursor={reviews_cursor}"]
    if threads_cursor:
        cmd += ["-F", f"threadsCursor={threads_cursor}"]

    return _run_json(cmd, stdin=QUERY)


def gh_thread_comments(
    thread_id: str, comments_cursor: str | None = None
) -> dict[str, Any]:
    cmd = [
        "gh",
        "api",
        "graphql",
        "-F",
        "query=@-",
        "-F",
        f"threadId={thread_id}",
    ]
    if comments_cursor:
        cmd += ["-F", f"commentsCursor={comments_cursor}"]

    payload = _run_json(cmd, stdin=THREAD_COMMENTS_QUERY)
    if payload.get("errors"):
        msg = f"GitHub GraphQL errors:\n{json.dumps(payload['errors'], indent=2)}"
        raise RuntimeError(msg)
    try:
        comments = payload["data"]["node"]["comments"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            "GitHub GraphQL response omitted review-thread comments"
        ) from exc
    if not isinstance(comments, dict):
        raise RuntimeError("GitHub GraphQL returned invalid review-thread comments")
    return comments


def _next_cursor(connection: dict[str, Any]) -> str | None:
    page_info = connection.get("pageInfo")
    if not isinstance(page_info, dict):
        raise RuntimeError("GitHub GraphQL response omitted pagination information")
    if not page_info.get("hasNextPage"):
        return None
    cursor = page_info.get("endCursor")
    if not isinstance(cursor, str) or not cursor:
        raise RuntimeError(
            "GitHub GraphQL response has a next page without an end cursor"
        )
    return cursor


def _fetch_thread_comment_nodes(thread: dict[str, Any]) -> list[dict[str, Any]]:
    connection = thread.get("comments")
    if not isinstance(connection, dict):
        raise RuntimeError("GitHub GraphQL response omitted review-thread comments")
    nodes = list(connection.get("nodes") or [])
    cursor = _next_cursor(connection)
    if not cursor:
        return nodes
    thread_id = thread.get("id")
    if not isinstance(thread_id, str) or not thread_id:
        raise RuntimeError("GitHub GraphQL response omitted the review-thread ID")
    while cursor:
        connection = gh_thread_comments(thread_id, cursor)
        nodes.extend(connection.get("nodes") or [])
        cursor = _next_cursor(connection)
    return nodes


def fetch_all(owner: str, repo: str, number: int) -> dict[str, Any]:
    conversation_comments: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    review_findings: list[dict[str, Any]] = []
    review_threads: list[dict[str, Any]] = []
    sonar_issues: list[dict[str, Any]] = []
    sonar_cli_available = _sonar_cli_available()
    sonar_comment_detected = False
    sonar_project_keys: set[str] = set()

    comments_cursor: str | None = None
    reviews_cursor: str | None = None
    threads_cursor: str | None = None
    include_comments = True
    include_reviews = True
    include_threads = True

    pr_meta: dict[str, Any] | None = None

    while include_comments or include_reviews or include_threads:
        payload = gh_api_graphql(
            owner=owner,
            repo=repo,
            number=number,
            comments_cursor=comments_cursor,
            reviews_cursor=reviews_cursor,
            threads_cursor=threads_cursor,
            include_comments=include_comments,
            include_reviews=include_reviews,
            include_threads=include_threads,
        )

        if payload.get("errors"):
            msg = f"GitHub GraphQL errors:\n{json.dumps(payload['errors'], indent=2)}"
            raise RuntimeError(msg)

        pr = payload["data"]["repository"]["pullRequest"]
        if pr_meta is None:
            pr_meta = {
                "number": pr["number"],
                "url": pr["url"],
                "title": pr["title"],
                "state": pr["state"],
                "head_sha": pr.get("headRefOid"),
                "base_branch": pr.get("baseRefName"),
                "owner": owner,
                "repo": repo,
            }

        if include_comments:
            c = pr.get("comments")
            if not isinstance(c, dict):
                raise RuntimeError(
                    "GitHub GraphQL response omitted pull-request comments"
                )
            # Collect every author; enrichment availability never controls inclusion.
            for comment in c.get("nodes") or []:
                if _is_sonar_bot(comment):
                    sonar_comment_detected = True
                    sonar_project_keys.update(_sonar_project_keys(comment))
                source_type = (
                    "sonarqube_comment"
                    if _is_sonar_bot(comment)
                    else "pull_request_comment"
                )
                normalized_comment = _normalize_external_node(
                    comment, source_type=source_type
                )
                findings = _extract_summary_findings(normalized_comment)
                normalized_comment["summary_extraction"] = _summary_metadata(
                    normalized_comment, findings
                )
                conversation_comments.append(normalized_comment)
                review_findings.extend(findings)
            comments_cursor = _next_cursor(c)
            include_comments = comments_cursor is not None

        if include_reviews:
            r = pr.get("reviews")
            if not isinstance(r, dict):
                raise RuntimeError(
                    "GitHub GraphQL response omitted pull-request reviews"
                )
            # Review submissions, including summary-only findings.
            for review in r.get("nodes") or []:
                if _is_sonar_bot(review):
                    sonar_comment_detected = True
                    sonar_project_keys.update(_sonar_project_keys(review))
                source_type = (
                    "sonarqube_review" if _is_sonar_bot(review) else "review_submission"
                )
                normalized_review = _normalize_external_node(
                    review, source_type=source_type
                )
                findings = _extract_summary_findings(normalized_review)
                normalized_review["summary_extraction"] = _summary_metadata(
                    normalized_review, findings
                )
                reviews.append(normalized_review)
                review_findings.extend(findings)
            reviews_cursor = _next_cursor(r)
            include_reviews = reviews_cursor is not None

        if include_threads:
            t = pr.get("reviewThreads")
            if not isinstance(t, dict):
                raise RuntimeError("GitHub GraphQL response omitted review threads")
            # Keep unresolved threads, even when their original diff is outdated.
            for thread in t.get("nodes") or []:
                thread_comments = _fetch_thread_comment_nodes(thread)
                for message in thread_comments:
                    if _is_sonar_bot(message):
                        sonar_comment_detected = True
                        sonar_project_keys.update(_sonar_project_keys(message))
                if not thread.get("isResolved"):
                    filtered_comments = [
                        _normalize_external_node(
                            msg,
                            source_type=(
                                "sonarqube_inline_comment"
                                if _is_sonar_bot(msg)
                                else "inline_review_comment"
                            ),
                        )
                        for msg in thread_comments
                    ]

                    # Only keep the thread if it still contains valid comments.
                    if filtered_comments:
                        normalized_thread = dict(thread)
                        path_text, path_truncated = _clean_external_text(
                            normalized_thread.get("path"), limit=1_024
                        )
                        normalized_thread["path"] = path_text
                        normalized_thread["path_truncated"] = path_truncated
                        normalized_thread["trust"] = "untrusted_external_content"
                        normalized_thread["source_type"] = "inline_review_thread"
                        normalized_thread["comments"] = {"nodes": filtered_comments}
                        review_threads.append(normalized_thread)
            threads_cursor = _next_cursor(t)
            include_threads = threads_cursor is not None

    sonar_status: dict[str, Any]
    if not sonar_comment_detected:
        sonar_status = {
            "status": "not_detected",
            "cli_available": sonar_cli_available,
            "issue_count": 0,
            "projects": [],
        }
    elif not sonar_cli_available:
        sonar_status = {
            "status": "skipped",
            "cli_available": False,
            "issue_count": 0,
            "projects": sorted(sonar_project_keys),
            "reason": "sonar CLI is not installed",
        }
    else:
        try:
            sonar_issues = _collect_sonar_issues(number, sorted(sonar_project_keys))
        except RuntimeError as error:
            detail, _ = _clean_external_text(str(error), limit=MAX_DIAGNOSTIC_CHARS)
            sonar_status = {
                "status": "error",
                "cli_available": True,
                "issue_count": 0,
                "projects": sorted(sonar_project_keys),
                "error": detail,
            }
        else:
            sonar_status = {
                "status": "loaded",
                "cli_available": True,
                "issue_count": len(sonar_issues),
                "projects": sorted(sonar_project_keys),
            }

    assert pr_meta is not None
    return {
        "schema_version": 2,
        "trust": "untrusted_external_content",
        "pull_request": pr_meta,
        "conversation_comments": conversation_comments,
        "reviews": reviews,
        "review_findings": review_findings,
        "review_threads": review_threads,
        "sonarqube": sonar_status,
        "sonar_issues": sonar_issues,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("agent", "json"), default="agent")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Packet directory (default: worktree Git directory)",
    )
    parser.add_argument(
        "--no-state",
        action="store_true",
        help="Preview collection without loading or changing workflow state",
    )
    args = parser.parse_args()
    _ensure_gh_authenticated()
    owner, repo, number = get_current_pr_ref()
    result = fetch_all(owner, repo, number)
    result["items"] = review_packet.normalize_items(result)
    state = review_state.collect(result, persist=not args.no_state)
    result["workflow"] = {
        key: state.get(key) for key in ("phase", "review_bot", "requested_after")
    }
    result["ledger"] = state["ledger"]
    directory = args.output_dir or review_state.git_dir() / "dstack" / "gh-pr-review"
    directory.mkdir(parents=True, exist_ok=True)
    packet = review_packet.render_packet(result)
    for name, content in (
        ("snapshot.json", json.dumps(result, indent=2) + "\n"),
        ("ledger.json", json.dumps(state["ledger"], indent=2) + "\n"),
        ("packet.md", packet),
    ):
        review_state.atomic_write(directory / name, content)
    print(f"Review artifacts: {directory}", file=sys.stderr)
    print(json.dumps(result, indent=2) if args.format == "json" else packet)
    if result["sonarqube"]["status"] == "error":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
