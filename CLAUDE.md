# NOC Toolkit - Project Instructions

## SLA & Schedules

### Data Freshness Report (DACSCAN)
- **Source:** Databricks notebook — `dataservices_treasury.treasury_teradata_base.pfocuscntl_meta_load_status`
- **Confluence:** https://confluence.livenation.com/spaces/DS/pages/310153213
- **SLA Deadline:** Report must show NO "Delayed" items by:
  - **2:30 PM ART** (Argentina Time, UTC-3)
  - **5:30 PM UTC**
  - **9:30 AM PST** (Pacific Standard Time, UTC-8)
- **Note:** The `load-status-001-batch-delta-transformation` job updates the meta_load_status table once per hour. If data is delayed in the report, actual tables may already be fresh — verify with table-level checks before reporting.
