# How to Forecast AI Spend — July 2026

*A practical guide to building an AI budget that covers the whole stack — not just LLM tokens — and projecting it forward with confidence.*

---

## The mistake almost everyone makes

Ask a team what their AI spend is and they'll quote you a model-provider bill. That number is real, but it's usually less than half the picture. By mid-2026, the LLM API line is just one column in a much wider ledger.

The organizations that get blindsided at renewal or board time are the ones who forecast **tokens** and ignore everything the tokens depend on: the vector stores that feed them, the databases and warehouses behind those, the orchestration that moves data between them, the developer tooling that keeps it all shippable, and the agentic workloads now embedded directly in the product.

Forecasting AI spend in 2026 is a **portfolio** problem, not a calculator problem. This guide walks the full stack, then shows how to pull it into a single projected view.

---

## The real AI cost stack — all of it

Budget each of these as its own line with its own driver. They scale differently, they're billed by different vendors, and they fail in different ways.

### 1. Model inference (LLM + specialty models)

The obvious layer: hosted API calls, or your own GPU inference. Driver is **tokens (in + out) × calls per task × task volume**. In 2026 the story isn't the falling per-token price — it's that agentic loops, long context, and reasoning tokens push *volume* up faster than price comes down. Don't forget non-LLM models either: speech, vision, rerankers, classifiers, and OCR all carry their own per-call costs.

### 2. Embeddings & vector storage

Every RAG system, semantic search feature, and recommendation surface runs on **embeddings**, and this is a cost most forecasts miss entirely:

- **Embedding generation** — a per-token cost every time you index a document *and* every time you embed a query. Re-indexing a large corpus on a schedule, or after a model upgrade, is a recurring (and lumpy) expense.
- **Vector storage** — priced on **vectors stored × dimensions**, plus **queries per second**. Both drivers matter; a large idle index still costs money, and a small index under heavy query load costs more than its size suggests.
- **Re-embedding events** — switching embedding models means re-embedding everything. Budget these as periodic step-changes, not smooth curves.

### 3. Databases, warehouses & knowledge graphs

AI features are data-hungry, and the storage/query layer is rarely one thing:

- **Operational databases** (Postgres, DynamoDB, etc.) backing the app and feature state.
- **Data warehouses / lakehouses** (Snowflake, BigQuery, Databricks, Redshift) for the analytics, training data, and feature pipelines behind the models — often billed on **compute credits + storage + scanned bytes**, which balloon under heavy AI-driven querying.
- **Knowledge graphs** (Neo4j and similar) increasingly used for structured retrieval and grounding — with their own storage and traversal-query costs.
- **Caches** (Redis and friends) that sit in front of all of the above to keep latency and repeat-query costs down.

Each has a different pricing model; treating "the database" as one line hides the fastest-growing costs.

### 4. Model & pipeline orchestration

The glue is not free:

- **Orchestration / workflow engines** (Airflow, Dagster, Prefect, Temporal, Step Functions) running ingestion, embedding, training, and eval pipelines.
- **Agent frameworks and serving layers** coordinating multi-step, multi-tool agent runs.
- **Compute for the pipelines themselves** — the boxes that run the DAGs, not just the models they call.
- **Data movement & egress** — moving embeddings, documents, and features between services is a real, often-surprising line, especially cross-region or cross-cloud.

### 5. Developer & MLOps tooling

The cost of *building and operating* AI, distinct from running it:

- **Observability & tracing** for LLM/agent runs (LangSmith, Langfuse, Arize, and the like).
- **Evaluation & testing** harnesses and the compute they consume (evals are themselves model calls).
- **Prompt/version management, feature stores, experiment tracking.**
- **Guardrails, PII/secrets scanning, and safety filtering** in the request path.
- **CI/CD and staging environments** that shadow production model traffic.

### 6. Agentic workloads embedded in the product

This is the fastest-moving line in 2026. When agents ship *inside the product*, cost stops tracking seats and starts tracking **autonomous work**:

- One user action can trigger a long chain of model calls, tool calls, retrievals, and retries — the **call-multiplier** effect.
- Background and scheduled agents consume tokens and infra with **no human in the loop to cap them**.
- Cost scales with product *usage and autonomy*, not with logins — which makes it the hardest layer to predict and the easiest to run away.

### 7. People

Frequently the single largest true cost: ML/AI engineers, data engineers, MLOps, plus the prompt, eval, and data work that never shows up on a vendor invoice. Put it in the forecast even when it lives in another cost center — decisions change when it's visible.

> **The point:** a forecast with only Layer 1 in it is off by a factor of two or more. The other layers are where the growth — and the surprises — live.

---

## Why breadth beats precision

You do not need a perfect token estimate for every feature. You need **every layer represented**, each with a driver you can flex. A budget that's roughly right across seven layers beats one that's precisely right on one and silent on the other six.

