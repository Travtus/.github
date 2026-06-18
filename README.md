# .github

This is a repository for Travtus' workflow templates.

To include a new template, add it to the workflow-templates directory. This repository also has its own workflows
in its .github directory. This is for PR automation around the workflows themselves (e.g., automated comments). 

If you update this repository's template for automatic comments, then you should also update this repository's workflow
for automatic comments.

## Project configuration file

If you use `uv` to manage your Python projects, please copy `pyproject-uv.toml` to your project root and rename it to `pyproject.toml`.
This file contains the configuration for the `uv` tool, which is used to manage Python projects.

For other package management tools, like `pip`, `poetry`, you can use the `pyproject.toml` file as a reference to create your own configuration file.

## Reusable workflows

### `auto_assign_round_robin.yml` — PR reviewer assignment

Assigns PR reviewers from the `Platform` and `frontend` GitHub teams based on changed file extensions. The workflow is intended to be selected as an organization ruleset required workflow, so individual repositories do not need caller workflows.

**Routing:**
- `Platform`: `.py`, `.yaml`, `.yml`, `.sql`
- `frontend`: `.ts`, `.tsx`, `.js`, `.jsx`, `.css`, `.scss`

Mixed PRs receive one reviewer from each matching team. Reviewer selection is round-robin and tracked in one bot-managed issue comment in `Travtus/.github`; `counts` are audit-only and do not drive assignment. The workflow uses GitHub Actions `concurrency` with `cancel-in-progress: false` so simultaneous PRs queue instead of racing the same state comment.

**Secrets and variables:**
- `PLATFORM_ADMIN_APP_ID` repository or organization variable
- `PLATFORM_ADMIN_APP_PRIVATE_KEY` repository or organization secret
- `SLACK_BOT_TOKEN` repository or organization secret

`PLATFORM_ADMIN_APP_ID` must contain the GitHub App **client ID** because `actions/create-github-app-token@v3` uses `client-id`.

The GitHub App must be able to read team members, request PR reviewers, read issue comments, and update the state issue comment. Required permissions:
- Repository `Pull requests`: read/write
- Repository `Issues`: read/write
- Organization `Members`: read-only

Slack mentions use the selected GitHub user's public email from `GET /users/{username}` and Slack `users.lookupByEmail`. If the GitHub public email is not set, the GitHub reviewer is still assigned and Slack is skipped with a workflow warning.

Before enabling the workflow:
- Confirm Slack channel IDs in `.github/auto_reviewer_config.json`.
- Use the configured state issue: `https://github.com/Travtus/.github/issues/70`.
- If the workflow does not find the state comment, it creates one automatically.
- To create it manually, add one comment containing:

````markdown
<!-- auto-reviewer-state -->
```json
{
  "Platform": {
    "cursor": -1,
    "last_reviewer": null,
    "previous_order": [],
    "counts": {}
  },
  "frontend": {
    "cursor": -1,
    "last_reviewer": null,
    "previous_order": [],
    "counts": {}
  }
}
```
````

### `pr_size_check.yml` — PR size enforcement

Fails a PR when the number of changed lines exceeds **750** (configurable in the workflow). Designed to keep PRs reviewable and aligned with trunk-based development.

**Triggers:** `pull_request`, `pull_request_review`

Add the `large-pr-exception` label only when a PR cannot reasonably be split. When the label is present, the size failure is skipped. The workflow requests review from the Platform Admin team and fails until the PR has an approval from a member of that team.
The exception path does not wait or poll for approval, so it does not extend workflow runtime. If a later Platform Admin approval does not retrigger the required workflow, rerun the check or use a separate GitHub App/per-repo `pull_request_review` workflow to retrigger it.
Platform Admin approval is PR-scoped, not commit-scoped, so a subsequent rebase or synchronize event does not require a fresh approval unless that approval is dismissed or superseded by a later change-requested review from the same admin.

Configure the repository variable `PLATFORM_ADMIN_APP_ID` with the GitHub App client ID and secret `PLATFORM_ADMIN_APP_PRIVATE_KEY` for a GitHub App installation that can read org team membership and request PR reviewers.

**Excluded from the line count:**
- `**/*.md`
- `**/test/**`, `**/tests/**`, `**/unit_test/**`, `**/unit_tests/**`
- `**/alembic*/**`
- `**/open_api.yaml`
- `**/requirements*.txt`
- Lockfiles such as `uv.lock`, `poetry.lock`, `Pipfile.lock`, `package-lock.json`, `npm-shrinkwrap.json`, `yarn.lock`, `pnpm-lock.yaml`, `bun.lock`, `bun.lockb`, `Cargo.lock`, `Gemfile.lock`, `go.sum`, and `composer.lock`
- `pyproject.toml`
- `.github/workflows/**`
- Binary files (Git reports `-` in numstat)


