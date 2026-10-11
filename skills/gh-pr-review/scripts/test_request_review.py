import argparse
import unittest

import request_review

AFTER = "2026-10-11T12:00:00Z"
BEFORE = "2026-10-11T11:00:00Z"
LATER = "2026-10-11T12:05:00Z"


def review(login, submitted_at):
    return {"user": {"login": login}, "submitted_at": submitted_at}


def comment(login, created_at, updated_at=None):
    return {
        "user": {"login": login},
        "created_at": created_at,
        "updated_at": updated_at or created_at,
    }


class BotNameTests(unittest.TestCase):
    def test_accepts_names_logins_and_mentions(self):
        for value, expected in [
            ("Copilot", "copilot"),
            ("CodeRabbit", "coderabbit"),
            ("code rabbit", "coderabbit"),
            ("@coderabbitai", "coderabbit"),
            ("coderabbitai[bot]", "coderabbit"),
            ("@greptileai", "greptile"),
            ("greptile-apps[bot]", "greptile"),
        ]:
            self.assertEqual(request_review.bot_name(value), expected, value)

    def test_rejects_unsupported_bots(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            request_review.bot_name("sourcery")


class LastUsedBotTests(unittest.TestCase):
    def test_picks_newest_bot_activity_across_sources(self):
        activity = request_review.pr_activity(
            [review("copilot-pull-request-reviewer[bot]", BEFORE)],
            [comment("coderabbitai[bot]", BEFORE, LATER)],
            [comment("octocat", "2026-12-01T00:00:00Z")],
        )
        self.assertEqual(request_review.last_used_bot(activity), "coderabbit")

    def test_recorded_request_counts_as_newest_use(self):
        activity = [("coderabbit", BEFORE)]
        self.assertEqual(
            request_review.last_used_bot(activity, ("greptile", AFTER)), "greptile"
        )
        self.assertEqual(
            request_review.last_used_bot(activity, (None, None)), "coderabbit"
        )

    def test_no_bot_activity(self):
        activity = request_review.pr_activity([review("octocat", AFTER)], [], [])
        self.assertIsNone(request_review.last_used_bot(activity))


class CompletionSignalTests(unittest.TestCase):
    def signal(self, bot, reviews=(), comments=(), statuses=(), runs=()):
        return request_review.completion_signal(
            bot, AFTER, reviews, comments, statuses, runs
        )

    def test_new_review_from_requested_bot_completes(self):
        self.assertIsNotNone(
            self.signal(
                "copilot", reviews=[review("copilot-pull-request-reviewer[bot]", LATER)]
            )
        )

    def test_old_or_other_bot_reviews_do_not_complete(self):
        self.assertIsNone(
            self.signal(
                "copilot",
                reviews=[
                    review("copilot-pull-request-reviewer[bot]", BEFORE),
                    review("coderabbitai[bot]", LATER),
                ],
            )
        )

    def test_coderabbit_finished_status_completes_but_pending_and_acknowledgements_do_not(
        self,
    ):
        status = {
            "creator": {"login": "coderabbitai[bot]"},
            "context": "CodeRabbit",
            "created_at": LATER,
        }
        ack = comment("coderabbitai[bot]", LATER)
        self.assertIsNone(
            self.signal(
                "coderabbit", comments=[ack], statuses=[{**status, "state": "pending"}]
            )
        )
        self.assertIn(
            "success",
            self.signal("coderabbit", statuses=[{**status, "state": "success"}]),
        )

    def test_greptile_check_run_or_summary_edit_completes(self):
        run = {
            "app": {"slug": "greptile-apps"},
            "status": "completed",
            "completed_at": LATER,
            "name": "Greptile",
        }
        self.assertIsNotNone(self.signal("greptile", runs=[run]))
        self.assertIsNone(
            self.signal("greptile", runs=[{**run, "status": "in_progress"}])
        )
        self.assertIsNotNone(
            self.signal(
                "greptile", comments=[comment("greptile-apps[bot]", BEFORE, LATER)]
            )
        )


if __name__ == "__main__":
    unittest.main()
