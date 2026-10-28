# Spencer Stuart job notifier

Checks your exact Spencer Stuart Workday search and emails you when a new job appears or a removed job returns. Runs every 30 minutes on Linux using systemd. Python 3.13+; no third-party runtime dependencies or browser required.

The default search preserves all six location IDs, Permanent, Full time, and Associate Team from your supplied URL. Workday files analysts and associates under the same Associate Team category, so the checker also keeps only jobs whose title contains the word “Analyst” (for example “Analyst, Industrial Practice” or “Senior Analyst, …”); associate roles never reach your inbox. Run `fetch` to see the exact jobs it currently matches.

## How notifications work

The checker fetches every listings page and each posting's details, comparing internal posting IDs against the previous successful snapshot. Changes to dates, titles, descriptions, and ordering do not cause alerts. One new job replacing another is detected even when the count stays unchanged.

The first successful check emails all current matches (or a no-matches confirmation). Subsequent digests contain only newly present jobs. A job absent in a successful check and present in a later check is labeled as returning. Removal itself does not send an email.

Each job includes its posting date and time since posted in calendar days, calculated using the UTC date when the email is sent. Workday supplies a date (`startDate`), but no exact posting time of day; the email states that limitation rather than assuming midnight. The digest separately shows the exact detection timestamp. If the date is missing or invalid, the email uses Workday's relative posting label as an estimate recorded at detection, or shows “Unknown.” Older queued emails without posting information still send normally. Posting age refers to the original date supplied by Workday, including for returning jobs.

SQLite records the snapshot and pending digest in one transaction. SMTP failure leaves the digest queued for another run. Older digests are attempted even when the current Workday fetch fails. Malformed, incomplete, or failed fetches leave the previous snapshot intact. A lock prevents overlapping checks using the same database.

## Try it locally

From this project directory:

```sh
python3 --version  # must be 3.13 or newer
python3 -m unittest discover -s tests -v
python3 -m job_scraping_notification fetch
```

`fetch` reads the live search without sending mail, creating a database, or modifying notification history. The source can run directly; installing the package is optional (`python3 -m pip install .` also provides the `spencer-jobs` command).

## Set up Gmail

