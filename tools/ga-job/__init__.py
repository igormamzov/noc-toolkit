"""GoAnywhere Web Client read-only tool.

Reads CompletedJobs and ListMonitors pages via session cookie scraping.
GoAnywhere prod (7.8.4) does not expose REST API for read operations on this
tenant — Admin User API Keys only authorize Submit Job calls. Login is
gated behind OKTA push MFA, so a Selenium-based one-time login captures the
session cookie and saves it for subsequent CLI calls.
"""

VERSION = "0.1.0"
