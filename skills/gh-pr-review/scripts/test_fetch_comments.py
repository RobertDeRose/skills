from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("fetch_comments.py")
spec = importlib.util.spec_from_file_location("fetch_comments", SCRIPT)
assert spec and spec.loader
fetch_comments = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_comments)


class CopilotFindingTests(unittest.TestCase):
    def review(self, body: str, login: str = "copilot-pull-request-reviewer"):
        return fetch_comments._normalize_external_node(
            {
                "id": "review-1",
                "submittedAt": "2026-09-20T14:23:07Z",
                "author": {"login": login},
                "body": body,
            },
            source_type="review_submission",
        )

    def test_extracts_nested_previously_missed_findings(self):
        review = self.review(
            """
            <details open>
            <summary><strong>Previously missed (2)</strong></summary>
            <details>
            <summary>Chunked downloads can exceed the limit</summary>
            `cockpit-os-upgrade/build/root-dir/usr/lib/os-upgrade/upgrade_tasks.py:144`

            Enforce the limit while streaming.
            </details>
            <details>
            <summary>Recovery can race</summary>
            `cockpit-os-upgrade/src/UpgradeApp.tsx:423`

            Disable the action while pending.
            </details>
            </details>
            """,
        )

        findings = fetch_comments._extract_copilot_findings(review)

        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0]["category"], "previously_missed")
        self.assertEqual(
            findings[0]["path"],
            "cockpit-os-upgrade/build/root-dir/usr/lib/os-upgrade/upgrade_tasks.py",
        )
        self.assertEqual(findings[0]["line"], 144)
        self.assertIn("Enforce the limit", findings[0]["body"])
        self.assertFalse(findings[0]["resolvable"])

    def test_extracts_legacy_flat_previously_missed_findings(self):
        review = self.review(
            """
            <details>
            <summary>Review details</summary>
            **Previously missed (1)** — in code that has not changed.

            **cockpit-os-upgrade/src/App.tsx:281**
            * Reset the reboot state when the command fails.

            - **Files reviewed:** 3/4 changed files
            </details>
            """,
        )

        findings = fetch_comments._extract_copilot_findings(review)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["path"], "cockpit-os-upgrade/src/App.tsx")
        self.assertEqual(findings[0]["line"], 281)
        self.assertIn("Reset the reboot state", findings[0]["body"])

    def test_ignores_non_copilot_review_summaries(self):
        review = self.review(
            "<details><summary>Previously missed (1)</summary>bad</details>", "human"
        )
        self.assertEqual(fetch_comments._extract_copilot_findings(review), [])


