# pd-merge — PagerDuty incident merge tool

Discovers and merges related PagerDuty incidents that share the same root cause
(same job/DAG name). See `pd-merge-logic.md` for the full scenario taxonomy
(A: same-day; B: cross-date with Jira validation; C: mass-failure rollup;
D: RDS Exports "failed to start" rollup).

## Usage

### Default — interactive merge workflow

```bash
python pd_merge.py                 # interactive, real merges
python pd_merge.py --dry-run       # preview without API changes
python pd_merge.py --verbose       # show extra debug output
python pd_merge.py --show-skips    # list incidents in the skip file
python pd_merge.py --clear-skips   # forget the skip file
```

Interactive prompts during merge:

| Input | Action |
|---|---|
| `y` | Merge all sources in the current group |
| `n` / `skip` / *empty* | Skip this group (remembered for future runs) |
| `all` | Merge all remaining groups without further prompts |
| `select` | Pick specific source incidents from the group |

### `jobs` subcommand — list `jb_*` jobs from a merged incident

Folded in from the deprecated standalone `pd-jobs` tool on 2026-05-31. Useful
when a Scenario C/D mass-failure incident has rolled up many alerts and you
want a flat list of every `jb_*` job mentioned anywhere — incident payload,
alerts, or notes.

```bash
python pd_merge.py jobs Q1WPEMZKLQZGJF
python pd_merge.py jobs https://yourcompany.pagerduty.com/incidents/Q1WPEMZKLQZGJF
```

Output: one job name per line on stdout, sorted. Exit code 1 if nothing matched.

## Files

- `pd_merge.py` — CLI (default workflow + `jobs` subcommand)
- `pd-merge-logic.md` — scenario logic reference (if present)
- `.pd_merge_skips.json` — gitignored skip persistence
