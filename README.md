# Internship Finder: AWS Lambda to Discord

This project checks internship and co-op postings, keeps United States roles posted in the last 14 days, assigns one primary discipline, and sends each new match to one of six Discord channels. AWS Lambda runs it from an EventBridge Scheduler `rate(5 minutes)` schedule.

It does not build or publish a website. Discord is the only presentation layer, and DynamoDB only stores job IDs and small operational metadata.

## Current coverage

`sources.toml` is the single deployed source catalog. It currently contains:

- 296 direct company career boards, including the supported Internmaxx company registry plus additional aerospace, defense, accounting, and major-technology sources
- the Simplify internship list and a Getro job feed
- optional USAJOBS support when API credentials are configured
- Greenhouse, Lever, Ashby, SmartRecruiters, Rippling, Workday, Oracle, Eightfold, ClearCompany, and company-specific adapters

The catalog's company-sector labels currently include 70 software, 46 AI, 34 quant, 27 fintech, 23 aerospace, 17 entertainment, 16 banking, 9 accounting, 7 chips/hardware, 4 health, 1 consulting, and 44 other direct sources. Accounting firms include PwC, EY, Deloitte, KPMG, BDO USA, Grant Thornton, Baker Tilly, Crowe, and Plante Moran. LinkedIn's own company internships are monitored separately; this does not scrape every job advertised by every company on LinkedIn. These labels are not routing limits: every posting from every source can be routed to any of the six Discord categories.

Tier 1 sources are checked every five minutes. Tier 2 sources are distributed across three five-minute slots, so each is checked every fifteen minutes. Tier 3 sources are distributed across twelve slots, so each is checked hourly. Meta is intentionally tier 3 because its endpoint repeatedly rate-limits more frequent AWS requests. This avoids trying to fetch the entire catalog at once.

## Processing and Discord routing

1. `jobfinder/sources.py` and `jobfinder/custom_sources.py` normalize all supported job boards.
2. `jobfinder/match.py` keeps internship/co-op/student roles, requires a recognizable US location, rejects postings with a supplied date older than `MAX_POSTING_AGE_DAYS`, and conservatively excludes clearly PhD-only roles. A role that accepts BS, MS, or PhD candidates is retained.
3. The matcher scores Software, Hardware/Embedded/Computer Engineering, Mechanical, Aerospace, Accounting, and Finance/Information Systems/FinTech keywords. A multi-category job goes only to its highest-scoring primary category.
4. `run.py` checks DynamoDB, atomically claims an unseen job, and posts a Discord embed whose clickable title contains both company and position for desktop and mobile previews, followed by location, category, posted date, requirements snapshot, and apply link.
5. Successful delivery changes the DynamoDB record to `delivered`. Rejected jobs are remembered as `ignored`, and a failed Discord call releases its claim for retry.

If an ATS supplies no posting date, the job is retained because the scraper cannot prove that it is stale. Correct initial seeding prevents those already-open jobs from being announced as new.

## Discord webhooks

Create one webhook in each of the six job channels and an optional seventh channel for system health:

| Destination | Lambda environment variable |
|---|---|
| Software Engineering | `DISCORD_WEBHOOK_SOFTWARE` |
| Hardware / Embedded / Computer Engineering | `DISCORD_WEBHOOK_HARDWARE` |
| Mechanical Engineering | `DISCORD_WEBHOOK_MECHANICAL` |
| Aerospace Engineering | `DISCORD_WEBHOOK_AEROSPACE` |
| Accounting | `DISCORD_WEBHOOK_ACCOUNTING` |
| Finance / Info Systems / FinTech | `DISCORD_WEBHOOK_FINANCE` |
| Source and pipeline health | `DISCORD_WEBHOOK_HEALTH` |

In Discord, open **Edit Channel → Integrations → Webhooks → New Webhook**, select the intended channel, and copy its URL. Keep webhook URLs private and rotate one immediately if it is exposed.

When the health webhook is configured, it receives:

