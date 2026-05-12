# Responsible AI Seed Principles — Agent Instructions

This document tells coding agents (Claude Code, Cursor, Copilot, and similar) how to behave when working in this repository. It operationalizes UC's Responsible AI Principles as concrete rules, not ideals to aspire to. Follow these rules in every task: planning, code, tests, docs, comments, and commit messages.

If a task would require violating one of these rules, stop and surface the conflict before proceeding. Do not silently work around it.

---

## 1. Appropriateness

Not every problem needs AI. Before adding an AI-powered component, justify it.

**Rules:**
- When asked to add an AI feature, first state in one or two sentences what problem it solves and who is affected. If you cannot, stop and ask.
- Prefer the simplest solution that meets the requirement. If a rules-based, deterministic, or non-ML approach would work, propose it before reaching for a model.
- If the feature affects high-stakes decisions (admissions, hiring, financial aid, discipline, health, immigration status, eligibility for services), flag the appropriateness question explicitly in the PR description. Do not merge without a human decision recorded.
- Do not add LLM calls, embeddings, or model inference inside hot paths "because we might want it later." Add them when there is a current, justified use.

**When you generate a PR or commit:**
- Include a one-line rationale for any new AI dependency, model call, or AI-driven branch of logic.

---

## 2. Transparency

People interacting with this system should know when AI is involved and be able to understand, contest, and seek remedy for AI-driven outcomes.

**Rules:**
- Any user-facing surface that produces AI-generated content, recommendations, or decisions must visibly indicate that AI is in use. Do not remove or obscure such indicators.
- Log every AI-driven decision that affects a user with: input summary, model and version, prompt or template ID, output, and timestamp. Logs must be retrievable by the affected user's identifier where lawful.
- Every AI-driven user-facing decision must have a documented appeal path (a human contact, a review endpoint, or a documented override mechanism). If the appeal path does not exist, do not ship the feature.
- Do not invent capabilities in marketing copy, tooltips, or help text. If a feature uses an LLM with known failure modes, say so in plain language.

**When you generate code:**
- Name AI-touching functions and modules so the AI involvement is obvious from the call site (`generateSummaryWithLLM`, not `getSummary`).
- Add a comment at the top of any module that calls a model: what model, what for, what the failure mode is, what the fallback is.

---

## 3. Accuracy, Reliability, and Safety

AI features must work as intended across their full lifetime, not just at launch.

**Rules:**
- Every model-backed feature ships with: an evaluation set, a measured baseline, and a documented acceptance threshold. No evals, no merge.
- Add monitoring for accuracy drift, latency, error rate, and cost per call. Wire alerts to a real human owner, not a shared inbox.
- Define and implement a fallback for every model call. When the model fails, times out, or returns low-confidence output, the system must degrade gracefully to a documented behavior (cached result, human queue, neutral default). Never silently return fabricated content as authoritative.
- Treat prompts and prompt templates as code: version them, code-review them, and reference them by ID in logs.
- Do not catch and discard model errors. Surface them, log them, and route them to the fallback.

**When you generate code:**
- Set explicit timeouts, retries, and token limits on every external model call.
- Write tests for the failure paths (empty output, malformed JSON, refusal, timeout, rate limit) before the happy path.

---

## 4. Fairness and Non-Discrimination

Bias assessment is continuous, not a one-time gate.

**Rules:**
- For any feature whose outputs vary across users, include a fairness evaluation in the eval set: measure performance across relevant demographic or contextual slices and record the disparities.
- Do not deploy a feature where measured disparity exceeds the documented threshold for that feature without a recorded human decision.
- Do not use protected attributes (race, ethnicity, national origin, religion, sex, gender identity, sexual orientation, disability status, age, veteran status) as model inputs unless there is a documented, lawful, narrowly-scoped reason. Proxies for protected attributes get the same scrutiny.
- Re-run fairness evals on model upgrades, prompt changes, and data pipeline changes. Treat any of those as a release that requires re-evaluation.

**When you generate code:**
- Make demographic slicing a first-class part of the eval harness, not an afterthought. If the eval harness doesn't support it, add the support before adding the feature.

---

## 5. Privacy and Security

Privacy and security are architectural constraints. They shape what gets built, not what gets reviewed at the end.

