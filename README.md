# Internship Finder: AWS Lambda to Discord

This project scans every ATS endpoint in `sources.toml`, keeps only internship/co-op/student roles, assigns one primary discipline, and posts each new match to one of five Discord channels. It runs as an AWS Lambda function on an Amazon EventBridge Scheduler `rate(5 minutes)` schedule.

It does not build or publish a website. DynamoDB is the durable source of truth for jobs already sent.

## How it works

1. `jobfinder/sources.py` normalizes Greenhouse, Lever, Ashby, SmartRecruiters, Rippling, Workday, Eightfold, ClearCompany, Getro, GitHub-list, and USAJOBS results.
2. `jobfinder/match.py` filters internship-style titles, scores expanded discipline keywords, selects one primary category, extracts a requirements snippet, and conservatively detects explicit graduation dates/ranges.
3. `run.py` removes duplicate job IDs, ignores IDs already in DynamoDB, atomically claims each new match, and sends a Discord embed.
4. A successful webhook call changes the DynamoDB record to `delivered`. A failed call releases its claim so the next invocation retries it.

Sources can have `tier = 1`, `2`, or `3` in `sources.toml`. Tier 1 is checked every five minutes, tier 2 about every fifteen minutes, and tier 3 about hourly. Greenhouse descriptions are fetched only for unseen internship candidates, shared ATS hosts are throttled, and unchanged GitHub list revisions are skipped using revision metadata in DynamoDB.

The five categories and environment variables are:

| Category | Lambda environment variable |
|---|---|
| Software Engineering | `DISCORD_WEBHOOK_SOFTWARE` |
| Hardware / Embedded / Computer Engineering | `DISCORD_WEBHOOK_HARDWARE` |
| Mechanical Engineering | `DISCORD_WEBHOOK_MECHANICAL` |
| Aerospace Engineering | `DISCORD_WEBHOOK_AEROSPACE` |
| Finance / Info Systems / FinTech | `DISCORD_WEBHOOK_FINANCE` |

## 1. Create the Discord webhooks

For each destination channel in Discord:

1. Open **Edit Channel → Integrations → Webhooks**.
2. Choose **New Webhook**, give it a useful name, and select the channel.
3. Choose **Copy Webhook URL**.
4. Keep each URL private. Anyone with it can post to that channel.

## 2. Create the DynamoDB state table

Create the table in the same AWS Region you will use for Lambda:

1. Open **AWS Console → DynamoDB → Tables → Create table**.
2. Set **Table name** to `internship-finder-seen`.
3. Set **Partition key** to `job_id`, type **String**. Do not add a sort key.
4. Under table settings, choose **Customize settings**.
5. Select **Provisioned** capacity and set both read and write capacity to `5`. Disable auto scaling if the console offers it; fixed 5/5 is ample for this workload.
6. Keep the default encryption and choose **Create table**.

DynamoDB's always-free allowance currently includes 25 GB plus 25 provisioned read and 25 provisioned write capacity units. A 5/5 table fits within that allowance as long as other tables in the account do not consume it.

## 3. Package the Lambda code

Requirements: Python 3.13 and PowerShell.

From the repository root, run:

```powershell
python -m unittest discover -s tests -v
.\scripts\package-lambda.ps1
```

For a read-only live endpoint check before packaging, run:

```powershell
python verify.py --only "SpaceX" --only "Canva" --only "Rippling"
```

This command does not access DynamoDB and cannot post to Discord. A source marked `ok` returned a valid response; a source marked `ERR` needs its URL/board identifier updated. An `ok` source with zero jobs is reachable but simply has no public postings at that moment.

The script creates `internship-finder-lambda.zip`. Its root contains `run.py`, `sources.toml`, and the `jobfinder` package. AWS's managed Python runtime supplies `boto3`, so this project has no third-party package to install. Do not zip the repository's parent folder; `run.py` must be at the ZIP root.

