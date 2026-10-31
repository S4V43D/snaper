# Single-page section monitor

A small Python project that checks the text of one CSS-selected section on one permitted, public webpage and emails you when it changes. The included URL is the neutral placeholder `https://example.com/`, selecting its `h1`. Configure your own allowed page before enabling it. No nicotine-product, deal, purchase, or restock target is configured.

## What happens

- First successful check: save a baseline, without a change email.
- Later checks: compare normalized text; unchanged content produces no email and no state commit.
- Changed text: send a plain-text Gmail email listing only REMOVED and ADDED word spans, then save the new baseline. Unchanged text is omitted; a text edit appears in both lists. A list with no changes says `(none)`. Moved text can also appear as removed and added.
- Download, access, selector, or email errors: fail the run and keep the previous baseline.
- GitHub stores snapshots on the separate `monitor-state` branch, so runner cleanup and cache expiration do not erase state. No personal access token is required.

Python 3.13 is used by the workflow. Install the pinned dependencies from `requirements.txt`. Tests use Python's built-in `unittest`.

## 1. Add the files to your repository

Extract the ZIP. Copy the **contents** of `section-monitor/` into your repository root, including the hidden `.github/` directory and `.gitignore`, and commit them to the repository's default branch. Do not leave the project nested beneath another folder: GitHub looks for `.github/workflows/monitor.yml` at the root.

Use a private repository if you do not want snapshots publicly visible. State contains the selected page text, its hash, a configuration fingerprint, and a UTC timestamp. Anyone with repository access can read it and its history. Neither credentials nor recipients are stored in state. Do not monitor sensitive content.

## 2. Choose a permitted section

Edit `config.json`:

```json
{
  "url": "https://example.com/",
  "selector": "h1",
  "ignore_selectors": [],
  "access_permission_confirmed": false
}
```

Replace the placeholder with a public information page you own or have permission to monitor, such as your own announcements page. Use its final, canonical HTTPS address. After reviewing the access rules below, set `access_permission_confirmed` to `true`.

Use the browser's Inspect function to identify a stable CSS selector. For example, `#announcements` selects an element by ID, and `main .opening-hours` selects a section within the main content. The selector must match **exactly one nonempty element in the downloaded HTML**. A missing section is an error, never interpreted as a content change. `ignore_selectors` removes matching descendants inside that section; for example, `[".timestamp", ".visitor-counter"]` can exclude frequent noise. Avoid selectors that remove meaningful updates.

Optional environment variables `MONITOR_URL` and `MONITOR_SELECTOR` override those two fields. In GitHub, add them under **Settings → Secrets and variables → Actions → Variables** if you prefer to manage them there. Empty variables use the JSON values. URLs must not contain credentials, secret query parameters, or personal data; alerts include the URL.

Comparison uses parsed HTML text with whitespace collapsed, and removes scripts, styles, templates and `noscript` elements. It does not execute JavaScript, compute CSS visibility, compare images, detect link-destination-only changes, or compare formatting. Text hidden by CSS can still be included. Pick a server-rendered section and test it. Content changes between checks that revert before the next check will be missed.

## 3. Create the Gmail App Password and repository secrets