- one alert after a source fails six consecutive checks
- one alert after twelve consecutive failures for sources prone to temporary HTTP 403/429 throttling, including Citadel, Citadel Securities, Microsoft, Meta, and LinkedIn
- no outage alert for a valid response containing zero jobs
- a recovery message only after two consecutive successful checks
- one daily heartbeat on the first successful scan at or after 8:00 AM Eastern, proving that Lambda, DynamoDB, and the health webhook are still connected

The scraper cannot send an alert if Lambda or EventBridge stops completely. A missing daily heartbeat is the Discord-side warning for that case; a CloudWatch alarm can also provide independent email notification.

## Per-company `/notify` commands

An optional Discord application adds three private slash-command responses:

- `/notify company:<name>` subscribes the member to that company in the current job channel.
- `/unnotify company:<name>` removes that channel-specific subscription.
- `/notifications` lists the member's subscriptions for the current job channel only.

The channel determines the category. For example, running `/notify Meta` in Software subscribes only to Meta posts routed to Software; it does not ping that member for a Meta Accounting post. Company names autocomplete from the direct-company entries in `sources.toml`.

Every company/category cohort gets one non-mentionable Discord role. A matching job contains one role mention, such as `@Notify: Meta / Software`, instead of mentioning every member separately. The bot is the only component allowed to ping these roles. If nobody subscribes to a company, its alerts continue through the original webhook with no ping. The six webhooks remain configured as a fallback and for ordinary unpinged alerts.

This feature uses a second Lambda named `internship-finder-commands`. It receives signed Discord interactions through a Function URL, responds immediately, and performs role changes in an asynchronous invocation. It does not need an EventBridge schedule, API Gateway, a continuously connected bot process, or a website.

## DynamoDB state

Create `internship-finder-seen` in the same AWS Region as Lambda with partition key `job_id` of type **String** and no sort key. No external database account or database server is required.

Each job is a small item whose state is `seeded`, `ignored`, `pending`, or `delivered`. The same table stores small source-health counters, list revision markers, notification-role mappings, Discord user IDs, and each user's selected company/category pairs. It does not store full job descriptions or Discord message content. No external database is required.

For a new table, **On-demand** capacity is easiest and has no fixed provisioned-capacity reservation, but AWS bills actual requests. Provisioned capacity can use DynamoDB's qualifying always-free provisioned allowance, but too-low capacity can throttle a bursty scan. AWS does not automatically stop services at a budget amount, so keep the billing budget and alerts enabled; a budget is a notification, not a hard spending cap. See the current [DynamoDB pricing page](https://aws.amazon.com/dynamodb/pricing/) before choosing a mode.

## Lambda configuration

Use:

- Runtime: Python 3.13
- Architecture: x86_64
- Handler: `run.lambda_handler`
- Memory: 512 MB
- Timeout: 15 minutes
- Region: the same Region as the DynamoDB table (currently `us-east-1`)
- VPC: none, unless the VPC has working public internet egress
- Provisioned concurrency: none; it is unnecessary for a scheduled scraper and incurs additional charges

Required environment variables:

```text
DYNAMODB_TABLE=internship-finder-seen
DISCORD_WEBHOOK_SOFTWARE=<software channel URL>
DISCORD_WEBHOOK_HARDWARE=<hardware channel URL>
DISCORD_WEBHOOK_MECHANICAL=<mechanical channel URL>
DISCORD_WEBHOOK_AEROSPACE=<aerospace channel URL>
DISCORD_WEBHOOK_ACCOUNTING=<accounting channel URL>
DISCORD_WEBHOOK_FINANCE=<finance channel URL>
```

Recommended environment variables:

```text
DISCORD_WEBHOOK_HEALTH=<health channel URL>
MAX_POSTING_AGE_DAYS=14
```

To activate role pings, also add these to the scheduled `internship-finder` Lambda. The values are numeric IDs copied from Discord, not webhook URLs:

```text
DISCORD_BOT_TOKEN=<Discord application bot token>
DISCORD_GUILD_ID=<Discord server ID>
DISCORD_CHANNEL_SOFTWARE=<software channel ID>
DISCORD_CHANNEL_HARDWARE=<hardware channel ID>
DISCORD_CHANNEL_MECHANICAL=<mechanical channel ID>
DISCORD_CHANNEL_AEROSPACE=<aerospace channel ID>
DISCORD_CHANNEL_ACCOUNTING=<accounting channel ID>
DISCORD_CHANNEL_FINANCE=<finance channel ID>
```

