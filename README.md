## SaaS Monitor - Terraform (Dev Env)

A small AWS stack to poll third‑party SaaS health endpoints (e.g., Microsoft Graph) on a schedule, preprocess results, and expose metrics/alarms for operations.

### Architecture
- EventBridge Scheduler → triggers Step Functions state machine on a cadence (default: every 5 minutes) with `{ appName }`.
- Step Functions (`observability-saas-monitor-sm`):
  - GetConfig: reads per‑app config from DynamoDB `ObservabilityAppConfig` by `appName`.
  - InvokePoller: invokes Lambda `observability-poller` to call the remote API using config (URL, headers, query, auth).
  - ChoiceAfterPoller: if `poll.ok` then invoke preprocess; else publish failure metric.
  - InvokePreprocess: invokes per‑app preprocess Lambda (name provided by config) to normalize/compute KPIs and emit metrics.
  - ReportFailure: emits `Observability/Poller:PollFailed` metric.
- Lambda functions:
  - `observability-poller` (Python 3.12):
    - Resolves API key from Secrets Manager (KMS‑encrypted), builds request, retries on transient codes, structured JSON logging, optional debug block in output.
  - `observability-preprocess-example-app` (Python 3.12):
    - Parses Graph response, normalizes status, computes KPIs, emits CloudWatch metrics.
- Secrets: `observability/saas/{saas_name}/api` (KMS key with rotation) for API tokens.
- CloudWatch:
  - Log groups: `/aws/lambda/observability-poller`, `/aws/lambda/observability-preprocess-example-app` (7 days retention).
  - Alarm: `AWS/States:ExecutionsFailed` on the state machine.

### Repo layout
```
envs/dev/
  lambda/
    poller/handler.py
    preprocess/example-app/handler.py
  main.tf
  variables.tf
  outputs.tf
```

### IAM roles and policies
- Lambda: `poller-lambda-role`
  - Trust: `lambda.amazonaws.com`
  - Managed: `service-role/AWSLambdaBasicExecutionRole`
  - Inline: secrets read and KMS decrypt limited to `${var.secret_path_prefix}/*` and stack KMS key
    - `secretsmanager:GetSecretValue`, `DescribeSecret`
    - `kms:Decrypt` (KMS key used by the secret)
- Lambda: `preprocess-example-app-lambda-role`
  - Trust: `lambda.amazonaws.com`
  - Managed: `service-role/AWSLambdaBasicExecutionRole`
  - Inline: `cloudwatch:PutMetricData` (Resource: `*`)
- Step Functions exec: `sfn-observability-exec`
  - `dynamodb:GetItem` on `ObservabilityAppConfig`
  - `lambda:InvokeFunction` on `observability-poller` (+ versions/aliases)
  - `lambda:InvokeFunction` on `arn:...:function:observability-preprocess-*`
  - `cloudwatch:PutMetricData` (for failure task)
- Scheduler exec: `scheduler-observability-exec`
  - `states:StartExecution` on the state machine
- KMS key: `secrets_key` (+ alias `alias/observability/secrets`)
  - Rotation enabled; 7‑day deletion window

### Configuration (DynamoDB item)
Table: `ObservabilityAppConfig` (PK: `appName`)
Expected attributes (DynamoDB AttributeValue shapes):
- `method (S)`, `url (S)`, `headers (M)`, `query (M)`, `timeout (N)`
- `secret_name (S)`, `json_key (S)`, `auth_header (S)`, `auth_prefix (S)`
- `retry (M)`: `{ max_attempts (N), backoff (N), retry_on (L of N) }`
- `preprocess_name (S)`: e.g., `observability-preprocess-example-app`

Example (Graph with OData expand):
```json
{
  "appName": {"S": "example-app"},
  "method": {"S": "GET"},
  "url": {"S": "https://graph.microsoft.com/v1.0/admin/serviceAnnouncement/healthOverviews"},
  "headers": {"M": {"Accept": {"S": "application/json"}}},
  "query": {"M": {"$expand": {"S": "issues($filter=startDateTime gt '2025-09-10T00:00:00Z')"}}},
  "timeout": {"N": "10"},
  "secret_name": {"S": "observability/saas/example-app/api"},
  "json_key": {"S": "api_key"},
  "auth_header": {"S": "Authorization"},
  "auth_prefix": {"S": "Bearer "},
  "retry": {"M": {"max_attempts": {"N": "3"}, "backoff": {"N": "1.5"}, "retry_on": {"L": [{"N":"429"},{"N":"500"},{"N":"502"},{"N":"503"},{"N":"504"}]}}},
  "preprocess_name": {"S": "observability-preprocess-example-app"}
}
```

### Lambda configuration
- Poller env vars:
  - `API_KEY_HEADER` (default: Authorization)
  - `API_KEY_PREFIX` (default: Bearer )
  - `LOG_LEVEL` (INFO/DEBUG)
  - `RETURN_DEBUG` (true/false) — embed sanitized request/attempts in response
  - `MAX_BODY_CHARS` (default 240000) — truncation guard to fit Step Functions 256KB limit
- Preprocess env vars:
  - `METRIC_NAMESPACE` (e.g., `Observability/ExampleApp`)

### Metrics
- Namespace `Observability/Poller`: `PollFailed` on failure branch
- Namespace `Observability/ExampleApp` (preprocess):
  - OverallAvailabilityPercent, ServicesOutageCount, ServicesDegradedCount, ServicesRecoveringCount, ServicesInvestigatingCount, CriticalScore

### Deployment
- Prereqs: Terraform >= 1.4, AWS credentials/profile for the target account.
- In `envs/dev`:
```
terraform init
terraform apply -auto-approve
```
- After deploy:
  - Put secret value into Secrets Manager at `${var.secret_path_prefix}/${var.saas_name}/api` (JSON or string; if JSON, set `json_key`).
  - Insert/update app config item in DynamoDB (`ObservabilityAppConfig`).
  - Verify the schedule triggers executions and metrics appear.

### Notes and best practices
- Prefer passing OData parameters via `query` map so the poller URL‑encodes safely.
- Step Functions uses `ResultPath` to preserve prior input; poll result placed at `$.poll.Payload`.
- Keep `RETURN_DEBUG=false` in production to reduce state size; use logs for deep debugging.
- Consider remote state and not committing local state files to git.

### Missing/optional project files
- `.gitignore` (added): ignores Terraform state, build zips, and local artifacts.
- Remote backend (optional): configure S3 + DynamoDB for Terraform state locking.
- CI pipeline (optional): lint/validate and plan/apply via GitHub Actions.
- S3 offload (optional): store large poll responses in S3 and pass an object URI when bodies exceed size limits.
