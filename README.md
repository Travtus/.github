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

### `pr_size_check.yml` — PR size enforcement

Fails a PR when the number of changed lines exceeds **700** (configurable in the workflow). Designed to keep PRs reviewable and aligned with trunk-based development.

**Triggers:** `pull_request`, `workflow_call`

**Excluded from the line count:**
- `**/*.md`
- `**/test/**`, `**/tests/**`, `**/unit_test/**`, `**/unit_tests/**`
- `uv.lock`
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