### `run-alembic-migrations.yml` — Run Alembic migrations on ECS

Runs an Alembic command as a one-shot Fargate task using an existing ECS task definition (with a `containerOverrides` command). Waits for the task to finish, prints a CloudWatch log URL, and fails on non-zero exit.

**Triggers:** `workflow_call`, `workflow_dispatch`

**Inputs:**

| Name | Required | Default | Description |
|---|---|---|---|
| `ECS_CLUSTER_NAME` | yes | — | ECS cluster name |
| `TASK_DEF` | yes | — | Task definition family or ARN |
| `CONTAINER_NAME` | yes | — | Container name within the task definition |
| `SUBNET_IDS` | yes | — | Comma-separated private subnet IDs |
| `SECURITY_GROUP_ID` | no | `""` | Security group ID (omit to use VPC default) |
| `MIGRATION_COMMAND` | no | `upgrade head` | One of `upgrade head`, `downgrade -1`, `downgrade base` |
| `AWS_REGION` | no | `us-east-2` | AWS region |

**Secrets:** `AWS_OIDC_ROLE_ARN` (required) — IAM role assumed via OIDC.

**Example:**
```yaml
jobs:
  migrate:
    uses: Travtus/.github/.github/workflows/run-alembic-migrations.yml@main
    secrets:
      AWS_OIDC_ROLE_ARN: ${{ secrets.AWS_OIDC_ROLE_ARN }}
    with:
      ECS_CLUSTER_NAME: my-cluster
      TASK_DEF: my-service
      CONTAINER_NAME: app
      SUBNET_IDS: subnet-aaa,subnet-bbb
      SECURITY_GROUP_ID: sg-123
      MIGRATION_COMMAND: upgrade head
```

---

### `deploy_ecs_service_to_env.yaml` — Deploy ECS service (build → migrate → restart)

Orchestrates a full ECS service deploy for one environment by chaining three reusable workflows:

1. **build** — `build_and_deploy_docker_to_ecr.yaml` builds and pushes the Docker image to ECR.
2. **migrate** — `run-alembic-migrations.yml` runs Alembic migrations as a one-shot ECS task. Skipped if `RUN_MIGRATIONS: false`.
3. **restart** — `restart-ecs-services-template.yaml` forces a new deployment of the ECS service. Runs only if `build` succeeded and `migrate` succeeded or was skipped.

**Triggers:** `workflow_call`

**Inputs:**

| Name | Required | Default | Description |
|---|---|---|---|
| `ECR_REPO_NAME` | yes | — | ECR repository name |
| `DOCKER_CONTEXT` | no | `.` | Docker build context path |
| `ECS_CLUSTER_NAME` | yes | — | ECS cluster name |
| `TASK_DEF` | yes | — | Task definition family or ARN (used by migration) |
| `CONTAINER_NAME` | yes | — | Container name within the task definition |
| `SUBNET_IDS` | yes | — | Comma-separated private subnet IDs |
| `SECURITY_GROUP_ID` | no | `""` | Security group ID (omit to use VPC default) |
| `RUN_MIGRATIONS` | no | `true` | Whether to run Alembic migrations before restart |
| `MIGRATION_COMMAND` | no | `upgrade head` | One of `upgrade head`, `downgrade -1`, `downgrade base` |
| `AWS_REGION` | no | `us-east-2` | AWS region |

**Secrets:** `PAT_GITHUB`, `AWS_OIDC_ROLE_ARN`, `AWS_ACCOUNT` (all required).

**Example:**
```yaml
jobs:
  deploy-staging:
    uses: Travtus/.github/.github/workflows/deploy_ecs_service_to_env.yaml@main
    secrets:
      PAT_GITHUB: ${{ secrets.PAT_GITHUB }}
      AWS_OIDC_ROLE_ARN: ${{ secrets.AWS_OIDC_ROLE_ARN }}
      AWS_ACCOUNT: ${{ secrets.AWS_ACCOUNT }}
    with:
      ECR_REPO_NAME: my-service
      ECS_CLUSTER_NAME: staging-cluster
      TASK_DEF: my-service
      CONTAINER_NAME: app
      SUBNET_IDS: subnet-aaa,subnet-bbb
      SECURITY_GROUP_ID: sg-123
      RUN_MIGRATIONS: true
```