Other optional variables:

| Name | Default | Purpose |
|---|---:|---|
| `JOB_SCOPE` | `internships` | Set to `all` only if full-time jobs are also wanted. |
| `MAX_DESCRIPTION_LOOKUPS` | `100` | Maximum missing descriptions fetched per invocation. |
| `MAX_JOBS_PER_RUN` | `40` | Delivery cap; overflow remains unseen for later runs. |
| `HEALTH_FAILURE_THRESHOLD` | `6` | Consecutive ordinary failures before a source alert. |
| `HEALTH_RATE_LIMIT_THRESHOLD` | `12` | Consecutive checks before alerting on HTTP 403/429 or a known throttle-prone source. |
| `HEALTH_RECOVERY_THRESHOLD` | `2` | Consecutive successful checks before reporting recovery. |
| `HEALTH_HEARTBEAT_HOUR` | `8` | Earliest local hour for the once-daily heartbeat (0-23). |
| `HEALTH_TIME_ZONE` | `America/New_York` | IANA time zone used for the heartbeat, including daylight saving time. |
| `USAJOBS_API_KEY` | none | Enables USAJOBS when paired with `USAJOBS_EMAIL`. |
| `USAJOBS_EMAIL` | none | Email registered for the USAJOBS API key. |
| `GITHUB_TOKEN` | none | Raises GitHub API limits for list revision checks; no write permission is needed. |

Lambda encrypts environment variables at rest, but users allowed to view the function configuration may be able to read them. Restrict IAM access.

### DynamoDB IAM policy

The Lambda execution role needs the following inline policy. Replace `REGION` and `ACCOUNT_ID` with the table's values:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InternshipFinderState",
      "Effect": "Allow",
      "Action": [
        "dynamodb:BatchGetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem"
      ],
      "Resource": "arn:aws:dynamodb:REGION:ACCOUNT_ID:table/internship-finder-seen"
    }
  ]
}
```

The standard Lambda basic execution role already provides CloudWatch Logs access.

## Set up the Discord command application

### 1. Create and invite the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications), choose **New Application**, and name it `Internship Finder`.
2. On **General Information**, copy the **Application ID** and **Public Key**. Keep the public key; the command Lambda uses it to verify every request.
3. Open **Bot**, choose **Reset Token**, and copy the bot token into a password manager. Never commit or post it. No privileged gateway intents are needed.
4. Open **OAuth2 -> URL Generator**. Select the `bot` and `applications.commands` scopes. Grant **View Channels**, **Send Messages**, **Embed Links**, **Manage Roles**, and **Mention Everyone**. Open the generated URL and add the bot to the server.
5. In **Server Settings -> Roles**, move the bot's role above any `Notify:` roles. The application creates those roles non-mentionable so normal members cannot ping them.
6. In Discord **User Settings -> Advanced**, enable Developer Mode. Right-click the server and each of the six job channels to copy their numeric IDs.

Limit the bot to the six job channels with Discord channel permission overrides if desired. It needs View Channel, Send Messages, Embed Links, and Mention Everyone in those channels; Manage Roles is a server-level permission.

### 2. Package and create the command Lambda

From the repository root in PowerShell, run:

```powershell
.\scripts\package-discord-commands.ps1
```

This builds Linux x86_64 Python 3.13 dependencies and creates `internship-finder-commands.zip`. If Python reports that `pip` is unavailable, run `python -m ensurepip --upgrade` once and retry.

In AWS Lambda, create a second function:

- Function name: `internship-finder-commands`
- Runtime: Python 3.13
- Architecture: x86_64
- Handler: `discord_commands.lambda_handler`
- Memory: 256 MB or 512 MB
- Timeout: 30 seconds
- Region: the scraper and DynamoDB Region (`us-east-1`)
- Provisioned concurrency: none

Upload `internship-finder-commands.zip`, then add:

```text
DYNAMODB_TABLE=internship-finder-seen
DISCORD_PUBLIC_KEY=<application public key>
DISCORD_BOT_TOKEN=<bot token>
DISCORD_GUILD_ID=<server ID>
DISCORD_CHANNEL_SOFTWARE=<software channel ID>
DISCORD_CHANNEL_HARDWARE=<hardware channel ID>
DISCORD_CHANNEL_MECHANICAL=<mechanical channel ID>
DISCORD_CHANNEL_AEROSPACE=<aerospace channel ID>
DISCORD_CHANNEL_ACCOUNTING=<accounting channel ID>
DISCORD_CHANNEL_FINANCE=<finance channel ID>
```

Add this inline policy to the command Lambda's execution role. Replace `REGION`, `ACCOUNT_ID`, and the function name if different:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "NotificationSubscriptions",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem"
      ],
      "Resource": "arn:aws:dynamodb:REGION:ACCOUNT_ID:table/internship-finder-seen"
    },
    {
      "Sid": "RunCommandWorker",
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": "arn:aws:lambda:REGION:ACCOUNT_ID:function:internship-finder-commands"
    }
  ]
}
```

