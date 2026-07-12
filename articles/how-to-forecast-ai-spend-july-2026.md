# How to Forecast AI Spend — July 2026

*A practical guide to constructing an AI budget you can defend to finance and adjust when reality moves.*

---

## Why this is hard (and why 2026 is different)

Forecasting AI spend is not like forecasting SaaS spend. A SaaS line item is a seat count times a price. AI spend is a *usage* curve multiplied by a *unit price* that has been falling for two years, layered on top of infrastructure you may or may not own, feeding features whose adoption you can only partly predict.

By mid-2026, three things have changed the game:

1. **Token prices keep falling, but your token *volume* keeps rising faster.** Per-token costs on frontier and mid-tier models have dropped materially year over year, yet most teams' bills went *up*. The reason is simple: cheaper tokens unlock more use cases, longer context windows, agentic loops that call the model many times per task, and reasoning models that spend tokens "thinking." Do not budget on price alone — budget on **price × volume × call-multiplier**.
2. **Agents changed the unit of work.** A single user action used to be one model call. In 2026 it's often a chain: plan, retrieve, call tools, reflect, retry. One "task" can be 5–50 calls. Your cost driver is no longer "requests"; it's "tokens per completed task."
3. **The build-vs-buy line moved.** Open-weight models are good enough for many workloads, and inference is cheap to self-host at scale — but only at scale. Below a real volume threshold, hosted APIs are still cheaper once you price in engineering time and GPU idle.

---

## The 5-layer AI cost stack

Budget each layer separately. They scale on different drivers and fail in different ways.

| Layer | What it is | Primary cost driver | Typical share of spend |
|---|---|---|---|
| **1. Model inference** | API calls to hosted models, or your own GPU inference | Tokens (in + out) × calls | 40–70% |
| **2. Infrastructure** | GPUs, vector DBs, orchestration, caching, egress | Compute-hours, storage, bandwidth | 10–30% |
| **3. Data & retrieval** | Embeddings, indexing, RAG pipelines, storage | Documents indexed, query volume | 5–15% |
| **4. Tooling & platforms** | Observability, eval, prompt management, guardrails, agent frameworks | Seats + usage tiers | 5–15% |
| **5. People** | ML/AI engineers, prompt/eval work, MLOps | Headcount | Often the largest true cost — budget it explicitly |

> **Rule of thumb:** if your forecast only has Layer 1 in it, it is wrong. The "hidden" layers (2–5) routinely double the sticker price of the tokens.

---

## Step 1 — Build the usage model, not the price model

Start from behavior, not from a price list. For each AI-powered feature, estimate:

```
monthly_cost  =  active_users
              ×  tasks_per_user_per_month
              ×  calls_per_task              (the agent multiplier)
              ×  avg_tokens_per_call
              ×  blended_price_per_token
```

Fill it in per feature, because the numbers differ wildly:

- A **chat assistant**: high tasks/user, moderate tokens, low call-multiplier.
- An **agentic workflow** (e.g. "resolve this ticket end to end"): low tasks/user, *high* call-multiplier, high tokens.
- A **batch/background job** (summarizing documents nightly): no users at all — driven by document volume.

Keep the drivers as named cells you can change. When someone asks "what if adoption doubles?" you change one number, not the whole model.

---

## Step 2 — Estimate tokens honestly

Most overruns come from underestimating tokens, not price. Watch for:

- **Context bloat.** Long system prompts, retrieved chunks, and conversation history all count as *input* tokens on every call. A 20-message conversation re-sends the whole history each turn unless you truncate or cache.
- **Reasoning/thinking tokens.** Reasoning models emit large volumes of intermediate output tokens that you pay for even though the user never sees them. Budget them as output.
- **Retries and self-correction.** Agents that verify their own work call the model again. Assume a real-world retry factor (1.2–2.0×) on agentic paths.
- **Output length.** Output tokens are usually priced higher than input. Cap `max_tokens` deliberately.

**Practical move:** instrument a representative sample of real traffic for two weeks and measure *actual* tokens per task. Extrapolated real data beats any spreadsheet guess.

---

## Step 3 — Apply the cost levers before you finalize

Your forecast should assume you'll use the standard optimizations, or explicitly note that you won't:

