# pd-clean-titles — strip Jira-link emojis from PD incident titles

When the PD↔Jira-Server integration links a DSSD/DRGN to a PD incident,
it prepends the title with `<TICKET> 📤 ` / `<TICKET> 🔗 ` markers. The
emojis don't render well in some terminals/Slack clients and they break
naive grep-by-substring workflows that look for the literal title.

This tool finds open/acknowledged incidents on the current user, scans
each title for those two specific emojis, and rewrites them in place to
plain ` - ` separators.

## Behaviour

- **Targets only two emojis:**
  - `📤` (`U+1F4E4`, outbox tray) — added on DSSD link
  - `🔗` (`U+1F517`, link) — added on DRGN link
- Anything else in the title is preserved verbatim, including newlines.
- Idempotent: a second run on already-cleaned titles is a no-op.
- Whitespace around the emoji is collapsed so `DSSD-X 📤 [ERROR]` →
  `DSSD-X - [ERROR]` (single spaces, not doubled).

## Usage

```bash
# Show planned rewrites and prompt for confirmation
python pd_clean_titles.py

# Just preview, never POST
python pd_clean_titles.py --dry-run

# Apply without prompting
python pd_clean_titles.py --yes
```

Output (preview block):

```
Planned rewrites:
======================================================================
  Q0YW7OYTRFZP3T
    -  DSSD-31338 📤 DRGN-18028 🔗 [ERROR] ... lincs_artistmapping-012 failed
    +  DSSD-31338 - DRGN-18028 - [ERROR] ... lincs_artistmapping-012 failed
  Q2SG1BV82HQ9CF
    -  DSSD-31347 📤 [WARNING] [AWS] Token is about to expire: IRSERVICE
    +  DSSD-31347 - [WARNING] [AWS] Token is about to expire: IRSERVICE
======================================================================
```

## API

`PUT /incidents/{id}` with `{"incident":{"type":"incident","title":"<new>"}}`
and `From: <user_email>` header. Uses the existing `PAGERDUTY_API_TOKEN`
REST API key — no UI session bearer required (unlike pd-create-drgn).

## Environment

- `PAGERDUTY_API_TOKEN` — classic PD REST API key

## Files

- `pd_clean_titles.py` — CLI
