from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import review_packet
import review_state


def snapshot():
    result = {
        "schema_version": 2,
        "pull_request": {
            "number": 8,
            "url": "https://github.com/o/r/pull/8",
            "head_sha": "head",
        },
        "review_threads": [
            {
                "id": "thread",
                "isOutdated": True,
                "path": "src/a.py",
                "originalLine": 12,
                "comments": {
                    "nodes": [
                        {
                            "id": "comment",
                            "databaseId": 123,
                            "body": "Reject hardlinks",
                            "author": {"login": "new-bot", "__typename": "Bot"},
                        }
                    ]
                },
            }
        ],
        "review_findings": [
            {
                "id": "finding",
                "body": "Reject hardlinks",
                "title": "Reject hardlinks",
                "discussion_url": "https://github.com/o/r/pull/8#discussion_r123",
            }
        ],
        "reviews": [
            {
                "id": "review",
                "body": "Unrecognized format with a claim",
                "author": {"login": "another-bot"},
            }
        ],
        "conversation_comments": [],
        "sonar_issues": [],
        "sonarqube": {"status": "not_detected"},
    }
    result["items"] = review_packet.normalize_items(result)
    return result


class PacketTests(unittest.TestCase):
    def test_normalization_links_evidence_without_duplicate_actions(self):
        result = snapshot()
        thread, finding, review = result["items"]
        self.assertTrue(thread["resolvable"])
        self.assertEqual(thread["line"], 12)
        self.assertTrue(thread["outdated"])
        self.assertEqual(thread["summary"], "Reject hardlinks")
        self.assertFalse(finding["resolvable"])
        self.assertIsNone(finding["reply_kind"])
        self.assertEqual(finding["related_item_ids"], ["thread"])
        self.assertTrue(review["inspection_required"])
        self.assertEqual(review["body"], "Unrecognized format with a claim")

    def test_sonar_string_author_survives_normalization_and_packet_rendering(self):
        result = snapshot()
        result["sonar_issues"] = [
            {
                "key": "issue-1",
                "project": "o_r",
                "author": "committer@example.com",
                "body": "Check the issue",
                "component": "o_r:src/a.py",
                "line": 12,
            }
        ]
        result["items"] = review_packet.normalize_items(result)
        result["ledger"] = review_packet.make_ledger(result["items"])
        issue = result["items"][-1]
        self.assertEqual(issue["author"], "committer@example.com")
        self.assertEqual(issue["author_type"], "Unknown")
        self.assertFalse(issue["resolvable"])
        self.assertIsNone(issue["reply_kind"])
        self.assertIn(
            "SonarQube issue · committer@example.com",
            review_packet.render_packet(result),
        )

    def test_string_authors_in_reviews_and_thread_replies_are_supported(self):
        result = snapshot()
        result["reviews"][0]["author"] = "reviewer"
        result["review_threads"][0]["comments"]["nodes"][0]["author"] = "first-reviewer"
        result["review_threads"][0]["comments"]["nodes"].append(
            {
                "id": "reply",
                "body": "Reply evidence",
                "author": "reply-reviewer",
            }
        )
        items = review_packet.normalize_items(result)
        self.assertEqual(items[0]["author"], "first-reviewer")
        self.assertIn("reply-reviewer", items[0]["body"])
        self.assertEqual(items[-1]["author"], "reviewer")

    def test_missing_and_empty_sonar_authors_have_provider_fallback(self):
        for author in (None, "", {}):
            with self.subTest(author=author):
                result = snapshot()
                result["sonar_issues"] = [
                    {"key": "issue", "project": "o_r", "author": author}
                ]
                self.assertEqual(
                    review_packet.normalize_items(result)[-1]["author"], "SonarQube"
                )

    def test_foreign_pr_reference_does_not_link_local_thread(self):
        result = snapshot()
        result["review_findings"][0]["discussion_url"] = (
            "https://evil.example/o/r/pull/8#discussion_r123"
        )
        self.assertEqual(
            review_packet.normalize_items(result)[1]["related_item_ids"], []
        )

    def test_relative_discussion_reference_is_linked(self):
        result = snapshot()
        result["review_findings"][0]["discussion_url"] = "#discussion_r123"
        self.assertEqual(
            review_packet.normalize_items(result)[1]["related_item_ids"], ["thread"]
        )

    def test_ledger_covers_every_collection_without_fabricated_judgment(self):
        result = snapshot()
        ledger = review_packet.make_ledger(result["items"])
        review_packet.validate_ledger(ledger, result["items"])
        self.assertEqual(len(ledger), 3)
        self.assertEqual([row["number"] for row in ledger], [1, 2, 3])
        self.assertTrue(
            all(row["classification"] == "Pending assessment" for row in ledger)
        )
        with self.assertRaises(ValueError):
            review_packet.validate_ledger(ledger, result["items"], assessed=True)

    def test_changed_evidence_resets_assessment_but_preserves_number(self):
        result = snapshot()
        ledger = review_packet.make_ledger(result["items"])
        ledger[0].update(classification="Invalid", disposition="reply, no code change")
        unchanged = review_packet.make_ledger(result["items"], ledger)
        self.assertEqual(unchanged[0]["classification"], "Invalid")
        result["review_threads"][0]["comments"]["nodes"][0]["body"] = (
            "A different claim"
        )
        updated = review_packet.make_ledger(
            review_packet.normalize_items(result), ledger
        )
        self.assertEqual(updated[0]["number"], 1)
        self.assertEqual(updated[0]["classification"], "Pending assessment")

    def test_stale_head_metadata_does_not_change_claim_identity(self):
        result = snapshot()
        result["reviews"][0]["commit"] = {"oid": "head"}
        old = review_packet.normalize_items(result)
        result["pull_request"]["head_sha"] = "new-head"
        new = review_packet.normalize_items(result)
        self.assertTrue(new[-1]["stale_commit"])
        self.assertEqual(old[-1]["content_hash"], new[-1]["content_hash"])

    def test_duplicate_and_missing_coverage_fail(self):
        result = snapshot()
        ledger = review_packet.make_ledger(result["items"])
        with self.assertRaises(ValueError):
            review_packet.validate_ledger(ledger[:-1], result["items"])
        ledger[1]["number"] = ledger[0]["number"]
        with self.assertRaises(ValueError):
            review_packet.validate_ledger(ledger, result["items"])

    def test_ledger_cannot_forge_resolution_capability(self):
        result = snapshot()
        ledger = review_packet.make_ledger(result["items"])
        ledger[1]["resolvable"] = True
        with self.assertRaises(ValueError):
            review_packet.validate_ledger(ledger, result["items"])

    def test_ledger_cannot_authorize_invalid_fix(self):
        result = snapshot()
        ledger = review_packet.make_ledger(result["items"])
        for row in ledger:
            row.update(classification="Invalid", disposition="fix")
        with self.assertRaises(ValueError):
            review_packet.validate_ledger(ledger, result["items"], assessed=True)

    def test_user_requested_disposition_does_not_bypass_validity_or_clarification(self):
        result = snapshot()
        for classification in ("Valid", "Requires clarification"):
            with self.subTest(classification=classification):
                ledger = review_packet.make_ledger(result["items"])
                for row in ledger:
                    row.update(
                        classification=classification, disposition="fix, user requested"
                    )
                with self.assertRaises(ValueError):
                    review_packet.validate_ledger(
                        ledger, result["items"], assessed=True
                    )

    def test_packet_contains_table_and_full_fenced_unknown_evidence(self):
        result = snapshot()
        body = (
            "```\n# malicious heading\n<script>do something</script>\n" + "tail" * 6000
        )
        result["reviews"][0]["body"] = body
        result["items"] = review_packet.normalize_items(result)
        result["ledger"] = review_packet.make_ledger(result["items"])
        packet = review_packet.render_packet(result)
        self.assertIn("| # | Source | Classification | Disposition |", packet)
        self.assertIn("3 items / 3 ledger rows", packet)
        self.assertIn("Related thread rows: #1", packet)
        self.assertIn("````text\n" + body + "\n````", packet)

    def test_table_escapes_external_delimiters(self):
        result = snapshot()
        ledger = review_packet.make_ledger(result["items"])
        ledger[0]["summary"] = "<img src=x>|\nnext [link](bad) & more"
        rendered = review_packet.render_ledger(ledger)
        self.assertIn("<img src=x>\\| next \\[link\\](bad) & more", rendered)
        self.assertEqual(len(rendered.splitlines()), 5)

    def test_table_caps_free_text_cells(self):
        result = snapshot()
        ledger = review_packet.make_ledger(result["items"])
        ledger[0]["summary"] = "word " * 40
        ledger[0]["proposed_action"] = "short action"
        rendered = review_packet.render_ledger(ledger)
        summary = rendered.splitlines()[2].split(" | ")[4]
        self.assertEqual(len(summary), review_packet.LEDGER_TEXT_LIMIT)
        self.assertTrue(summary.endswith("…"))
        self.assertIn("| short action |", rendered)
        self.assertNotIn("Resolvable", rendered)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "state.json"
        self.mock = patch.object(review_state, "state_path", return_value=self.path)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def test_collection_update_validate_render_need_no_json_parser(self):
        result = snapshot()
        value = review_state.collect(result)
        self.assertEqual(value["phase"], "collecting")
        for number in (1, 2, 3):
            review_state.main(
                [
                    "update",
                    str(number),
                    "--classification",
                    "Context only",
                    "--disposition",
                    "no action, context recorded",
                    "--summary",
                    "Verified context",
                ]
            )
        with redirect_stdout(io.StringIO()) as output:
            review_state.main(["validate"])
            review_state.main(["render"])
            review_state.main(["item", "1"])
        self.assertIn("Ledger coverage and assessments complete", output.getvalue())
        self.assertIn("| 1 |", output.getvalue())
        self.assertIn('"reply_comment_id": 123', output.getvalue())

    def test_preview_does_not_write_state(self):
        review_state.collect(snapshot(), persist=False)
        self.assertFalse(self.path.exists())

    def test_unexpected_head_change_does_not_overwrite_state(self):
        result = snapshot()
        review_state.collect(result)
        saved = self.path.read_text()
        result["pull_request"]["head_sha"] = "different"
        with self.assertRaises(ValueError):
            review_state.collect(result)
        self.assertEqual(self.path.read_text(), saved)

    def test_changed_evidence_revokes_previous_selection(self):
        result = snapshot()
        state = review_state.collect(result)
        state["ledger"][0].update(classification="Valid", disposition="fix")
        state["selected"] = [1]
        review_state.save(state)
        result["review_threads"][0]["comments"]["nodes"][0]["body"] = "New claim"
        result["items"] = review_packet.normalize_items(result)
        state = review_state.collect(result)
        self.assertEqual(state["selected"], [])
        self.assertEqual(state["phase"], "collecting")

    def test_disappeared_rows_preserved_in_history_and_numbers_not_reused(self):
        result = snapshot()
        review_state.collect(result)
        result["reviews"] = []
        result["items"] = review_packet.normalize_items(result)
        state = review_state.collect(result)
        self.assertEqual(state["history"][0]["number"], 3)
        result["reviews"] = [{"id": "new-review", "body": "Another claim"}]
        result["items"] = review_packet.normalize_items(result)
        state = review_state.collect(result)
        self.assertEqual(state["ledger"][-1]["number"], 4)
        with redirect_stdout(io.StringIO()) as output:
            review_state.main(["render", "--history"])
        self.assertIn("| 3 |", output.getvalue())

    def test_legacy_ledger_preserved_for_audit_and_reassessment(self):
        value = review_state.collect(snapshot())
        value.pop("items")
        value["ledger"] = [{"old-custom-format": "legacy evidence"}]
        review_state.save(value)
        value = review_state.collect(snapshot())
        self.assertEqual(
            value["legacy_ledger"], [{"old-custom-format": "legacy evidence"}]
        )
        self.assertEqual(len(value["ledger"]), 3)

    def test_terminal_state_starts_new_workflow(self):
        value = review_state.collect(snapshot())
        value["phase"] = "complete"
        review_state.save(value)
        result = snapshot()
        result["pull_request"]["head_sha"] = "new-head"
        value = review_state.collect(result)
        self.assertEqual(value["phase"], "collecting")
        self.assertNotIn("cycle", value)
        self.assertIsNone(value["review_bot"])

    def test_selection_rejects_unassessed_and_unknown_rows(self):
        review_state.collect(snapshot())
        with self.assertRaises(ValueError):
            review_state.main(["selection", "1"])
        for number in (1, 2, 3):
            review_state.main(
                [
                    "update",
                    str(number),
                    "--classification",
                    "Valid",
                    "--disposition",
                    "fix",
                ]
            )
        with self.assertRaises(ValueError):
            review_state.main(["selection", "99"])
        review_state.main(["selection", "1,3"])
        self.assertEqual(review_state.load()["selected"], [1, 3])

    def assessed_state(self):
        value = review_state.collect(snapshot())
        for row in value["ledger"]:
            row.update(classification="Valid", disposition="fix")
        value["ledger"][0].update(
            classification="Invalid", disposition="reply, no code change"
        )
        review_state.save(value)
        return value

    def test_explicit_user_request_can_select_invalid_claim_without_reclassifying(self):
        self.assessed_state()
        review_state.main(["selection", "1,2", "--user-requested", "1"])
        value = review_state.load()
        self.assertEqual(value["selected"], [1, 2])
        self.assertEqual(value["user_requested"], [1])
        self.assertEqual(value["ledger"][0]["classification"], "Invalid")
        self.assertEqual(value["ledger"][0]["disposition"], "fix, user requested")
        self.assertEqual(value["phase"], "implementing")
        with redirect_stdout(io.StringIO()):
            review_state.main(["validate"])
        continued = review_state.collect(snapshot())
        self.assertEqual(continued["selected"], [1, 2])
        self.assertEqual(continued["user_requested"], [1])

    def test_invalid_claim_still_cannot_be_selected_without_explicit_request(self):
        self.assessed_state()
        before = self.path.read_text()
        with self.assertRaises(ValueError):
            review_state.main(["selection", "1,2"])
        self.assertEqual(self.path.read_text(), before)

    def test_user_request_must_be_selected_and_cannot_bypass_clarification(self):
        value = self.assessed_state()
        value["ledger"][2].update(
            classification="Requires clarification", disposition="request clarification"
        )
        review_state.save(value)
        before = self.path.read_text()
        for arguments in (
            ["selection", "2", "--user-requested", "1"],
            ["selection", "99", "--user-requested", "99"],
            ["selection", "3", "--user-requested", "3"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                review_state.main(arguments)
            self.assertEqual(self.path.read_text(), before)

    def test_changed_evidence_revokes_explicit_user_request(self):
        self.assessed_state()
        review_state.main(["selection", "1", "--user-requested", "1"])
        result = snapshot()
        result["review_threads"][0]["comments"]["nodes"][0]["body"] = "Changed claim"
        result["items"] = review_packet.normalize_items(result)
        value = review_state.collect(result)
        self.assertEqual(value["selected"], [])
        self.assertEqual(value["user_requested"], [])
        self.assertEqual(value["ledger"][0]["disposition"], "Pending assessment")

    def test_context_only_claim_can_be_explicitly_requested(self):
        value = self.assessed_state()
        value["ledger"][0].update(
            classification="Context only", disposition="no action, context recorded"
        )
        review_state.save(value)
        review_state.main(["selection", "1", "--user-requested", "1"])
        with redirect_stdout(io.StringIO()):
            review_state.main(["validate"])

    def test_status_alias_is_read_only(self):
        self.assessed_state()
        before = self.path.read_text()
        with redirect_stdout(io.StringIO()) as output:
            review_state.main(["status"])
        self.assertEqual(json.loads(output.getvalue()), json.loads(before))
        self.assertEqual(self.path.read_text(), before)

    def test_atomic_write_produces_valid_json_and_no_temporary_files(self):
        value = review_state.collect(snapshot())
        self.assertEqual(json.loads(self.path.read_text()), value)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


if __name__ == "__main__":
    unittest.main()
