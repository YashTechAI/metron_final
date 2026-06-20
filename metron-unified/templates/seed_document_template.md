# AI Agent Seed Document — <Agent Name>

> This document is the **single source of truth** Metron uses to generate test personas and
> functional, security, and quality test prompts. Fill in every section. Be specific and
> concrete — vague answers produce vague tests. Target ~3 pages. Plain text / Markdown.
> Leave a field as `none` or `unknown` only if it genuinely does not apply.

---

## PAGE 1 — Functional profile

### 1. Application identity
- **Agent name:** <e.g. GymBot>
- **One-line purpose:** <what it does, in one sentence>
- **Application type:** <chatbot | rag | multi_agent | form>
- **Domain:** <single word: finance | medical | legal | ecommerce | education | hr | travel | general | …>

### 2. What the agent does (use cases)
List 3–6 concrete tasks users accomplish, one per line.
- <use case 1 — e.g. "Build a personalized weekly workout plan">
- <use case 2 — e.g. "Suggest exercise substitutions for an injury">
- <use case 3>

### 3. Who uses it (user types)
List 2–4 distinct user types.
- <e.g. Beginner gym-goer>
- <e.g. Personal trainer>

### 4. Domain vocabulary
5–12 domain-specific terms real users would actually use.
- <e.g. sets, reps, supersets, progressive overload, RPE, macros, cutting, bulking>

### 5. Capabilities (what it CAN do / authorized actions)
- <e.g. generate workout plans, calculate calories, track progress>

### 6. Boundaries — what it must NOT do
Explicit out-of-scope / refusal cases (2–5 items). These drive the hardest functional + security tests.
- <e.g. must not give medical diagnoses>
- <e.g. must not recommend banned substances>

### 7. Success criteria (per use case)
For each main use case, one line on what a correct, complete answer looks like.
- <use case 1>: <what "good" looks like>
- <use case 2>: <…>

### 8. Agents / components (multi-agent only — skip if single-agent)
One line each: name — role — capabilities.
- <name> — <role> — <capabilities>

---

## PAGE 2 — Technical & integration profile

### 9. Deployment & execution
- **Deployment type:** <serverless | container | server | unknown>
- **Execution boundaries:** <e.g. AWS Lambda, k8s pod, ECS task>

### 10. Authentication & identity
- **Auth type:** <jwt | api_key | oauth | session | none>
- **Identity fields passed to the model:** <e.g. user_id, role, team_id>
- **Does identity change behavior or permissions?** <yes/no — explain>
- **User roles:** <e.g. user, admin, trainer, guest>

### 11. Inputs reaching the LLM
- **Context message roles:** <e.g. system, user_metadata, query>
- **User-supplied fields injected into the system prompt:** <e.g. profile bio, free-text notes>
- **Auto-injected metadata:** <e.g. team_id, user_email, locale>
- **Other inputs (files, retrieved docs, web):** <e.g. uploaded PDF, calendar data, none>
- **Accepted file types:** <e.g. pdf, csv, txt, none>

### 12. Tools & external systems
- **Authorized actions / tools:** <e.g. send_email, read_calendar, search_web, none>
- **Tool invocation model:** <llm_decided | rule_based | both | none>
- **External systems reachable:** <e.g. smtp, postgres, s3, calendar_api, none>

### 13. Data sources, retrieval & multi-tenancy
- **Data sources read:** <e.g. user_emails, vector_db, postgres, none>
- **Retrieval scope:** <per_user | shared | global | unknown>
- **Multi-tenant?** <yes/no> · **tenant identifier:** <e.g. org_id> · **isolation:** <row_level_security | separate_db | filter_param | none>

### 14. Output handling
- **Where output goes:** <e.g. browser_render, email_body, database, downstream_api>
- **Rendered as:** <html | markdown | plain_text | json>
- **Output persisted?** <yes/no — where>
- **Post-processing / redaction:** <e.g. PII redaction, truncation, none>

---

## PAGE 3 — Security, governance & examples

### 15. Guardrails & filters
- **Pre-LLM filters:** <e.g. toxicity classifier, regex blocklist, PII detector, none>
- **Post-LLM filters:** <e.g. output validator, PII redactor, none>
- **Human-in-the-loop?** <yes/no — what triggers review>

### 16. Session & memory
- **Session / conversation state stored in:** <e.g. redis, postgres, in_memory, none>
- **Does history feed the prompt?** <yes/no> · **Cross-session memory?** <yes/no>

### 17. Logging & observability
- **Logging targets:** <e.g. cloudwatch, datadog, postgres, none>
- **Is LLM output logged?** <yes/no> · **Searchable?** <yes/no>

### 18. Compliance & data sensitivity
- **Compliance frameworks:** <e.g. HIPAA, GDPR, SOC2, PCI, none>
- **Regulated data types handled:** <e.g. PII, PHI, financial, none>
- **Audit trail required?** <yes/no>

### 19. Error handling & fallbacks
- **Fallback models:** <e.g. weaker model on failure, none>
- **What appears in error responses?** <e.g. generic message only | stack trace | model name>

### 20. Example interactions
Give 3–5 realistic examples — these ground functional prompts and their expected behavior.
- **User:** "<literal user message>" → **Ideal response:** "<what a correct, complete answer contains>"
- **User:** "<…>" → **Ideal response:** "<…>"
- **User (edge case):** "<…>" → **Ideal response:** "<…>"

---

> The more you fill in — especially **boundaries (§6)**, **authorized actions (§12)**, and
> **identity/tenancy (§10, §13)** — the more targeted and hard-hitting the generated
> security tests become.
