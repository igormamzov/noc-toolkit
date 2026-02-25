# NOC Toolkit - Version Information

This document describes the versioning strategy for the NOC Toolkit and all its tools.

---

## Versioning Standard

We follow **Semantic Versioning (SemVer)**: `MAJOR.MINOR.PATCH`

- **MAJOR (X)** - Breaking changes, incompatible API changes
- **MINOR (Y)** - New features (backward compatible)
- **PATCH (Z)** - Bug fixes (backward compatible)

### Pre-1.0 Development

All components are currently in **0.x.x** version, indicating active development:
- API is not yet stable
- Breaking changes may occur between minor versions
- Version 1.0.0 will indicate production-ready, stable API

---

## Current Versions

| Component                  | Version | Status        | Description                                    |
|----------------------------|---------|---------------|------------------------------------------------|
| **noc-toolkit**            | 0.1.0   | Development   | Main toolkit launcher and orchestrator         |
| **pd-monitor**             | 0.1.0   | Development   | Auto-acknowledge triggered PagerDuty incidents |
| **pd-jira-tool**           | 0.3.0   | Development   | PagerDuty-Jira integration and sync tool       |
| **pagerduty-job-extractor**| 0.1.0   | Development   | Extract failed job names from PD incidents     |

---

## Version Storage

Each component stores its version in two places:

1. **Python file** - `VERSION = "X.Y.Z"` constant at the top of the main script
2. **README.md** - `**Version:** X.Y.Z` in the header section

### Accessing Version Information

**From Command Line:**
```bash
# NOC Toolkit
python3 noc-toolkit.py --help  # Version shown in help

# Individual Tools
python3 tools/pd-monitor/pd_monitor.py --version
```

**From Python Code:**
```python
# Import version from tool
from pd_monitor import VERSION
print(f"Version: {VERSION}")
```

---

## Version History

### NOC Toolkit v0.1.0 (2026-02-22)

**Initial unified release:**
- Unified launcher for all NOC tools
- Centralized configuration via shared `.env` file
- Standardized versioning across all tools
- Tools: pd-monitor (0.1.0), pd-jira-tool (0.3.0), pagerduty-job-extractor (0.1.0)

### pd-monitor v0.1.0 (2026-02-22)

**Initial release:**
- Monitor triggered incidents assigned to current user
- Automatic acknowledgment with smart comment logic
- Continuous monitoring mode with countdown timer
- Output file for incidents needing attention

### pd-jira-tool v0.3.0 (2026-02-22)

**Version standardization:**
- Formalized version number from previous informal "v3.2"
- Existing features: auto-discovery, status tracking, auto-snooze
- Progress bar with time estimation
- Smart filtering and duplicate prevention

### pagerduty-job-extractor v0.1.0 (2026-02-22)

**Initial versioned release:**
- Extract failed job names matching `jb_*` pattern
- Support for incident URLs and IDs
- Integration with NOC Toolkit

---

## Roadmap to v1.0.0

Before marking any component as 1.0.0 (production-ready), we will:

1. **Stabilize API** - No more breaking changes
2. **Complete Testing** - Comprehensive test coverage
3. **User Feedback** - Incorporate feedback from production use
4. **Documentation** - Complete documentation for all features
5. **Error Handling** - Robust error handling and recovery

Target: **Q2 2026**

---

## Version Update Process

When updating versions:

1. **Update Python file** - Change `VERSION` constant
2. **Update README.md** - Change version in header
3. **Update VERSION.md** - Add entry to version history
4. **Tag in Git** - Create version tag (if using git)
5. **Update Changelog** - Document changes in CHANGELOG.md (if exists)

---

**Last Updated:** 2026-02-22
**Maintained by:** NOC Team