For each line, capture three things:

1. **The driver** — what makes it go up (tokens, vectors, scanned bytes, compute-hours, documents, agent runs, headcount).
2. **The owner** — which team or vendor it belongs to.
3. **The sensitivity** — how hard it moves when product usage doubles.

Some layers scale linearly with usage (inference, embeddings-per-query). Some are step functions (re-embedding, warehouse tier upgrades, reserved GPU). Some are fixed-ish until they suddenly aren't (tooling seats, orchestration compute). Label each so your projection behaves correctly when you flex the inputs.

---

## The optimization levers (assume you'll use them)

Model a **naïve** forecast and an **optimized** one; the gap is your engineering roadmap.

- **Prompt caching** for stable prompt prefixes — one of the highest-ROI levers.
- **Model routing / tiering** — cheap model for easy work, frontier model only when needed.
- **Batch processing** for non-interactive jobs.
- **Embedding hygiene** — dedupe, chunk sensibly, avoid needless re-indexing.
- **Warehouse discipline** — partitioning, materialized views, and query limits to stop scanned-bytes blowups.
- **Caching everywhere** — vector query caches, response caches, feature caches.
- **Right-sizing infra** — kill idle GPUs; self-host only where utilization stays high.

---

## The real solution: one aggregated view, projected forward

Here's the crux. The failure mode isn't any single wrong number — it's that the numbers **live in different places**. The model bill is with one vendor. The warehouse spend is in a finance export. Vector storage is on a card someone expensed. Orchestration compute is buried in a cloud bill. Agentic usage is a metric in the product analytics no one has mapped to dollars.

You cannot forecast what you can't see in one place. So the job has two moves:

1. **Aggregate** — pull every layer above into a single, normalized view: same time buckets, same units where possible (cost, and cost-per-outcome), tagged by feature, team, and driver. This is the hard, unglamorous part, and it's where most efforts stall.
2. **Project forward** — once it's aggregated, drive it off the handful of real drivers (usage growth, adoption curves, the agent call-multiplier, planned re-embeddings, model migrations) and produce **low / base / high** scenarios you can defend. Re-forecast monthly, because prices, models, and your own usage all move fast.

Wrap it in guardrails so the projection stays honest: per-layer budget alerts, per-user and per-agent quotas, and a graceful-degradation path when spend spikes. Track **cost per successful outcome** (per resolved ticket, per generated document, per active account), not just total spend — total spend rising while cost-per-outcome falls is success, not a problem.

---

## Where StackSpend fits

Building and maintaining that aggregated, forward-looking view by hand — across model providers, vector stores, warehouses, knowledge graphs, orchestration, developer tooling, and embedded agent usage — is exactly the work that stalls most finance and engineering teams. The data is scattered across a dozen vendors and billing models, and by the time you've reconciled it, it's a month stale.

**This is what StackSpend is built for.** StackSpend brings the whole AI stack into one aggregated view and projects it forward — unifying spend across every layer, attributing cost to features, teams, and drivers, and turning that into scenario-based forecasts you can take to finance. Instead of a token calculator that ignores 60% of the bill, you get the portfolio picture: what you're spending, what's driving it, and where it's headed — updated continuously rather than rebuilt in a spreadsheet each quarter.

Forecast the whole stack, aggregate it in one place, and project it forward. That's the discipline — and StackSpend is how companies operationalize it.

---

## A one-page checklist

- [ ] All seven layers represented — inference, embeddings/vectors, databases & warehouses & graphs, orchestration, dev/MLOps tooling, embedded agentic workloads, people
- [ ] Each line has a **driver**, an **owner**, and a **sensitivity**
- [ ] Real usage measurements from sampled traffic, not guesses
- [ ] Agent call-multiplier and retry factor included for embedded workloads
- [ ] Step-change events modeled (re-embedding, warehouse tier bumps, reserved GPU, model migration)
- [ ] Naïve **and** optimized forecasts produced; gap = roadmap
- [ ] Everything **aggregated into one normalized view**, tagged by feature/team/driver
- [ ] **Forward projection** with low / base / high scenarios
- [ ] Budget alerts, quotas, and a graceful-degradation path in place
- [ ] Cost-per-outcome tracked and tied to value
- [ ] Monthly re-forecast scheduled

---

## The bottom line

In July 2026, a credible AI budget isn't a token calculation. It's a **portfolio across the whole stack** — inference, embeddings and vector stores, databases and warehouses and knowledge graphs, orchestration, developer tooling, and the agentic workloads embedded in your product — pulled into a **single aggregated view and projected forward**. Get the breadth right, aggregate it in one place, project it with scenarios, and wrap it in guardrails. That's how you build a number you can defend and adjust when reality moves — and it's exactly the workflow StackSpend exists to run.