1. Turn on **2-Step Verification** for the sending Google account.
2. Open [Google App Passwords](https://myaccount.google.com/apppasswords), create one named `Section monitor`, and copy it. Use this App Password, not your ordinary Google password. The monitor removes spaces from the displayed password.
3. In the GitHub repository, open **Settings → Secrets and variables → Actions → New repository secret** and create:

| Secret | Value |
| --- | --- |
| `GMAIL_USERNAME` | The sending account's complete Gmail or Google Workspace email address |
| `GMAIL_APP_PASSWORD` | Its Google App Password |
| `ALERT_TO` | One recipient email address, which may be the same account |

The sender must be the account that owns the App Password. This project uses `smtp.gmail.com:465` with certificate-verified TLS from connection start. It sends only plain text and never logs the password.

App Passwords require 2-Step Verification and may be unavailable with organizational restrictions, Advanced Protection, or security-key-only verification. A normal Google password change revokes existing App Passwords; create a replacement and update the GitHub secret. Do not weaken account protections to enable this project. See [Google's App Password guidance](https://support.google.com/accounts/answer/185833) and [Gmail SMTP documentation](https://developers.google.com/workspace/gmail/imap/imap-smtp).

Do not commit credentials, place them in `config.json`, include them in screenshots, or paste them into workflow commands. A `.env` file is not loaded by this program. GitHub injects secrets into only the monitor step. Revoke the App Password if exposed or when you retire the monitor.

## 4. Enable and check GitHub Actions

1. Commit the configured files to the default branch and enable Actions if prompted.
2. Ensure repository/organization policy permits the workflow's `contents: write` permission and permits the Actions token to create and update `monitor-state`. Rules requiring pull requests or signed commits on **all** branches can block state writes; scope those rules appropriately for this generated branch. The workflow does not write the default branch.
3. Open **Actions → Section monitor → Run workflow**. Select the default branch and mode `dry-run`. This fetches the page and validates the section without sending mail or saving a baseline. Check that it reports a reasonable text length. A dry run still makes website requests.
4. Run mode `test-email`; check the recipient's inbox and spam folder. This sends one test message without fetching a page or changing state.
5. Run mode `check` to establish the baseline. Confirm the run succeeds and `state.json` appears on the newly created `monitor-state` branch. Later runs email only when selected text changes.

Scheduled runs use cron `7,22,37,52 * * * *`: approximately every 15 minutes, at minutes 07, 22, 37 and 52 in UTC. The offset avoids the top of the hour. The workflow must exist on the default branch. GitHub can delay or drop scheduled jobs under load; this is not an exact timer. Public-repository schedules may be disabled after 60 days without repository activity. Check the Actions page periodically and re-enable as needed. See [GitHub schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

This is about 96 runs per day. Review your account's Actions usage and billing limits. Disable the workflow when it is no longer needed. Enable GitHub notifications for failed workflows: page/SMTP errors are reported as failed runs, not through a second email-alert channel.

All manual modes and scheduled checks share one concurrency group, so GitHub does not run them simultaneously. Pending runs may be replaced by newer pending runs; a check is not guaranteed for every interval. The workflow always checks out default-branch code, including for manual runs. Do not add a pull-request trigger that executes untrusted code with secrets.

## Run locally

From the project directory, create and activate a virtual environment:

```powershell
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python monitor.py --dry-run
```

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python monitor.py --dry-run
```

The dry run requires configured permission but no email secrets. For real checks, provide the three email environment variables through your local secret manager or prompt for them. In PowerShell, you can enter the password without displaying or saving it in command history:

```powershell
$env:GMAIL_USERNAME = Read-Host 'Sending email address'
$env:ALERT_TO = Read-Host 'Recipient email address'
$secret = Read-Host 'Gmail App Password' -AsSecureString
$env:GMAIL_APP_PASSWORD = [System.Net.NetworkCredential]::new('', $secret).Password
python monitor.py --test-email
python monitor.py
Remove-Item Env:GMAIL_APP_PASSWORD
```

On Bash:

```bash
read -r -p 'Sending email address: ' GMAIL_USERNAME
read -r -p 'Recipient email address: ' ALERT_TO
read -r -s -p 'Gmail App Password: ' GMAIL_APP_PASSWORD
echo
export GMAIL_USERNAME ALERT_TO GMAIL_APP_PASSWORD
python monitor.py --test-email
python monitor.py
unset GMAIL_APP_PASSWORD
```

Each invocation checks once and exits; it is not a background service. The default local state is `.local-state/state.json` and is ignored by Git. Override with `--state PATH` and/or `--config PATH`. Local and GitHub state are independent. Do not run multiple local processes against the same state file or schedule a second copy of the same monitor.

## Changing the target or resetting

Changing the URL, selected section, or ignored selectors deliberately fails until you reset: different targets must not be compared as if they were the same section. Review permission for the new page, update the configuration/variables, and manually run mode `reset-baseline`. Locally, use `python monitor.py --reset-baseline`. This fetches the current section, replaces the baseline and sends no change email. It intentionally discards any pending difference; use it only after reviewing the situation.

Corrupt state also fails rather than silently starting over. The explicit reset repairs it after a successful fetch. A state branch with a missing `state.json` fails ordinary runs; use the reset mode after review. Deleting the entire state branch causes the next successful check to be treated as a first run, with no change email.

## Delivery and storage limits

State advances only after Gmail accepts a change message. If SMTP fails, the next run compares against the old baseline and tries again if the change is still present. SMTP acceptance does not prove inbox delivery.

Email and GitHub commits are not one transaction. If Gmail accepts a message but the connection drops, the state write fails, or the state commit cannot be pushed, a future run can send a duplicate. This project favors retrying over silently marking an undelivered alert as handled; it does not promise exactly-once delivery. A changed page that reverts during an outage may never generate an alert.

Snapshot updates use an atomic local file replacement. GitHub state commits happen only for a new baseline or changed content. Branch history retains older snapshots; a highly changeable section creates a growing history. Emails include at most 6,000 characters of removed text and 6,000 characters of added text, with a notice if either list is truncated; comparison uses the full selected text, up to 100,000 characters. Changing only this email format does not require a baseline reset.

## Safety and access rules

- Use only non-restricted, public information pages you own or are authorized to monitor. Public availability and `robots.txt` permission alone do not establish authorization. Respect the site's terms and published automation limits.
- Do not configure nicotine/tobacco products, restricted-product deals, stock/restock alerts, purchase automation, or other age-restricted shopping. The permission flag is your acknowledgement, not a classifier that can verify a site's subject matter or rules.
- Do not bypass login, paywalls, CAPTCHA, age gates, IP blocks, or access controls. No browser impersonation, proxy rotation, login cookies, or credential forwarding is provided.
- The monitor identifies itself as `PermittedSectionMonitor/1.0`, checks `robots.txt` each run, and makes one page request when allowed. A robots 404 permits the technical check to continue only after your separate permission acknowledgement. Other non-200 robots responses, redirects, and disallow rules stop the run.
- The code respects parsed crawl-delay values and rejects reported delays/rates stricter than its nominal two-request-per-15-minute budget. It does not implement a persistent rate limiter across manual runs. Space manual checks appropriately and use a slower schedule if the site requires it. Do not reduce the interval below 15 minutes for this project.
- HTTP 401, 403, 429, and other errors stop the current run without retries or state updates. A scheduled run will try again later: if access is denied or rate limiting occurs, **disable the workflow and resolve permission/rate issues before resuming**. No automatic backoff across runs is implemented.
- Only HTTPS on port 443 is supported; certificates are verified. Redirects are refused, so review and configure the final URL manually. Private/local resolved addresses are rejected, and environment proxies/.netrc credentials are disabled. The DNS check reduces accidental private-network access; it is not a hardened defense against a hostile DNS-rebinding target. Configure only trusted URLs.
- Responses are limited to 2 MB, with connection/read timeouts and a download-duration check. A login or anti-bot page that returns HTTP 200 and happens to match a broad selector can still look like a change. Use a specific selector and inspect the initial baseline.
- Send alerts only to an address you control or whose owner has agreed to receive them. Keep repository write access limited to trusted collaborators, since repository code can access Actions secrets. Review dependency and action updates periodically; the workflow uses official action major-version tags.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Permission acknowledgement error | Review this README, then set the JSON boolean to `true` |
| Selector matches zero or several elements | Inspect the original page HTML and select one stable section; JavaScript-only content is unsupported |
| Robots, redirect or HTTP failure | Review the canonical URL and site rules; stop monitoring when access is denied |
| Gmail authentication fails | Verify the sender and App Password, 2-Step Verification, account eligibility and revocation status |
| State push fails | Review Actions write permissions and branch rules; duplicates are possible until persistence is fixed |
| No alert on first run | Expected: the first check creates the baseline; use `test-email` to validate delivery |
| Schedule does not run | Check the default-branch workflow, Actions enablement, inactivity disablement and usage limits |
| Too many change emails | Narrow the selector or exclude a timestamp/counter, then reset the baseline |

## Project files

```text
section-monitor/
  .github/workflows/monitor.yml
  .gitignore
  config.json
  monitor.py
  requirements.txt
  tests/test_monitor.py
  README.md
```

The tests mock all network and email operations. They exercise baseline/unchanged/changed behavior, failed-send retry, preservation on fetch/write failure, configuration changes, corrupt state, access restrictions, parsing and TLS SMTP setup. A local Git workflow smoke check was also performed when packaging. Live Gmail delivery and a hosted GitHub Actions run require your account setup and are not pre-verified.