class SummaryAdapterTests(unittest.TestCase):
    review = CopilotFindingTests.review

    def test_extracts_suppressed_copilot_findings(self):
        review = self.review(
            "<details><summary>Suppressed comments (1)</summary><details>"
            "<summary>Missing bound</summary>`src/example.py:12`\nApply a size limit.</details></details>"
        )
        findings = fetch_comments._extract_summary_findings(review)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["category"], "suppressed")

    def test_extracts_flat_suppressed_findings(self):
        review = self.review(
            "**Suppressed comments (1)**\n`src/example.py:12`\nApply a size limit."
        )
        self.assertEqual(
            fetch_comments._extract_summary_findings(review)[0]["category"],
            "suppressed",
        )

    def test_coderabbit_nitpicks_and_outside_diff(self):
        review = self.review(
            """<details><summary>Nitpick comments (1)</summary>
          <details><summary>src/test.py (1)</summary>
          `6-10`: **Quality** | **Trivial**
          **Strengthen the test.**
          Assert the job has no condition.
          <details><summary>Prompt for AI Agents</summary>Do things.</details>
          </details></details>
          <details><summary>Outside diff range comments (1)</summary>
          <details><summary>src/app.py (1)</summary>`21`: **Reject hardlinks.**\nCheck st_nlink.</details></details>""",
            "coderabbitai[bot]",
        )
        findings = fetch_comments._extract_summary_findings(review)
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0]["path"], "src/test.py")
        self.assertEqual(findings[0]["end_line"], 10)
        self.assertEqual(findings[0]["title"], "Strengthen the test.")
        self.assertNotIn("Do things", findings[0]["body"])
        self.assertEqual(findings[1]["category"], "outside_diff")

    def test_greptile_issue_list_and_thread_reference(self):
        review = self.review(
            """<a href="https://github.com/o/r/pull/8#discussion_r123">Finding</a>
<details><summary>Fix with agent prompt</summary>
`````markdown
### Issue 1
src/app.py:423-431
Reject linked files.

---
Fix everything automatically.
`````
</details>""",
            "greptile-apps",
        )
        findings = fetch_comments._extract_summary_findings(review)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["line"], 423)
        self.assertEqual(
            findings[0]["discussion_url"],
            "https://github.com/o/r/pull/8#discussion_r123",
        )
        self.assertNotIn("automatically", findings[0]["body"])

    def test_string_provider_author_can_select_summary_adapter(self):
        review = self.review(
            "**Suppressed comments (1)**\n`src/example.py:12`\nApply a size limit."
        )
        review["author"] = "copilot-pull-request-reviewer"
        self.assertEqual(
            fetch_comments._extract_summary_findings(review)[0]["category"],
            "suppressed",
        )

    def test_unknown_summary_is_not_guessed(self):
        self.assertEqual(
            fetch_comments._extract_summary_findings(
                self.review("Fix this", "new-bot")
            ),
            [],
        )

    def test_distinct_same_length_findings_have_stable_unique_ids(self):
        review = self.review("")
        a = fetch_comments._make_review_finding(
            review, category="suppressed", title="A", body="src/a.py:12\nIssue A"
        )
        b = fetch_comments._make_review_finding(
            review, category="suppressed", title="B", body="src/b.py:12\nIssue B"
        )
        again = fetch_comments._make_review_finding(
            review, category="suppressed", title="A", body="src/a.py:12\nIssue A"
        )
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(a["id"], again["id"])

    def test_full_body_retained_and_tail_finding_extracted(self):
        body = (
            "x"
            * 25000
            + "\n<details><summary>Suppressed comments (1)</summary><details>"
            "<summary>Tail claim</summary>`src/a.py:12`\nCheck the tail.</details></details>"
        )
        review = self.review(body)
        self.assertFalse(review["body_truncated"])
        self.assertEqual(review["body"], body)
        self.assertEqual(len(fetch_comments._extract_summary_findings(review)), 1)