**Rules:**
- Apply data minimization at the source. Do not send fields to a model that the model does not need to complete the task. Strip PII and sensitive identifiers before any external API call unless there is a documented reason and a documented data agreement covering that use.
- Do not log raw prompts or completions that contain personal data without redaction. Hash or tokenize user identifiers in logs.
- Do not send UC data (student records, employee records, health information, research data under restricted-use agreements) to a third-party model endpoint without confirming the data classification is permitted under UC IS-3 and any applicable DUA, BAA, or contract terms.
- Default to the most restrictive privacy setting available on every third-party service (no training on inputs, no retention beyond session, zero data retention where offered).
- Require explicit user consent for any AI feature that processes personal data beyond the immediate task. Consent must be revocable.
- Never hardcode secrets, API keys, or credentials. Use environment variables or the project's secret manager. Reject any PR that adds a secret in plaintext.

**When you generate code:**
- Show the data path in comments at the entry point of any function that touches personal data: where it comes from, where it goes, what gets stripped, what gets logged.
- Default `retention=0`, `train_on_input=false`, and equivalent flags on every third-party model client.

---

## 6. Human Values

People retain agency over decisions that affect them. AI assists; it does not replace human judgment in consequential matters.

**Rules:**
- For any AI-driven decision that materially affects a person, provide a mechanism for that person (or a human acting on their behalf) to understand the basis of the decision, contest it, and obtain human review.
- Do not implement features that nudge, manipulate, or apply behavioral pressure without informed user awareness. Persuasive design that obscures its mechanics is out of scope for this project.
- Where the system makes a recommendation, the user must be able to ignore it without penalty or degraded service.
- For any feature touching civil rights contexts (speech, association, religious expression, due process, equal access to services, immigration status, protected activity), document the rights analysis in the PR. If you cannot articulate the rights implications, stop and escalate.

**When you generate code:**
- Build override and opt-out paths first, alongside the primary path. Do not defer them.

---

## 7. Shared Benefit

A system that works well for some users at the expense of others fails this principle regardless of its aggregate metrics.

**Rules:**
- Evaluate features against the users least well-served by the existing system, not the median user. If the feature improves average performance while worsening outcomes for an underserved group, that is a failure, not a tradeoff.
- Consider accessibility from the start: screen reader compatibility, keyboard navigation, low-bandwidth conditions, non-English language needs, and devices typical of the affected population.
- Account for environmental cost. Prefer smaller models, cached results, and batched inference where they meet the requirement. Document the cost-per-use of expensive model calls in the PR.
- Do not optimize for engagement metrics that conflict with user wellbeing or the institution's mission.

**When you generate code:**
- Include accessibility checks in the test suite for any user-facing change. Do not gate them behind a separate "a11y pass" that gets skipped.

---

## 8. Accountability

Accountability flows to the humans who build and deploy these systems. It is not diffused by the complexity of the stack or the involvement of vendors.

**Rules:**
- Every AI-driven feature has a named human owner recorded in the repository (CODEOWNERS, a service catalog entry, or an equivalent). "The team" is not an owner.
- Vendor and third-party model use does not transfer responsibility. Document what the vendor does, what we do, and where the boundary is. Review vendor terms before integration and on renewal.
- Maintain an audit trail sufficient to reconstruct any AI-driven decision that affected a user: inputs, model version, prompt version, output, downstream action, and timestamp.
- When something goes wrong, document it. Keep a postmortem record for AI-related incidents and feed the findings back into evals, prompts, and monitoring.

**When you generate code:**
- Add or update CODEOWNERS for any new AI-touching directory in the same PR that introduces it.
- Add an entry to the AI feature inventory (or create one if it does not exist) for any new model-backed capability: name, owner, model, purpose, data classification, fallback, eval location.

---

## How to Use This File

- Treat this as binding for all work in this repository. It supersedes generic best-practice defaults where they conflict.
- When a task is ambiguous against these rules, ask before proceeding. Do not infer permission from silence.
- When proposing changes to this file itself, open a PR with the rationale. Do not edit it as part of unrelated work.

## Scope and Authority

These principles derive from the University of California Responsible AI Principles. They apply to all code, prompts, models, and integrations in this repository. They apply equally to production systems, prototypes, and internal tools, with proportionate rigor.
