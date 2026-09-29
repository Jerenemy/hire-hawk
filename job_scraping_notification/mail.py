"""Gmail SMTP delivery over TLS."""
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from datetime import date, datetime, timezone
import smtplib
import ssl


def posting_lines(job, today):
    """Use calendar days: Workday's startDate has no time or timezone."""
    try:
        posted = date.fromisoformat(job.get("posted_date"))
    except (ValueError, TypeError):
        label = job.get("posted_label")
        age = f"{label} (Workday estimate at detection)" if isinstance(label, str) and label else "Unknown"
        return ["Posted: Unknown (date/time not provided)", f"Time since posted: {age}"]
    days = (today - posted).days
    if days < 0:
        age = "Unknown (Workday reports a future date)"
    elif days == 0:
        age = "Today (calendar date)"
    else:
        age = f"{days} calendar day{'s' if days != 1 else ''} ago"
    return [f"Posted: {posted.isoformat()} (time of day not provided by Workday)",
            f"Time since posted: {age}"]


class Gmail:
    def __init__(self, address, password, recipient):
        if not all((address, password, recipient)):
            raise ValueError("Set GMAIL_ADDRESS, GMAIL_APP_PASSWORD, and EMAIL_TO")
        for value in (address, recipient):
            if "@" not in value or any(c in value for c in "\r\n,;"):
                raise ValueError("Email settings must each contain one email address")
        self.address, self.password, self.recipient = address, password.replace(" ", ""), recipient

    def send(self, subject, body):
        message = EmailMessage()
        message["From"], message["To"] = self.address, self.recipient
        message["Subject"] = subject
        message["Date"], message["Message-ID"] = formatdate(localtime=False), make_msgid()
        message.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30, context=ssl.create_default_context()) as smtp:
            smtp.login(self.address, self.password)
            refused = smtp.send_message(message)
            if refused:
                raise smtplib.SMTPException("Gmail refused the recipient")

    def digest(self, payload, created_at, *, now=None):
        now = now or datetime.now(timezone.utc)
        today = now.astimezone(timezone.utc).date()
        jobs = payload["jobs"]
        label = "Initial matches" if payload["initial"] else "New or returning jobs"
        lines = [f"{label}: {len(jobs)}", f"Detected at {created_at} UTC", ""]
        if not jobs:
            lines.append("Monitoring is active. No jobs currently match your search.")
        for job in jobs:
            suffix = " (returned to search)" if job["returning"] else ""
            lines.extend([job["title"] + suffix, f"Location: {job['location']}",
                          f"Requisition: {job['requisition']}",
                          *posting_lines(job, today), job["url"], ""])
        if jobs:
            lines.extend([f"Posting ages calculated as of {today.isoformat()} (UTC calendar date).", ""])
        lines.extend(["Search:", payload["search_url"]])
        self.send(f"Spencer Stuart — {label}: {len(jobs)}", "\n".join(lines))
