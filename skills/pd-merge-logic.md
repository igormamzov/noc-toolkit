# PagerDuty Incident Merge Logic

## Trigger Conditions

This sub-skill activates when:
- User asks to merge PagerDuty incidents
- User asks to find duplicate/related incidents
- User asks to clean up or consolidate PD incidents
- Keywords: "merge incidents", "merge PD", "find duplicates", "consolidate incidents", "smerdzhit", "смерджить"

## Purpose

Automates discovery and merging of related PagerDuty incidents that share the same root cause (same job/DAG name). Applies a deterministic priority system to select the best **target** (surviving) incident.

---

## Step 1: Fetch All Active Incidents

Fetch all triggered/acknowledged incidents assigned to the current user:

```bash
curl -s "https://api.pagerduty.com/incidents?user_ids[]=<USER_ID>&statuses[]=triggered&statuses[]=acknowledged&limit=100&sort_by=created_at" \
  -H "Authorization: Token token=<PD_TOKEN>" \
  -H "Accept: application/vnd.pagerduty+json;version=2"
```

- Use `strict=False` when parsing JSON (PD responses may contain control characters)
- If `more: true` in response, paginate with `offset` parameter

## Step 2: Extract Job Name from Title

Each incident title follows one of these patterns. Extract the **job name** and classify the **alert type**:

| Alert Type | Pattern | Priority | Example Title |
|---|---|---|---|
| Databricks batch job failed | `Databricks batch job <JOB_NAME> failed` | **1 (TARGET)** | `[ERROR] [DATABRICKS] Databricks batch job crowder-enr-category failed` |
| Monitor job failed | `Monitor job '<JOB_NAME>_prod' failed` | **2** | `Monitor job 'jb_edw_evt_attraction_genre_0014_prod' failed` |
| AirFlow DAG failed consecutively | `AirFlow DAG <JOB_NAME> has failed consecutively` | **3** | `[CRITICAL] [AIRFLOW] AirFlow DAG crowder-enr-category has failed consecutively...` |
| AirFlow DAG exceeded run time | `AirFlow DAG <JOB_NAME> exceeded expected run time` | **3** | `[CRITICAL] [AIRFLOW] AirFlow DAG verified-fan-registration exceeded expected run time` |

### Normalization Rules

1. Strip common title prefixes before extraction: `DSSD-NNNNN`, `DRGN-NNNNN`, `FCR-NNNNN`, `COREDATA-NNNNN`, `monitoring`, `restarted`, `Disabled. Ignore`
2. For Monitor jobs: strip `_prod` and `_airflow_prod` suffixes from job name
3. Group incidents by normalized job name

## Step 3: Filter Mergeable Groups

A group is **mergeable** only if:
- Contains **2 or more** incidents with the same normalized job name
- All incidents are **triggered** or **acknowledged** (not resolved)
- Passes one of the merge scenarios below:

### Scenario A: Same-Day Group (default)

All incidents are from the **same day** → mergeable, proceed to Step 4.

### Scenario B: Cross-Date Group with DSSD/DRGN Ticket

If a group spans **different dates** AND one incident has a **DSSD/DRGN ticket**:

1. **Fetch the DSSD/DRGN ticket from Jira** — read Summary, Description, Status, comments
2. **Fetch alerts** for both old and new incidents — compare actual error types
3. **Decision:**
   - If the Jira ticket describes the **same root cause** as the new incident (e.g., same error, same failure mode, ongoing known issue) → **MERGE** the new incident into the old one (preserving the DSSD/DRGN ticket context)
   - If the errors are **different** (e.g., old = SLA violation "exceeded run time", new = batch job failure during a mass outage) → **DO NOT MERGE** — different root causes
   - If the new incidents are part of a **mass failure event** (many unrelated jobs failing simultaneously) → **DO NOT MERGE** — the mass event has its own root cause unrelated to the old DSSD ticket

### Scenario C: Mass Failure Consolidation

When a **mass failure event** occurs (many unrelated jobs failing simultaneously with the same error, e.g., `FetchFailedException`), and there is already a **DSSD ticket** for the mass event:

1. **Identify the mass-failure DSSD incident** — look for a title like "Multiple databricks batch jobs failing: <error>"
2. **Check its alerts** — the DSSD incident may already contain alerts for specific jobs
3. **Merge candidates** into the mass-failure DSSD incident:
   - **Strong match:** standalone incidents whose job name already appears in the DSSD's alerts → merge
   - **Strong match:** standalone incidents with confirmed same error in notes (e.g., `FetchFailedException`) → merge
   - **Likely:** standalone Databricks/Monitor/AirFlow failures from the **same time window** (after the mass-failure DSSD was created), without their own DSSD tickets → merge
   - **Consequential failures** also merge: "data delayed" (downstream of failed jobs), "step not started on time" (dependency chain broken) → merge
