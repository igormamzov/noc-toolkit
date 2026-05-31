# pd-escalate — DRGN → DSSD escalation automation

Automates the post-DSSD-creation escalation workflow from a PagerDuty
incident:

1. Resolve PD user
2. Fetch PD incident (and pull DRGN from `external_references` / notes)
3. Fetch DSSD status & assignee
4. Create Jira link `DRGN "is blocked by" DSSD`
5. *(Optional)* Clear `CDS Opt role` on the DRGN — suppresses auto-DSSD creation
6. Transition DRGN → **Escalated**
7. Post PD note with escalation summary
8. Print Slack template ready to paste into `#cds-ops-24x7-int`

## Usage

```bash
# Standard escalation: DRGN auto-detected, transition to Escalated,
# Jira automation creates a fresh DSSD.
python pd_escalate.py --pd Q33L5GALLQ3ESB --dssd DSSD-29386

# Dry run (no API mutations)
python pd_escalate.py --pd Q33L5GALLQ3ESB --dssd DSSD-29386 --dry-run

# Override auto-detected DRGN explicitly
python pd_escalate.py --pd Q33L5GALLQ3ESB --dssd DSSD-29386 --drgn DRGN-15087
```

## `--no-auto-dssd` — link to an existing DSSD without creating a duplicate

When a DRGN moves to **Escalated**, a Jira automation rule fires and creates
a fresh DSSD Escalation ticket *if* the `CDS Opt role` field is populated.

Sometimes that's the wrong move — e.g. when the chronic underlying issue is
already tracked in **DSSD-31131** (`asra` family) or **DSSD-31259**
(`sfmc_backfeed_0160`), creating yet another DSSD just adds noise.

`--no-auto-dssd` clears `CDS Opt role` on the DRGN *before* the transition,
which suppresses the auto-DSSD creation. The DRGN is then linked to the
pre-existing ticket the user supplied via `--dssd`.

```bash
python pd_escalate.py \
    --pd Q33L5GALLQ3ESB \
    --dssd DSSD-31131 \
    --no-auto-dssd
```

### Field resolution

By default the tool resolves `CDS Opt role` to its `customfield_NNNNN` ID by
calling `jira.fields()` and matching by name (case-insensitive). If the field
is later renamed or the lookup is too slow, override it via the env var:

```env
JIRA_CDS_OPT_ROLE_FIELD_ID=customfield_45204
```

### Failure mode: field requires an explicit option ID

If your Jira config rejects setting the field to `None` (some single-select
fields require an option ID instead of a null), the tool will surface the
JIRAError. Use the env override to point at the right field, then determine
the correct "None"-equivalent option ID via:

```bash
curl -s -H "Authorization: Bearer $JIRA_PERSONAL_ACCESS_TOKEN" \
    "$JIRA_SERVER_URL/rest/api/2/issue/DRGN-NNNNN/editmeta" \
    | jq '.fields["customfield_45204"]'
```

The tool does not yet support setting an explicit option ID — open a follow-up
if you hit this case in production.

## Environment

Required (validated at startup):

- `PAGERDUTY_API_TOKEN`
- `JIRA_SERVER_URL`
- `JIRA_PERSONAL_ACCESS_TOKEN`

Optional:

- `JIRA_CDS_OPT_ROLE_FIELD_ID` — bypass the `CDS Opt role` field name lookup
