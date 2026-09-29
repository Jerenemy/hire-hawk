import argparse
import json
import logging
import os
import smtplib

from .mail import Gmail
from .state import State, process_lock
from .workday import DEFAULT_URL, FetchError, Workday

log = logging.getLogger(__name__)


def check(client, state, mailer):
    # Deliver old queued alerts even if this run's Workday fetch fails.
    fetch_error = None
    try:
        jobs = client.fetch()
        count = state.record(jobs, client.search_url)
        log.info("Successful snapshot: %d matching jobs, %d newly present", len(jobs), count)
    except Exception as exc:
        fetch_error = exc
    for notification_id, payload, created_at in state.pending():
        mailer.digest(payload, created_at)
        state.delivered(notification_id)
        log.info("Delivered notification %d", notification_id)
    if fetch_error:
        raise fetch_error


def main():
    parser = argparse.ArgumentParser(description="Monitor your Spencer Stuart Workday search")
    parser.add_argument("command", choices=["check", "fetch", "test-email"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    try:
        if args.command == "fetch":
            client = Workday(os.environ.get("SEARCH_URL", DEFAULT_URL))
            print(json.dumps([j.to_dict() for j in client.fetch()], indent=2))
            return 0
        mailer = Gmail(os.environ.get("GMAIL_ADDRESS"), os.environ.get("GMAIL_APP_PASSWORD"),
                       os.environ.get("EMAIL_TO"))
        if args.command == "test-email":
            mailer.send("Spencer Stuart notifier — test", "Your job notifier can send email successfully.")
            log.info("Test email accepted by Gmail")
            return 0
        client = Workday(os.environ.get("SEARCH_URL", DEFAULT_URL))
        with process_lock(os.environ.get("DATABASE_PATH", "./state/jobs.sqlite3")) as path:
            state = State(path)
            try:
                check(client, state, mailer)
            finally:
                state.close()
        return 0
    except Exception as exc:
        # Never log configuration values or SMTP response bodies (which may contain addresses).
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            reason = "Gmail authentication failed; check your address and app password"
        elif isinstance(exc, smtplib.SMTPException):
            reason = "SMTP delivery failed; pending emails will retry next run"
        elif isinstance(exc, (FetchError, ValueError, RuntimeError)):
            reason = str(exc)
        else:
            reason = "Check network, database permissions, and provider availability"
        log.error("%s: %s", type(exc).__name__, reason)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