1. Enable [Google 2-Step Verification](https://support.google.com/mail/answer/185839).
2. Create an [app password](https://myaccount.google.com/apppasswords) named “Spencer jobs”. Use that password, not your Google account password. See [Google's app-password guide](https://support.google.com/mail/answer/185833) if the option is unavailable; managed accounts may restrict it.
3. Put your Gmail address, app password, and destination address in the configuration below. `EMAIL_TO` is one address and can be the same as the sending account. Gmail is contacted over TLS at `smtp.gmail.com:465`.

Keep credentials on your server, outside the repository. You do not need to send them to anyone. If your Google password changes, you may need to create a new app password.

## Install on your Linux server

These commands assume systemd, sudo, and an installed Python 3.13+ interpreter with the `venv` module. Install Python using your distribution's supported installation method first; use its actual executable name in the commands if it differs from `python3`.

`/opt/spencer-jobs` is a root-owned git checkout, which the [automatic deploys](#automatic-deploys) fast-forward. Run the following **from `/opt/spencer-jobs`** once it is cloned:

```sh
sudo git clone https://github.com/Jerenemy/hire-hawk.git /opt/spencer-jobs
cd /opt/spencer-jobs
sudo useradd --system --home-dir /var/lib/spencer-jobs --shell /usr/sbin/nologin spencer-jobs
sudo python3 -m venv /opt/spencer-jobs/.venv
sudo install -d -o spencer-jobs -g spencer-jobs -m 0700 /var/lib/spencer-jobs
sudo install -o root -g root -m 0600 .env.example /etc/spencer-jobs.env
sudoedit /etc/spencer-jobs.env
```

Replace the three email placeholders. Leave `DATABASE_PATH=/var/lib/spencer-jobs/jobs.sqlite3`. The exact search is built in; `SEARCH_URL` need not be set. If you override it, use the full URL with literal `&` characters, surrounded by single quotes. Supported URL parameters are `locations`, `workerSubType`, `timeType`, `jobFamilyGroup`, and optional `searchText`. Unsupported parameters are rejected rather than silently ignored. Changing the URL requires a new database path and starts a new baseline.

The environment file accepts `KEY=value` assignments (no `export`). It is read by systemd, **not automatically loaded by Python**. Do not commit it.

Install the service and timer:

```sh
sudo install -m 0644 deploy/spencer-jobs.service /etc/systemd/system/
sudo install -m 0644 deploy/spencer-jobs.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Validate the live fetch under the service account:

```sh
sudo -u spencer-jobs /bin/sh -c 'cd /opt/spencer-jobs && .venv/bin/python -m job_scraping_notification fetch'
```

Send a test email with the protected environment file. This does not change job history:

```sh
sudo systemd-run --wait --pipe --collect \
  --unit=spencer-jobs-test \
  --property=User=spencer-jobs \
  --property=WorkingDirectory=/opt/spencer-jobs \
  --property=EnvironmentFile=/etc/spencer-jobs.env \
  /opt/spencer-jobs/.venv/bin/python -m job_scraping_notification test-email
```

Check your inbox (and spam folder), then run the first real check and enable the schedule:

```sh
sudo systemctl start spencer-jobs.service
sudo journalctl -u spencer-jobs.service -n 50 --no-pager
sudo systemctl enable --now spencer-jobs.timer
systemctl list-timers spencer-jobs.timer
```

The first check sends the current matches. The timer then runs at minute 00 and 30 of each hour. `Persistent=true` runs a missed check after downtime; it cannot recover jobs posted and removed entirely while the server was offline. No cron entry is needed.

## Configuration

| Variable | Meaning | Default |
| --- | --- | --- |
| `GMAIL_ADDRESS` | Gmail account used to send | Required for email commands |
| `GMAIL_APP_PASSWORD` | Gmail app password; spaces are stripped | Required for email commands |
| `EMAIL_TO` | Single destination email address | Required for email commands |
| `DATABASE_PATH` | Persistent SQLite database | `./state/jobs.sqlite3`; server example uses `/var/lib/spencer-jobs/jobs.sqlite3` |
| `SEARCH_URL` | Spencer Stuart en-US Workday search URL | Your exact supplied search |

Commands: `fetch` prints live matches as JSON; `test-email` verifies Gmail delivery; `check` fetches, saves changes, and drains pending emails. Failures return exit status 1 and log to stderr/the systemd journal. Successful snapshots and accepted emails are logged. Credentials and SMTP response bodies are not logged.

## Automatic deploys

`.github/workflows/deploy.yml` runs on every push to `main` (and by hand from the Actions tab). It runs the tests, then SSHes into the server and runs `sudo /usr/local/sbin/spencer-jobs-deploy <commit>`. That script fast-forwards `/opt/spencer-jobs` to `origin/main`, reinstalls the service and timer units, reloads systemd, and checks the code imports. The service is a oneshot started by the timer, so the next check runs the new code; nothing is restarted and the database in `/var/lib/spencer-jobs` is never touched. If the checkout was edited on the server, the fast-forward refuses and the run fails, leaving the server as it was.

One-time setup, on the server. It uses the same `jzay` deploy key as the website, so step 1 is already done if the website deploys automatically.

1. Make a key for GitHub and authorize it (skip if `~/.ssh/github_deploy` exists):

```bash
ssh-keygen -t ed25519 -N '' -C github-deploy -f ~/.ssh/github_deploy && cat ~/.ssh/github_deploy.pub >> ~/.ssh/authorized_keys
```

2. If `/opt/spencer-jobs` was set up by copying files rather than cloning, turn it into a checkout. This keeps `.venv`, and `/etc/spencer-jobs.env` and the database are elsewhere:

```bash
sudo git -C /opt/spencer-jobs init -q -b main && sudo git -C /opt/spencer-jobs remote add origin https://github.com/Jerenemy/hire-hawk.git && sudo git -C /opt/spencer-jobs fetch -q origin main && sudo git -C /opt/spencer-jobs reset -q --hard origin/main
```

3. Install the deploy script root-owned (the workflow runs this copy, so a push cannot change what sudo runs) and let `jzay` run it without a password:

```bash
sudo install -o root -g root -m 0755 /opt/spencer-jobs/deploy/spencer-jobs-deploy /usr/local/sbin/
```

```bash
echo 'jzay ALL=(root) NOPASSWD: /usr/local/sbin/spencer-jobs-deploy' | sudo tee /etc/sudoers.d/spencer-jobs-deploy && sudo chmod 0440 /etc/sudoers.d/spencer-jobs-deploy && sudo visudo -c
```

4. In GitHub, hire-hawk → Settings → Secrets and variables → Actions, add `DEPLOY_HOST`, `DEPLOY_USER` (`jzay`) and `DEPLOY_SSH_KEY` (the contents of `~/.ssh/github_deploy`), with the same values as the website repository, plus `DEPLOY_PORT` if SSH is not on 22. Then rerun the latest "Test and deploy" run from the Actions tab.

After a change to `deploy/spencer-jobs-deploy` itself, repeat step 3.

## Maintenance and troubleshooting

```sh
sudo journalctl -u spencer-jobs.service --since '1 day ago' --no-pager
systemctl status spencer-jobs.timer
sudo systemctl start spencer-jobs.service  # retry now
sudo systemctl disable --now spencer-jobs.timer  # stop future checks
```

For authentication failures, check the Gmail address, app password, and account restrictions. For network failures, verify outbound HTTPS (443), Gmail SMTP (465), DNS, and the server's CA certificates. Workday requests time out after 30 seconds and transient failures retry up to three attempts with bounded backoff. SMTP uses a 30-second timeout and retries queued notifications on later checks.

If the database is not writable, check ownership of `/var/lib/spencer-jobs`. Service hardening permits writes only to its state directory and private temporary directory; keep a custom database path under `/var/lib/spencer-jobs` or update the service's write permissions. `SEARCH_URL changed` means you need to restore the original URL or select a new database for the new search.

The database contains history and pending notifications; keep it across code updates. To back it up, stop the timer, wait for an active service run to finish, copy the database, and restart the timer. Deleting it resets monitoring and emails all current matches again.

## Limits and validation

- Workday's public CXS endpoint is undocumented and may change. Its adapter is isolated in `job_scraping_notification/workday.py`.
- Pagination checks reject changed totals, duplicate results, missing pages, and invalid details. Workday does not provide an atomic snapshot, so changes during a fetch cannot be completely ruled out.
- A reappearance must span two successful checks. Alerts indicate jobs entering your filtered results, which can include an existing job whose location or category changed.
- If Gmail accepts a message and the process stops before recording delivery, it can be sent again. Accepted mail is not a guarantee of inbox placement.
- Failures appear in the journal; there is no separate failure-alert channel. Review logs periodically.

The automated suite uses Workday fixtures and mocked SMTP to cover filters, pagination, stable IDs, first-run behavior, returning jobs, transactions, failures, retries, restarts, and locking. Live read-only validation on 2026-10-28 returned three analyst postings (Industrial Practice; Supply Chain and Operations Officer Practice; Board & CEO Advisory Practice) and none of the associate ones. A real test email and systemd activation must be completed on your server with your credentials.