- **Prompt caching.** If your prompts share a large stable prefix (system prompt, retrieved corpus, few-shot examples), caching can cut input costs dramatically on repeated calls. This is one of the highest-ROI levers in 2026.
- **Model tiering / routing.** Send easy requests to a small cheap model and escalate only hard ones to a frontier model. A good router often cuts blended cost 40–70% with negligible quality loss.
- **Batch processing.** For non-interactive workloads, batch APIs typically offer a large discount versus real-time.
- **Fine-tuning / distillation.** A small fine-tuned model can replace a large general model for a narrow, high-volume task — trading one-time training cost for ongoing savings.
- **Truncation & summarization of context.** Don't send the whole history; summarize it.

Build two versions of the forecast: **naïve** (no optimization) and **optimized** (levers applied). The gap between them is your engineering roadmap.

---

## Step 4 — Add the layers people forget

- **Egress and inter-service bandwidth** can be surprisingly large for RAG systems moving embeddings and documents around.
- **Vector database** costs scale with vectors stored *and* queries per second — both, not either.
- **Observability & eval tooling** is not optional at production scale; price a real tier.
- **Idle GPU** if self-hosting. A reserved GPU costs the same whether it runs at 10% or 90% utilization. Self-hosting only wins if you keep utilization high.
- **Engineering time.** The largest line in many AI budgets is the people building and maintaining it. Put it in the forecast even if it lives in a different cost center — decisions change when it's visible.

---

## Step 5 — Model the ranges, not a single number

AI spend is uncertain, so forecast a band:

- **Base case:** expected adoption, optimizations shipped on schedule.
- **Low case:** slow adoption, or a feature gets cut.
- **High case:** viral adoption, agent loops longer than expected, optimizations slip.

Then commit to **guardrails**, not just a target:

- Hard and soft **budget alerts** per feature/team at the provider and gateway level.
- **Rate limits and per-user quotas** so a runaway loop or abuse can't produce an unbounded bill.
- A **kill switch / graceful degradation** path (fall back to a cheaper model or cached response) when spend spikes.

A forecast without cost controls is a wish. The controls are what make the number real.

---

## A worked mini-example

Suppose a support-automation agent:

- 8,000 active users, 6 resolved tickets each per month → 48,000 tasks/month
- 12 model calls per task (plan → retrieve → act → verify), with a 1.3× retry factor → ~15.6 effective calls
- ~2,500 input + 500 output tokens per call

That's roughly **48,000 × 15.6 × 3,000 ≈ 2.25B tokens/month**.

- **Naïve** at a mid-tier blended rate: a large monthly bill dominated by re-sent context.
- **Optimized:** prompt-cache the stable retrieval context, route the 60% simplest tickets to a small model, cap output length. Blended cost per task can fall by half or more.

The lesson isn't the exact dollar figure — it's that the **call-multiplier and context size**, not the headline token price, decide your bill.

---

## Step 6 — Make it a living forecast

- **Re-forecast monthly.** Prices, models, and your own usage all move fast. A quarterly-only cadence is too slow in this space.
- **Track cost per successful outcome** (per resolved ticket, per generated doc, per active user), not just total spend. Total spend rising while cost-per-outcome falls is *success*, not a problem.
- **Tie spend to value.** For each AI line item, know the revenue, retention, or cost-saving it drives. Budgets survive scrutiny when they're framed as ROI, not cost.
- **Keep a model-swap buffer.** Assume you'll migrate to a newer/cheaper model within the year and re-baseline when you do.

---

## A one-page checklist

- [ ] Usage model built per feature (users × tasks × calls × tokens × price)
- [ ] Real token measurements from sampled traffic, not guesses
- [ ] Agent call-multiplier and retry factor included
- [ ] Reasoning/thinking tokens budgeted as output
- [ ] All five cost layers present (inference, infra, data, tooling, people)
- [ ] Naïve **and** optimized forecast produced; gap = roadmap
- [ ] Cost levers assigned owners (caching, routing, batching, tiering)
- [ ] Low / base / high ranges modeled
- [ ] Budget alerts, quotas, and a kill switch in place
- [ ] Cost-per-outcome tracked and tied to value
- [ ] Monthly re-forecast scheduled

---

## The bottom line

In July 2026, the winning AI budget is not the one that bets on the lowest token price. It's the one that models **volume and the agent call-multiplier honestly, budgets all five cost layers, bakes in the optimization levers, and wraps the whole thing in guardrails and a monthly re-forecast.** Forecast the behavior, control the tail risk, and measure cost per outcome — the dollar figure will follow.