The existing scheduled Lambda already has the `dynamodb:BatchGetItem` permission needed to find role mappings. Add the bot token, server ID, and six channel IDs shown in the previous section to that function as well, rebuild `internship-finder-lambda.zip`, and upload it.

### 3. Connect Discord to the Function URL

1. In `internship-finder-commands`, open **Configuration -> Function URL** and create a URL with auth type **NONE**. CORS is not required. Public access is intentional: the handler rejects requests that do not have Discord's valid Ed25519 signature.
2. Copy the Function URL. In the Discord application, open **General Information**, paste it into **Interactions Endpoint URL**, and save. Discord sends a signed validation ping; a successful save proves the URL and public key work.
3. Register the three server commands from the repository root. The token prompt is hidden:

```powershell
$env:DISCORD_APPLICATION_ID = "<application ID>"
$env:DISCORD_GUILD_ID = "<server ID>"
python .\scripts\register-discord-commands.py
Remove-Item Env:DISCORD_APPLICATION_ID, Env:DISCORD_GUILD_ID
```

Because these are guild commands, they normally appear in that server immediately. Test `/notify Meta` inside the Software channel, then `/notifications`, then `/unnotify Meta`. The replies are ephemeral and visible only to the member running the command.

Do not use Lambda's ordinary **Test** button with an invented Discord request: signature validation will correctly reject it. Validation happens through the Developer Portal endpoint check and real slash commands. CloudWatch Logs for `internship-finder-commands` show worker errors without logging the bot or interaction token.

## Package and test locally

From the repository root in PowerShell:

```powershell
python -m unittest discover -s tests -v
.\scripts\package-lambda.ps1
.\scripts\package-discord-commands.ps1
```

These create `internship-finder-lambda.zip`, with `run.py` at its root, and `internship-finder-commands.zip`, with `discord_commands.py` at its root. AWS supplies `boto3`; the command ZIP additionally bundles PyNaCl for Discord signature verification.

To check real public endpoints without touching AWS, DynamoDB, or Discord:

```powershell
python verify.py --only "SpaceX" --only "Google" --only "Apple"
```

An `ok` source returned a valid response. `ok` with zero jobs means the board was reachable but had no public results. `ERR` means its adapter or endpoint needs attention.

## Safe upgrade and Accounting backfill

For the release that adds Accounting, use the sequence below. The category backfill has separate DynamoDB IDs, so it can deliver qualifying Accounting internships even if the original jobs were recorded during an earlier seed. It also baselines every newly added source to prevent unrelated old jobs from flooding the other channels.

1. Create the Accounting Discord channel and webhook. Add its URL to Lambda as `DISCORD_WEBHOOK_ACCOUNTING`.
2. Disable `internship-finder-every-5-minutes` in Amazon EventBridge Scheduler.
3. Build and upload the new ZIP, then wait for Lambda to show the successful update banner.
4. Run `{"test_webhooks": true}`. It sends one labeled test to each of the six job channels.
5. Create an `accounting-backfill` test event and run it for partition `0`, then change the index through `11`:

```json
{
  "backfill_category": "accounting",
  "seed_partition": 0,
  "seed_partition_count": 12
}
```

