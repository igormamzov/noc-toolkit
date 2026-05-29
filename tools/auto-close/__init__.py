"""Auto-close DRGN tickets for transient Databricks failures.

Scans PagerDuty for acknowledged Databricks batch job failures, verifies
recovery via CDT (next scheduled run = success), then closes the
auto-created DRGN ticket. PD incidents auto-resolve on DRGN close via
the Jira → PD integration.

Whitelist (whitelist.json) restricts which jobs are eligible — start
narrow (asra_*), expand carefully.
"""
VERSION = "0.1.0"