4. **Do NOT merge:**
   - Non-Databricks/AirFlow/Monitor incidents (e.g., "Long Running GoAnywhere Jobs")
   - Incidents with their own separate DSSD/DRGN tickets for unrelated issues
   - Incidents from **before** the mass failure started (check timestamps)

### DO NOT Merge

- Cross-date incidents where errors are **different root causes** (e.g., old "exceeded run time" from last week + new "batch job failed" from today's mass outage)
- Cross-date incidents where the new failure is part of a **mass failure event** — even if the job name matches an old DSSD ticket (merge into the mass-failure DSSD instead, see Scenario C)
- Incidents with **different DSSD/DRGN ticket numbers** — they are tracked separately
- Incidents for **different workspaces/environments** even if the alert text looks similar (e.g., `ticketmaster-cds-analytics` vs `ticketmaster-cds-prod`)
- Incidents where the **job name is different** even if the prefix matches (e.g., `jb_edw_mbr_cpn_cat_0434_cpn_campn` vs `jb_edw_mbr_cpn_cat_0434_extended_tkt_type` are different sub-jobs — do NOT merge unless confirmed same root cause)
- Non-Databricks/AirFlow/Monitor incidents into mass-failure DSSD tickets

## Step 4: Check Notes (Comments)

For each incident in a mergeable group, fetch notes:

```bash
curl -s "https://api.pagerduty.com/incidents/<INCIDENT_ID>/notes" \
  -H "Authorization: Token token=<PD_TOKEN>" \
  -H "Accept: application/vnd.pagerduty+json;version=2"
```

### Classify Notes

| Note Content | Classification |
|---|---|
| `working on it` (any case) | **Ignore** — not a real comment |
| `DSSD-NNNNN - Status - Assignee. Snooze` | **Context note** — contains DSSD ticket reference. For same-day merges: ignore. For cross-date merges: use to look up the Jira ticket and compare errors (see Step 3 Scenario B) |
| `DRGN-NNNNN - Status - Assignee. Snooze` | **Context note** — contains DRGN ticket reference. Same rules as DSSD above |
| `Disabled. Ignore` | **Ignore** — auto-generated |
| Actual error messages, diagnostic info, Jira comments, Slack links, Databricks job links | **Real comment** |

## Step 5: Select Target (Surviving Incident)

Apply these rules **in order** — first match wins:

### Rule 1: Real Comments Override Everything

If **exactly one** incident in the group has real comments → it becomes the **TARGET**, regardless of alert type or creation time.

If **multiple** incidents have real comments → apply Rule 2 among those with real comments.

### Rule 2: Alert Type Priority

Among candidates (all, or only those with real comments):

| Priority | Alert Type | Role |
|---|---|---|
| **1 (highest)** | Databricks batch job failed | Preferred TARGET |
| **2** | Monitor job failed | TARGET only if no Databricks incident exists |
| **3 (lowest)** | AirFlow DAG failed/exceeded | TARGET only if no Databricks or Monitor exists |

### Rule 3: Tiebreaker

If multiple incidents share the same priority level → **earliest created** becomes TARGET.

### Decision Flowchart

```
Has any incident got REAL comments?
├── YES (exactly one) → that incident is TARGET
├── YES (multiple) → among those, pick highest alert priority → tiebreak by earliest
└── NO → pick highest alert priority → tiebreak by earliest
```

## Step 6: Execute Merge

Merge all non-target incidents INTO the target:

```bash
curl -s -X PUT "https://api.pagerduty.com/incidents/<TARGET_ID>/merge" \
  -H "Authorization: Token token=<PD_TOKEN>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/vnd.pagerduty+json;version=2" \
  -H "From: <JIRA_EMAIL>" \
  -d '{"source_incidents": [{"id": "<SOURCE_ID>", "type": "incident_reference"}]}'
```

- Merge **one at a time** (one source per API call)
- If a merge fails with "Arguments Caused Error" → the source incident is likely already resolved; skip it
- After all merges, report results: success count, failures, and reasons

## Step 7: Present Results to User

Before merging, **always show the plan** in table format:

```
### Group: <job_name> — N incidents
| Role | ID | Alert Type | P | Real notes | Created |
|---|---|---|---|---|---|
| TARGET | QXXX | Databricks batch job failed | 1 | 1 (error details) | HH:MM |
| merge  | QYYY | Monitor job failed           | 2 | 0                  | HH:MM |
| merge  | QZZZ | AirFlow DAG failed consec.   | 3 | 0                  | HH:MM |
```

Wait for user confirmation before executing merges.

---

## Examples

### Example 1: Standard 3-alert group (no real comments)

Incidents:
- `Monitor job 'jb_edw_foo_0034_prod' failed` (17:34)
- `Databricks batch job jb_edw_foo_0034 failed` (17:41)
- `AirFlow DAG jb_edw_foo_0034 has failed consecutively` (19:06)

Result: **TARGET = Databricks** (P1), merge Monitor + AirFlow into it.

### Example 2: Real comment overrides priority

Incidents:
- `Databricks batch job bar failed` (17:56) — 0 real notes
- `AirFlow DAG bar failed consecutively` (18:06) — 1 real note with Spark error details

Result: **TARGET = AirFlow** (has real comment), merge Databricks into it.

### Example 3: Cross-date, mass failure — DO NOT merge

Incidents:
- `DSSD-28981 AirFlow DAG baz exceeded expected run time` (Feb 18) — 20 snooze notes, Jira: "Frequent SLA violations"
- `Databricks batch job baz failed` (Feb 26) — 0 real notes, part of mass Spark shuffle failure

Analysis: Old = chronic SLA violation (job runs too long). New = batch job crash during a mass outage (many unrelated jobs failing with `FetchFailedException`). Jira ticket describes a different problem than today's mass event.

Result: **DO NOT MERGE** — the new failure is part of a mass event, not a continuation of the old SLA issue.

### Example 4: Same alert type, same day — merge by earliest

Incidents:
- `Monitor job 'qux_airflow_prod' failed` (17:24) — 0 real notes
- `Monitor job 'qux_airflow_prod' failed` (18:28) — 0 real notes
- `Monitor job 'qux_airflow_prod' failed` (19:22) — 0 real notes

Result: **TARGET = earliest Monitor** (17:24), merge the other two.

### Example 5: Cross-date, same root cause with DSSD — MERGE into old

Incidents:
- `DSSD-29050 Databricks batch job foo-bar failed` (Feb 22) — 10 snooze notes, Jira: "foo-bar fails with OOM on large partitions"
- `Databricks batch job foo-bar failed` (Feb 26) — 1 real note: "java.lang.OutOfMemoryError: Java heap space"

Analysis: Jira DSSD-29050 describes OOM failures. New incident's alert/notes show the same OOM error. Not a mass event — only this job failed. Same root cause, ongoing issue.

Result: **MERGE new → old** (TARGET = Q_old with DSSD-29050). Preserves Jira ticket context.

### Example 6: Mass failure consolidation into DSSD

Target incident:
- `DSSD-29178 Multiple databricks batch jobs failing: org.apache.spark.shuffle.FetchFailedException` (14:26 UTC) — 25 alerts already merged

Candidates:
- `Databricks batch job jb_edw_evt_business_location_0034 failed` (17:41) — note confirms `Py4JJavaError: FetchFailedException` → **MERGE** (confirmed same error)
- `AirFlow DAG crowder-enr-item has failed consecutively` (18:06) — crowder-enr-item already in DSSD-29178 alerts → **MERGE** (strong match)
- `Databricks batch job lytics-primary-transactions-batch failed` (18:36) — same time window, no own ticket → **MERGE** (likely)
- `Databricks delta GA4 web events data delayed` (18:21) — GA4 DAGs failed in mass event, so data is delayed → **MERGE** (consequential)
- `Databricks batch job discovery-attraction step not started on time` (20:26) — dependency chain broken by mass failure → **MERGE** (consequential)
- `Long Running GoAnywhere Jobs` (17:30) — not Databricks/AirFlow → **DO NOT MERGE**

---

## API Reference

| Operation | Method | Endpoint |
|---|---|---|
| List incidents | GET | `/incidents?user_ids[]=X&statuses[]=triggered&statuses[]=acknowledged` |
| Get incident | GET | `/incidents/<ID>` |
| Get alerts | GET | `/incidents/<ID>/alerts` |
| Get notes | GET | `/incidents/<ID>/notes` |
| Add note | POST | `/incidents/<ID>/notes` |
| Merge | PUT | `/incidents/<TARGET_ID>/merge` |

All requests require:
- `Authorization: Token token=<PD_TOKEN>`
- `Accept: application/vnd.pagerduty+json;version=2`

Write operations additionally require:
- `Content-Type: application/json`
- `From: <JIRA_EMAIL>`

---

## Version History

- v1.0 (2026-02-26): Initial version. Priority-based target selection (Databricks > Monitor > AirFlow), real comment override, same-day-only merge rule.
- v1.1 (2026-02-26): Cross-date merge with DSSD/DRGN ticket validation. DSSD/DRGN snooze notes now used as context (not ignored) — fetch Jira ticket, compare errors. Mass failure events block cross-date merges.
- v1.2 (2026-02-26): Mass failure consolidation (Scenario C). When a mass-failure DSSD exists, merge standalone incidents into it by: strong match (job in DSSD alerts / confirmed error), likely (same time window), consequential (data delayed / step not started). Exclude non-Databricks jobs.
