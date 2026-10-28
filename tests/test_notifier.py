import copy
from dataclasses import replace
from datetime import date, datetime, timezone
import io
import json
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from job_scraping_notification.__main__ import check
from job_scraping_notification.mail import Gmail, posting_lines
from job_scraping_notification.state import State, process_lock
from job_scraping_notification.workday import DEFAULT_URL, FetchError, Workday, request_json

FIXTURE = json.loads((Path(__file__).parent / "fixtures/workday.json").read_text())


def fixture_jobs():
    return Workday(request=Mock(side_effect=[FIXTURE["page"], *FIXTURE["details"]])).fetch()


class WorkdayTests(unittest.TestCase):
    def test_exact_filters_and_details(self):
        request = Mock(side_effect=[FIXTURE["page"], *FIXTURE["details"]])
        jobs = Workday(request=request).fetch()
        payload = request.call_args_list[0].args[1]
        self.assertEqual(payload, {
            "appliedFacets": {
                "locations": ["be264fa6cb8b011cf4589e6d18ba1127", "be264fa6cb8b01c1fd5b656c18bae126",
                              "be264fa6cb8b01d075134b6c18bad126", "be264fa6cb8b013345813d6c18bac926",
                              "be264fa6cb8b01f22a80586c18bad926", "be264fa6cb8b01fe8dc4006c18bab126"],
                "workerSubType": ["be264fa6cb8b0175451b3a2012baaf10"],
                "timeType": ["d665eb2b24fc01b581cff464a3b96100"],
                "jobFamilyGroup": ["be264fa6cb8b01d203f2b5eb17ba5222"],
            }, "limit": 20, "offset": 0, "searchText": "",
        })
        self.assertEqual(jobs[0].id, FIXTURE["details"][0]["jobPostingInfo"]["id"])
        self.assertEqual(jobs[1].location, "New York; Chicago")
        self.assertEqual(jobs[0].posted_date, "2026-08-17")
        self.assertEqual(jobs[0].posted_label, "Posted 30+ Days Ago")

    def test_multiple_pages(self):
        rows = FIXTURE["page"]["jobPostings"]
        request = Mock(side_effect=[{"total": 2, "jobPostings": rows[:1]},
                                    {"total": 2, "jobPostings": rows[1:]}, *FIXTURE["details"]])
        self.assertEqual(len(Workday(request=request).fetch()), 2)
        self.assertEqual([c.args[1]["offset"] for c in request.call_args_list[:2]], [0, 1])
        self.assertEqual(request.call_args_list[0].args[1]["appliedFacets"],
                         request.call_args_list[1].args[1]["appliedFacets"])

    def test_reject_incomplete_changed_duplicate_and_malformed_pages(self):
        row = FIXTURE["page"]["jobPostings"][0]
        bad_pages = [
            [{"total": 2, "jobPostings": [row]}, {"total": 2, "jobPostings": []}],
            [{"total": 2, "jobPostings": [row]}, {"total": 1, "jobPostings": []}],
            [{"total": 2, "jobPostings": [row, row]}],
            [{"total": 0, "jobPostings": [row]}],
            [{"total": "2", "jobPostings": []}], [{}],
        ]
        for pages in bad_pages:
            with self.subTest(pages=pages), self.assertRaises(FetchError):
                Workday(request=Mock(side_effect=pages)).fetch()

    def test_invalid_details_and_duplicate_ids(self):
        for invalid in ({}, {"jobPostingInfo": {"id": "x"}},
                        {"jobPostingInfo": dict(FIXTURE["details"][0]["jobPostingInfo"], posted=False)}):
            with self.subTest(invalid=invalid), self.assertRaises(FetchError):
                Workday(request=Mock(side_effect=[FIXTURE["page"], invalid])).fetch()
        with self.assertRaises(FetchError):
            Workday(request=Mock(side_effect=[FIXTURE["page"], FIXTURE["details"][0], FIXTURE["details"][0]])).fetch()

    def test_only_analyst_titles(self):
        details = copy.deepcopy(FIXTURE["details"])
        details[0]["jobPostingInfo"]["title"] = "Associate, Financial Services"
        details[1]["jobPostingInfo"]["title"] = "Senior Analyst, Industrial Practice"
        jobs = Workday(request=Mock(side_effect=[FIXTURE["page"], *details])).fetch()
        self.assertEqual([j.title for j in jobs], ["Senior Analyst, Industrial Practice"])
        for title in ("Senior Associate, Consumer Practice", "Associate General Counsel", "Analytics Lead"):
            details[1]["jobPostingInfo"]["title"] = title
            with self.subTest(title=title):
                self.assertEqual(Workday(request=Mock(side_effect=[FIXTURE["page"], *details])).fetch(), [])

    def test_valid_empty(self):
        self.assertEqual(Workday(request=Mock(return_value={"total": 0, "jobPostings": []})).fetch(), [])

    def test_unsupported_filters_rejected(self):
        with self.assertRaises(ValueError):
            Workday(DEFAULT_URL + "&unknown=value")

    @patch("job_scraping_notification.workday.time.sleep")
    @patch("job_scraping_notification.workday.urlopen")
    def test_bounded_retries(self, open_url, sleep):
        open_url.side_effect = URLError("offline")
        with self.assertRaises(FetchError):
            request_json("https://example.com")
        self.assertEqual(open_url.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        open_url.reset_mock()
        open_url.side_effect = HTTPError("https://example.com", 404, "missing", {}, None)
        with self.assertRaises(FetchError):
            request_json("https://example.com")
        self.assertEqual(open_url.call_count, 1)

    @patch("job_scraping_notification.workday.urlopen")
    def test_invalid_json(self, open_url):
        open_url.return_value.__enter__.return_value = io.BytesIO(b"<html>unavailable</html>")
        with self.assertRaises(FetchError):
            request_json("https://example.com")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "jobs.sqlite3"
        self.state = State(self.path)
        self.jobs = fixture_jobs()
        self.client = Mock(search_url=DEFAULT_URL)
        self.client.fetch.return_value = self.jobs
        self.mailer = Mock()

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def run_check(self):
        check(self.client, self.state, self.mailer)

    def test_initial_then_unchanged_and_metadata_edits(self):
        self.run_check()
        self.assertEqual(self.mailer.digest.call_count, 1)
        payload = self.mailer.digest.call_args.args[0]
        self.assertTrue(payload["initial"])
        self.assertEqual(len(payload["jobs"]), 2)
        self.client.fetch.return_value = [replace(j, title="Edited") for j in reversed(self.jobs)]
        self.run_check()
        self.assertEqual(self.mailer.digest.call_count, 1)

    def test_date_and_order_changes(self):
        self.run_check()
        changed = copy.deepcopy(FIXTURE)
        changed["page"]["jobPostings"].reverse()
        for row in changed["page"]["jobPostings"]:
            row["postedOn"] = "Posted Today"
        for detail in changed["details"]:
            detail["jobPostingInfo"]["startDate"] = "2026-09-29"
            detail["jobPostingInfo"]["postedOn"] = "Posted Today"
        self.client.fetch.return_value = Workday(request=Mock(side_effect=[changed["page"], *reversed(changed["details"])])).fetch()
        self.run_check()
        self.assertEqual(self.mailer.digest.call_count, 1)

    def test_equal_count_replacement_and_return(self):
        self.run_check()
        new = replace(self.jobs[0], id="new-id", requisition="R9999")
        self.client.fetch.return_value = [new, self.jobs[1]]
        self.run_check()
        payload = self.mailer.digest.call_args.args[0]
        self.assertEqual([j["id"] for j in payload["jobs"]], ["new-id"])
        self.assertFalse(payload["jobs"][0]["returning"])
        self.client.fetch.return_value = []
        self.run_check()
        self.assertEqual(self.mailer.digest.call_count, 2)
        self.client.fetch.return_value = self.jobs
        self.run_check()
        self.assertTrue(all(j["returning"] for j in self.mailer.digest.call_args.args[0]["jobs"]))

    def test_empty_initial_confirmation_only_once(self):
        self.client.fetch.return_value = []
        self.run_check()
        self.run_check()
        self.assertEqual(self.mailer.digest.call_count, 1)
        self.assertEqual(self.mailer.digest.call_args.args[0]["jobs"], [])

    def test_smtp_failure_restart_retry(self):
        self.mailer.digest.side_effect = smtplib.SMTPException("failed")
        with self.assertRaises(smtplib.SMTPException):
            self.run_check()
        self.assertEqual(len(self.state.pending()), 1)
        self.state.close()
        self.state = State(self.path)
        self.mailer.digest.side_effect = None
        self.run_check()
        self.assertEqual(self.mailer.digest.call_count, 2)
        self.assertEqual(self.state.pending(), [])
        self.run_check()
        self.assertEqual(self.mailer.digest.call_count, 2)

    def test_fetch_failure_does_not_remove_jobs_and_delivers_pending(self):
        self.state.record(self.jobs, DEFAULT_URL)
        self.client.fetch.side_effect = FetchError("incomplete")
        with self.assertRaises(FetchError):
            self.run_check()
        self.assertEqual(self.mailer.digest.call_count, 1)
        self.assertEqual(self.state.pending(), [])
        self.client.fetch.side_effect = None
        self.run_check()
        self.assertEqual(self.mailer.digest.call_count, 1)

    def test_transaction_rolls_back_snapshot_and_outbox(self):
        self.state.record(self.jobs, DEFAULT_URL)
        self.state.db.execute("CREATE TRIGGER fail_snapshot BEFORE INSERT ON current_jobs BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(Exception):
            self.state.record([replace(self.jobs[0], id="other")], DEFAULT_URL)
        self.assertEqual(len(self.state.pending()), 1)
        self.assertEqual({r[0] for r in self.state.db.execute("SELECT id FROM current_jobs")}, {j.id for j in self.jobs})

    def test_search_change_rejected(self):
        self.state.record(self.jobs, DEFAULT_URL)
        with self.assertRaises(ValueError):
            self.state.record([], DEFAULT_URL + "&searchText=Other")
        self.assertEqual(len(self.state.pending()), 1)

    def test_process_lock(self):
        with process_lock(self.path):
            with self.assertRaises(RuntimeError):
                with process_lock(self.path):
                    self.fail("Second process acquired lock")
        with process_lock(self.path):
            pass


class MailTests(unittest.TestCase):
    @patch("job_scraping_notification.mail.smtplib.SMTP_SSL")
    def test_gmail_delivery_and_digest(self, smtp_class):
        smtp = smtp_class.return_value.__enter__.return_value
        smtp.send_message.return_value = {}
        mailer = Gmail("sender@gmail.com", "abcd efgh", "recipient@example.com")
        jobs = [dict(j.to_dict(), returning=True) for j in fixture_jobs()]
        mailer.digest({"initial": False, "jobs": jobs, "search_url": DEFAULT_URL}, "2026-09-29 12:00:00",
                      now=datetime(2026, 9, 30, 12, tzinfo=timezone.utc))
        smtp.login.assert_called_once_with("sender@gmail.com", "abcdefgh")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "recipient@example.com")
        for job in jobs:
            for field in ("title", "location", "requisition", "url"):
                self.assertIn(job[field], message.get_content())
        self.assertIn("returned to search", message.get_content())
        self.assertIn("Posted: 2026-08-17 (time of day not provided by Workday)", message.get_content())
        self.assertIn("Time since posted: 44 calendar days ago", message.get_content())
        self.assertIn("Time since posted: 79 calendar days ago", message.get_content())
        self.assertIn("Detected at 2026-09-29 12:00:00 UTC", message.get_content())
        smtp.send_message.return_value = {"recipient@example.com": (550, b"refused")}
        with self.assertRaises(smtplib.SMTPException):
            mailer.send("test", "test")

    def test_missing_credentials(self):
        with self.assertRaises(ValueError):
            Gmail(None, None, None)

    def test_posting_age_edge_cases_and_legacy_payload(self):
        today = date(2026, 9, 29)
        cases = [
            ({"posted_date": "2026-09-29"}, "Today (calendar date)"),
            ({"posted_date": "2026-09-28"}, "1 calendar day ago"),
            ({"posted_date": "2026-09-30"}, "Unknown (Workday reports a future date)"),
            ({"posted_date": "invalid", "posted_label": "Posted 30+ Days Ago"},
             "Posted 30+ Days Ago (Workday estimate at detection)"),
            ({"posted_date": None}, "Unknown"),
            ({}, "Unknown"),
        ]
        for job, expected in cases:
            with self.subTest(job=job):
                self.assertEqual(posting_lines(job, today)[1], f"Time since posted: {expected}")

    def test_legacy_queued_digest_still_sends(self):
        job = dict(fixture_jobs()[0].to_dict(), returning=False)
        del job["posted_date"]
        del job["posted_label"]
        mailer = Gmail("sender@gmail.com", "password", "recipient@example.com")
        mailer.send = Mock()
        mailer.digest({"initial": True, "jobs": [job], "search_url": DEFAULT_URL}, "2026-09-29 12:00:00")
        self.assertIn("Time since posted: Unknown", mailer.send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