## 4. Create and configure the Lambda function

1. Open **AWS Console → Lambda → Functions → Create function**.
2. Choose **Author from scratch**.
3. Name it `internship-finder`.
4. Select the **Python 3.13** runtime and `x86_64` architecture.
5. Choose **Create a new role with basic Lambda permissions**, then create the function.
6. On the **Code** tab choose **Upload from → .zip file**, upload `internship-finder-lambda.zip`, and save.
7. Open **Runtime settings → Edit** and set the handler to `run.lambda_handler`.
8. Open **Configuration → General configuration → Edit**. Set memory to `256 MB` and timeout to `15 min`, then save. Increase memory if CloudWatch shows the scan is consistently slow.
9. Do not attach the function to a VPC unless that VPC has internet egress/NAT. The scraper and Discord webhook both require public HTTPS access.

### Add the environment variables

Under **Configuration → Environment variables → Edit**, add:

```text
DYNAMODB_TABLE=internship-finder-seen
DISCORD_WEBHOOK_SOFTWARE=<software channel URL>
DISCORD_WEBHOOK_HARDWARE=<hardware channel URL>
DISCORD_WEBHOOK_MECHANICAL=<mechanical channel URL>
DISCORD_WEBHOOK_AEROSPACE=<aerospace channel URL>
DISCORD_WEBHOOK_FINANCE=<finance channel URL>
```

Optional variables:

| Name | Default | Purpose |
|---|---:|---|
| `JOB_SCOPE` | `internships` | Set to `all` to include non-internship jobs. |
| `MAX_DESCRIPTION_LOOKUPS` | `100` | Maximum missing ATS descriptions fetched per invocation. |
| `MAX_JOBS_PER_RUN` | `40` | Notification cap; overflow remains unseen and is sent on later runs. |
| `USAJOBS_API_KEY` | none | Enables USAJOBS when paired with `USAJOBS_EMAIL`. |
| `USAJOBS_EMAIL` | none | Email registered for the USAJOBS API key. |
| `GITHUB_TOKEN` | none | Optional. Raises the GitHub API rate limit for unchanged-list revision checks. No repository write permission is needed. |

Lambda encrypts environment variables at rest, but users who can view the function configuration may be able to read them. Restrict IAM access to the function.

### Grant DynamoDB access

1. In Lambda, open **Configuration → Permissions**.
2. Select the execution role name to open it in IAM.
3. Choose **Add permissions → Create inline policy → JSON**.
4. Paste the policy below, replacing `REGION` and `ACCOUNT_ID` with the table's Region and your 12-digit AWS account ID:

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

5. Choose **Next**, name it `InternshipFinderDynamoState`, and create the policy.

The basic Lambda role already supplies CloudWatch Logs permissions.

## 5. Seed the initial state and test

Without a baseline, the first normal run treats every current posting as new. To start with only jobs discovered after deployment:

1. Open the Lambda **Test** tab and create an event named `seed-current-jobs`.
2. Use this JSON:

```json
{
  "seed_only": true
}
```

3. Run the test once. It records all current eligible postings in DynamoDB without calling Discord.
4. Confirm the returned summary has a nonzero `seeded` value. A source can appear in `failed_sources` without aborting the scan; for example, USAJOBS is skipped until its two credentials exist.
5. Create a separate `test-webhooks` event containing `{"test_webhooks": true}`. Run it once and confirm that each of the five channels receives exactly one clearly labeled test embed.
6. Change the test event to `{}` and run it again. Normally it delivers zero because the current jobs are now seeded. Check **Monitor → View CloudWatch logs** for the full summary.

If you want all currently open jobs posted to Discord, skip the seed event. The notification cap sends the backlog over multiple invocations.

## 6. Add the EventBridge five-minute trigger

