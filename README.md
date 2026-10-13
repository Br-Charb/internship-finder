# Internship Finder: AWS Lambda to Discord

This project checks internship and co-op postings, keeps United States roles posted in the last 14 days, assigns one primary discipline, and sends each new match to one of five Discord channels. AWS Lambda runs it from an EventBridge Scheduler `rate(5 minutes)` schedule.

It does not build or publish a website. Discord is the only presentation layer, and DynamoDB only stores job IDs and small operational metadata.

## Current coverage

`sources.toml` is the single deployed source catalog. It currently contains:

- 288 direct company career boards, including the complete supported Internmaxx company registry plus additional aerospace and defense sources
- the Simplify internship list and a Getro job feed
- optional USAJOBS support when API credentials are configured
- Greenhouse, Lever, Ashby, SmartRecruiters, Rippling, Workday, Eightfold, ClearCompany, and company-specific adapters

The catalog's company-sector labels currently include 69 software, 47 AI, 35 quant, 27 fintech, 23 aerospace, 17 entertainment, 16 banking, 7 chips/hardware, 4 health, 1 consulting, and 42 other direct sources. These are source-sector labels, not routing limits: every posting from every source can be routed to any of the five Discord categories.

Tier 1 sources are checked every five minutes. Tier 2 sources are distributed across three five-minute slots, so each is checked every fifteen minutes. Tier 3 sources are distributed across twelve slots, so each is checked hourly. This avoids trying to fetch the entire catalog at once.

## Processing and Discord routing

1. `jobfinder/sources.py` and `jobfinder/custom_sources.py` normalize all supported job boards.
2. `jobfinder/match.py` keeps internship/co-op/student roles, requires a recognizable US location, and rejects postings with a supplied date older than `MAX_POSTING_AGE_DAYS`.
3. The matcher scores Software, Hardware/Embedded/Computer Engineering, Mechanical, Aerospace, and Finance/Information Systems/FinTech keywords. A multi-category job goes only to its highest-scoring primary category.
4. `run.py` checks DynamoDB, atomically claims an unseen job, and posts a Discord embed with its clickable position title, company, graduation range (`?` when not stated), location, category, posted date, requirements snapshot, and apply link.
5. Successful delivery changes the DynamoDB record to `delivered`. Rejected jobs are remembered as `ignored`, and a failed Discord call releases its claim for retry.

If an ATS supplies no posting date, the job is retained because the scraper cannot prove that it is stale. Correct initial seeding prevents those already-open jobs from being announced as new.

## Discord webhooks

Create one webhook in each of the five job channels and an optional sixth channel for system health:

| Destination | Lambda environment variable |
|---|---|
| Software Engineering | `DISCORD_WEBHOOK_SOFTWARE` |
| Hardware / Embedded / Computer Engineering | `DISCORD_WEBHOOK_HARDWARE` |
| Mechanical Engineering | `DISCORD_WEBHOOK_MECHANICAL` |
| Aerospace Engineering | `DISCORD_WEBHOOK_AEROSPACE` |
| Finance / Info Systems / FinTech | `DISCORD_WEBHOOK_FINANCE` |
| Source and pipeline health | `DISCORD_WEBHOOK_HEALTH` |

In Discord, open **Edit Channel → Integrations → Webhooks → New Webhook**, select the intended channel, and copy its URL. Keep webhook URLs private and rotate one immediately if it is exposed.

When the health webhook is configured, it receives:

- one alert after a source fails three consecutive checks
- one alert when a historically nonempty source unexpectedly returns zero jobs three consecutive times
- a recovery message when the source works again
- one daily heartbeat proving that Lambda, DynamoDB, and the health webhook are still connected

The scraper cannot send an alert if Lambda or EventBridge stops completely. A missing daily heartbeat is the Discord-side warning for that case; a CloudWatch alarm can also provide independent email notification.

## DynamoDB state

Create `internship-finder-seen` in the same AWS Region as Lambda with partition key `job_id` of type **String** and no sort key. No external database account or database server is required.

Each job is a small item whose state is `seeded`, `ignored`, `pending`, or `delivered`. The same table stores small source-health counters and list revision markers. It does not store full job descriptions.

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
DISCORD_WEBHOOK_FINANCE=<finance channel URL>
```

Recommended environment variables:

```text
DISCORD_WEBHOOK_HEALTH=<health channel URL>
MAX_POSTING_AGE_DAYS=14
```

Other optional variables:

| Name | Default | Purpose |
|---|---:|---|
| `JOB_SCOPE` | `internships` | Set to `all` only if full-time jobs are also wanted. |
| `MAX_DESCRIPTION_LOOKUPS` | `100` | Maximum missing descriptions fetched per invocation. |
| `MAX_JOBS_PER_RUN` | `40` | Delivery cap; overflow remains unseen for later runs. |
| `HEALTH_FAILURE_THRESHOLD` | `3` | Consecutive bad checks before a source alert. |
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

## Package and test locally

From the repository root in PowerShell:

```powershell
python -m unittest discover -s tests -v
.\scripts\package-lambda.ps1
```

This creates `internship-finder-lambda.zip`, with `run.py` at its root. AWS supplies `boto3`; there are no third-party runtime packages.

To check real public endpoints without touching AWS, DynamoDB, or Discord:

```powershell
python verify.py --only "SpaceX" --only "Google" --only "Apple"
```

An `ok` source returned a valid response. `ok` with zero jobs means the board was reachable but had no public results. `ERR` means its adapter or endpoint needs attention.

## Safe upgrade and baseline procedure

Use this sequence when deploying the expanded catalog. It prevents all currently open jobs at newly added companies from flooding Discord.

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

For the original five job webhooks, the separate `{"test_webhooks": true}` event sends one clearly labeled test to every job channel.

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
4. The health test appears in the sixth Discord channel.
5. A normal `{}` invocation finishes without delivery failures.
6. EventBridge creates a fresh invocation after five minutes.
7. One daily health heartbeat appears in Discord.

There is no HTML renderer, `docs/` site output, GitHub Pages deployment, or website publishing step.