class CollectionTests(unittest.TestCase):
    def connection(self, nodes, cursor=None):
        return {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
        }

    def payload(self, *, comments=(), reviews=(), threads=(), comments_cursor=None):
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 8,
                        "url": "https://github.com/o/r/pull/8",
                        "title": "Test",
                        "state": "OPEN",
                        "headRefOid": "head",
                        "baseRefName": "dev",
                        "comments": self.connection(list(comments), comments_cursor),
                        "reviews": self.connection(list(reviews)),
                        "reviewThreads": self.connection(list(threads)),
                    }
                }
            }
        }

    def comment(self, login, identity="comment", body="Claim"):
        return {
            "id": identity,
            "body": body,
            "author": {"login": login, "__typename": "Bot"},
        }

    def thread(self, *, resolved=False, outdated=False, comments=None):
        return {
            "id": "thread",
            "isResolved": resolved,
            "isOutdated": outdated,
            "path": "src/a.py",
            "comments": self.connection(comments or [self.comment("new-bot")]),
        }

    def test_all_authors_and_outdated_unresolved_threads_retained_without_sonar_cli(
        self,
    ):
        payload = self.payload(
            comments=[
                self.comment("github-actions[bot]", "actions"),
                self.comment("sonarcloud[bot]", "sonar"),
                self.comment("new-bot", "unknown"),
            ],
            reviews=[self.comment("github-actions", "review")],
            threads=[self.thread(outdated=True)],
        )
        with (
            patch.object(fetch_comments, "gh_api_graphql", return_value=payload),
            patch.object(fetch_comments, "_sonar_cli_available", return_value=False),
        ):
            snapshot = fetch_comments.fetch_all("o", "r", 8)
        self.assertEqual(len(snapshot["conversation_comments"]), 3)
        self.assertEqual(len(snapshot["reviews"]), 1)
        self.assertTrue(snapshot["review_threads"][0]["isOutdated"])
        self.assertEqual(snapshot["sonarqube"]["status"], "skipped")

    def test_resolved_threads_are_excluded(self):
        with patch.object(
            fetch_comments,
            "gh_api_graphql",
            return_value=self.payload(threads=[self.thread(resolved=True)]),
        ):
            self.assertEqual(
                fetch_comments.fetch_all("o", "r", 8)["review_threads"], []
            )

    def test_comments_paginate_without_reloading_finished_connections(self):
        pages = [
            self.payload(comments=[self.comment("a", "a")], comments_cursor="next"),
            self.payload(comments=[self.comment("b", "b")]),
        ]
        with patch.object(fetch_comments, "gh_api_graphql", side_effect=pages) as api:
            snapshot = fetch_comments.fetch_all("o", "r", 8)
        self.assertEqual(len(snapshot["conversation_comments"]), 2)
        self.assertFalse(api.call_args.kwargs["include_reviews"])
        self.assertFalse(api.call_args.kwargs["include_threads"])
        self.assertEqual(api.call_args.kwargs["comments_cursor"], "next")

    def test_thread_replies_are_paginated(self):
        thread = self.thread()
        thread["comments"] = self.connection([self.comment("a", "first")], "next")
        with patch.object(
            fetch_comments,
            "gh_thread_comments",
            return_value=self.connection([self.comment("b", "second")]),
        ):
            self.assertEqual(len(fetch_comments._fetch_thread_comment_nodes(thread)), 2)

    def test_graphql_error_blocks_partial_collection(self):
        with patch.object(
            fetch_comments,
            "gh_api_graphql",
            return_value={"errors": [{"message": "Denied"}]},
        ):
            with self.assertRaises(RuntimeError):
                fetch_comments.fetch_all("o", "r", 8)

    def test_sonar_error_is_explicit_and_comments_retained(self):
        payload = self.payload(
            comments=[
                self.comment(
                    "sonarcloud", body="https://sonarcloud.io/dashboard?id=o_r"
                )
            ]
        )
        with (
            patch.object(fetch_comments, "gh_api_graphql", return_value=payload),
            patch.object(fetch_comments, "_sonar_cli_available", return_value=True),
            patch.object(
                fetch_comments,
                "_collect_sonar_issues",
                side_effect=RuntimeError("Denied"),
            ),
        ):
            snapshot = fetch_comments.fetch_all("o", "r", 8)
        self.assertEqual(snapshot["sonarqube"]["status"], "error")
        self.assertEqual(len(snapshot["conversation_comments"]), 1)

    def test_sonar_cli_string_author_reaches_normalized_ledger(self):
        payload = self.payload(
            comments=[
                self.comment(
                    "sonarcloud",
                    body="https://sonarcloud.io/dashboard?id=o_r",
                )
            ]
        )
        issues = {
            "issues": [
                {
                    "key": "issue-1",
                    "author": "committer@example.com",
                    "message": "Check this claim",
                    "component": "o_r:src/a.py",
                    "line": 12,
                }
            ]
        }
        with (
            patch.object(fetch_comments, "gh_api_graphql", return_value=payload),
            patch.object(fetch_comments, "_sonar_cli_available", return_value=True),
            patch.object(fetch_comments, "_run_json", return_value=issues),
        ):
            result = fetch_comments.fetch_all("o", "r", 8)
        self.assertEqual(result["sonarqube"]["status"], "loaded")
        self.assertEqual(result["sonar_issues"][0]["author"], "committer@example.com")
        result["items"] = fetch_comments.review_packet.normalize_items(result)
        result["ledger"] = fetch_comments.review_packet.make_ledger(result["items"])
        self.assertEqual(len(result["ledger"]), 2)
        self.assertIn(
            "committer@example.com", fetch_comments.review_packet.render_packet(result)
        )

    def test_auth_failure_keeps_original_permission_diagnostic(self):
        with patch.object(
            fetch_comments,
            "_run",
            side_effect=RuntimeError("keychain: Operation not permitted"),
        ):
            with self.assertRaises(RuntimeError) as error:
                fetch_comments._ensure_gh_authenticated()
        message = str(error.exception)
        self.assertIn("keychain: Operation not permitted", message)
        self.assertIn("approved permissions", message)
        self.assertIn("only if credentials are actually missing or invalid", message)

    def test_auth_failure_keeps_original_network_diagnostic(self):
        with patch.object(
            fetch_comments,
            "_run",
            side_effect=RuntimeError("Could not resolve api.github.com"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Could not resolve api.github.com"
            ):
                fetch_comments._ensure_gh_authenticated()

    def test_fork_pr_uses_base_repository_url(self):
        with patch.object(
            fetch_comments,
            "gh_pr_view_json",
            return_value={"number": 8, "url": "https://github.com/base/project/pull/8"},
        ):
            self.assertEqual(
                fetch_comments.get_current_pr_ref(), ("base", "project", 8)
            )


class CollectorCliTests(unittest.TestCase):
    def result(self):
        return {
            "schema_version": 2,
            "pull_request": {
                "number": 8,
                "url": "https://github.com/o/r/pull/8",
                "head_sha": "head",
            },
            "review_threads": [],
            "reviews": [],
            "review_findings": [],
            "sonar_issues": [],
            "conversation_comments": [
                {
                    "id": "unknown",
                    "body": "Unknown bot claim",
                    "author": {"login": "new-bot"},
                }
            ],
            "sonarqube": {"status": "not_detected"},
        }

    def run_cli(self, directory, result, format_name=None):
        argv = [str(SCRIPT), "--no-state", "--output-dir", directory]
        if format_name:
            argv += ["--format", format_name]
        with (
            patch.object(sys, "argv", argv),
            patch.object(fetch_comments, "_ensure_gh_authenticated"),
            patch.object(
                fetch_comments, "get_current_pr_ref", return_value=("o", "r", 8)
            ),
            patch.object(fetch_comments, "fetch_all", return_value=result),
            redirect_stdout(io.StringIO()) as output,
        ):
            fetch_comments.main()
        return output.getvalue()

    def test_default_agent_packet_and_saved_ledger_are_ready_to_use(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.run_cli(directory, self.result())
            ledger = json.loads((Path(directory) / "ledger.json").read_text())
            self.assertIn("| # | Source | Classification | Disposition |", output)
            self.assertEqual(ledger[0]["number"], 1)
            self.assertEqual(
                (Path(directory) / "packet.md").read_text().strip(), output.strip()
            )

    def test_json_stdout_has_no_markdown_or_diagnostic_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.run_cli(directory, self.result(), "json")
            payload = json.loads(output)
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(len(payload["items"]), len(payload["ledger"]))

    def test_sonar_error_writes_evidence_but_exits_nonzero(self):
        result = self.result()
        result["sonarqube"] = {"status": "error", "error": "Denied"}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit) as error:
                self.run_cli(directory, result)
            self.assertEqual(error.exception.code, 1)
            self.assertTrue((Path(directory) / "snapshot.json").exists())


if __name__ == "__main__":
    unittest.main()