The backfill sends only Accounting jobs that have a parseable posting date within the last 14 days, pass the US-only and PhD-only filters, and have not already been sent by this backfill. If `deferred_by_limit` is greater than zero, rerun that same partition until it becomes zero. If `failed_sources` is nonempty, rerun the partition until it succeeds. DynamoDB makes every rerun safe.

6. After partitions `0` through `11` finish, invoke once with `{}`. A genuinely new posting that appeared during the upgrade may be delivered.
7. Re-enable the EventBridge schedule and confirm a new invocation after five minutes.

### Fresh installation or full-catalog baseline

Use this sequence for a brand-new DynamoDB table or a future large catalog expansion where no historical category backfill is wanted.

1. Open **Amazon EventBridge Scheduler → Schedules** and disable `internship-finder-every-5-minutes`.
2. Build `internship-finder-lambda.zip` using the command above.
3. In **Lambda → internship-finder → Code**, choose **Upload from → .zip file**, upload it, and save.
4. Confirm the handler, 512 MB memory, and 15-minute timeout.
5. Add `DISCORD_WEBHOOK_HEALTH` and `MAX_POSTING_AGE_DAYS=14` under **Configuration → Environment variables**.
6. Create a Lambda test event with `{"test_health_webhook": true}` and run it. The health channel should receive one green connection test.
7. Create a test event named `seed-expanded-catalog` and run it twelve times. Start with index `0`, then change only `seed_partition` through `11`:

```json
{
  "seed_only": true,
  "seed_partition": 0,
  "seed_partition_count": 12
}
```

Each result should show the matching `seed_partition`, a nonzero `sources_checked`, and `delivered: 0`. Re-running the same index is safe if it times out because DynamoDB writes are idempotent. Do not re-enable the schedule until all indexes `0` through `11` have completed.

8. Invoke once with `{}`. It should usually deliver zero; a genuinely new posting that appeared during the seed window can be delivered.
9. Re-enable the EventBridge schedule.
10. After at least five minutes, confirm a new Lambda invocation under **Monitor** and a normal JSON summary in CloudWatch Logs.

The separate `{"test_webhooks": true}` event sends one clearly labeled test to every configured job channel.

## EventBridge schedule

The recurring schedule uses:

- schedule expression: `rate(5 minutes)`
- target: the `internship-finder` Lambda function
- target payload: `{}`
- flexible time window: off
- schedule state: enabled after seeding
- retry: optional; DynamoDB claims make normal duplicate invocations safe

Five minutes is safe because catalog tiers spread the work across invocations. EventBridge Scheduler currently includes up to 14 million invocations per month in its free tier; this schedule makes about 8,640 monthly invocations. Lambda compute, DynamoDB requests, logs, data transfer, and any other AWS usage are separate, so billing alarms should remain enabled. See [EventBridge pricing](https://aws.amazon.com/eventbridge/pricing/) and [Lambda concurrency behavior](https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html).

## Updating the source catalog

To refresh the supported Internmaxx registry later:

```powershell
python .\scripts\import-internmaxx.py
python -m unittest discover -s tests -v
```

Review and commit the resulting `sources.toml`. The importer keeps locally configured sources not present upstream. A GitHub commit does not update the already-uploaded Lambda function: rebuild and upload the ZIP, then seed newly introduced boards before re-enabling the schedule.

The GitHub Actions workflow is CI only. It runs unit tests after pushes and pull requests; it has no AWS or Discord credentials, does not scrape on a schedule, and cannot send job alerts. Production scheduling exists only in EventBridge.

## Validation checklist

1. Unit tests pass locally.
2. Selected live endpoints pass `verify.py`.
3. All twelve seed partitions complete with `delivered: 0`.
4. The health test appears in the optional seventh Discord channel.
5. A normal `{}` invocation finishes without delivery failures.
6. EventBridge creates a fresh invocation after five minutes.
7. One daily health heartbeat appears in Discord at about 8:00 AM Eastern.
8. `/notify`, `/unnotify`, and `/notifications` work only in the six job channels, and a subscribed test company creates one `Notify:` role instead of individual mentions.

There is no HTML renderer, `docs/` site output, GitHub Pages deployment, or website publishing step.