1. On the Lambda function page, choose **Add trigger**.
2. Select **EventBridge Scheduler** (shown as **Scheduler** in some console views).
3. Set a schedule name such as `internship-finder-every-5-minutes` and use the `default` schedule group.
4. Select **Recurring schedule** and enter `rate(5 minutes)`.
5. Use `{}` as the JSON payload. Do not use the `seed_only` payload for the recurring schedule.
6. Turn **Flexible time window** off if that option is shown.
7. Choose **Create a new role for this schedule**, give the role a name, and choose **Add**.
8. In **EventBridge Scheduler → Schedules**, verify the schedule is **Enabled**.

EventBridge Scheduler invokes Lambda asynchronously. Confirm operation after five minutes in the Lambda CloudWatch log stream and in the DynamoDB table.

At a five-minute interval there are about 8,640 scheduled invocations in a 30-day month, well below the current EventBridge Scheduler and Lambda request free allowances. Lambda's compute allowance is duration-based: at 1,024 MB, 400,000 GB-seconds covers an average of about 46 seconds per invocation at this frequency. Monitor **Billing → Free Tier** and the Lambda `Duration` metric because unusually slow ATS endpoints, other workloads in the account, logging, or data transfer can still create charges.

## Updating sources or code

`sources.toml` is the single master source list. Edit it in GitHub whenever you add or remove a company. A GitHub edit does not automatically modify an already-uploaded Lambda ZIP, so after merging the change:

1. Pull the repository locally.
2. Run `.\scripts\package-lambda.ps1` again.
3. Upload the new ZIP from the Lambda **Code** tab.

The included GitHub Actions workflow is CI only: it runs unit tests after pushes and pull requests. It has no Discord webhook or AWS credentials, does not scrape on a schedule, and cannot post jobs. Production scheduling happens exclusively in EventBridge. You can delete the CI workflow if you do not want automatic test checks; doing so does not affect Lambda.

## What is needed to deploy

- Five private Discord webhook URLs, one for each category.
- The AWS Region where Lambda and DynamoDB will live.
- An AWS account with permission to create/configure one Lambda function, one DynamoDB table, one EventBridge schedule, and the small IAM roles described above.
- Optional USAJOBS API key and registered email. Every other configured source works without credentials.

Do not commit webhook URLs or AWS credentials to this repository. Put webhook URLs in Lambda environment variables. If someone else performs the deployment, grant narrowly scoped AWS access rather than sharing a root-account password or long-lived root key.

## Validation checklist

1. Run `python -m unittest discover -s tests -v` to validate filtering, routing, adapters, tier scheduling, and delivery state behavior without network calls.
2. Run `python verify.py --only "Company Name"` to validate an actual configured endpoint without AWS or Discord.
3. Invoke Lambda once with `{"seed_only": true}` and confirm the summary, CloudWatch source lines, and `seeded` records in DynamoDB.
4. Invoke Lambda with `{"test_webhooks": true}` and confirm one test embed reaches each Discord channel.
5. Invoke Lambda with `{}`. Immediately after seeding it should normally deliver zero jobs.
6. Confirm the EventBridge schedule shows a successful invocation and a new Lambda log entry after five minutes.

There is no HTML renderer, `docs/` output, GitHub Pages deployment, or website publishing step in this version. Discord is the only presentation destination.

## Operational notes

- Discord delivery is effectively at-least-once. Conditional DynamoDB claims prevent routine duplicates, including overlapping Lambda runs. Like any webhook system, a crash in the tiny interval after Discord accepts a message but before DynamoDB records completion can produce one duplicate after the 15-minute claim expires.
- A job that matches more than one category goes only to the highest-scoring primary category.
- Discord embeds use a clickable position title and show Company, Graduation, Location, Category, Posted date, requirements, and an apply link. Graduation displays `?` when the posting does not explicitly provide a graduation date or range.
- Unmatched jobs are stored with an `ignored` state so they do not consume description lookups and DynamoDB reads on every five-minute poll.
- Webhook URLs are never logged. Delete and recreate a Discord webhook immediately if its URL is exposed.
