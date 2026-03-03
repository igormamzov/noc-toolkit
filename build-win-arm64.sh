#!/bin/bash
# NOC Toolkit — Windows ARM64 Portable Builder
# Creates a self-contained package with embedded Python ARM64 + dependencies
# Runs on macOS/Linux — no Windows required for building
#
# Usage:
#   ./build-win-arm64.sh                    # Build with default Python 3.12.8
#   PYTHON_VERSION=3.13.1 ./build-win-arm64.sh  # Override Python version

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# -----------------------------------------------
# Configuration
# -----------------------------------------------

# Python version for embedded distribution (ARM64 available since 3.11)
PYTHON_VERSION="${PYTHON_VERSION:-3.12.8}"
PYTHON_MAJOR_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f1-2)
PYTHON_TAG=$(echo "$PYTHON_MAJOR_MINOR" | tr -d '.')

# Extract toolkit version
VERSION=$(grep 'VERSION *= *"' noc-toolkit.py | head -1 | sed 's/.*"\(.*\)"/\1/')
if [ -z "$VERSION" ]; then
    echo "[ERROR] Could not extract VERSION from noc-toolkit.py"
    exit 1
fi

RELEASE_DIR="release"
PACKAGE_NAME="noc-toolkit-v${VERSION}-windows-arm64"
STAGING="${RELEASE_DIR}/${PACKAGE_NAME}"
PYTHON_URL="https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-embed-arm64.zip"
PYTHON_ZIP="/tmp/python-${PYTHON_VERSION}-embed-arm64.zip"

echo "============================================"
echo "NOC Toolkit — Windows ARM64 Portable Builder"
echo "============================================"
echo "Toolkit:  v${VERSION}"
echo "Python:   ${PYTHON_VERSION} (ARM64 embedded)"
echo "Output:   ${RELEASE_DIR}/${PACKAGE_NAME}.zip"
echo "============================================"
echo ""

# -----------------------------------------------
# Step 1: Download Python ARM64 Embedded
# -----------------------------------------------
echo "[1/6] Downloading Python ${PYTHON_VERSION} ARM64 embedded..."

if [ -f "$PYTHON_ZIP" ]; then
    echo "  Using cached: ${PYTHON_ZIP}"
else
    HTTP_CODE=$(curl -sL -o "$PYTHON_ZIP" -w "%{http_code}" "$PYTHON_URL")
    if [ "$HTTP_CODE" != "200" ]; then
        rm -f "$PYTHON_ZIP"
        echo "  [ERROR] Download failed (HTTP ${HTTP_CODE})"
        echo "  URL: ${PYTHON_URL}"
        echo "  Try a different version: PYTHON_VERSION=3.12.7 ./build-win-arm64.sh"
        exit 1
    fi
    echo "  Downloaded: $(ls -lh "$PYTHON_ZIP" | awk '{print $5}')"
fi
echo ""

# -----------------------------------------------
# Step 2: Set up staging directory
# -----------------------------------------------
echo "[2/6] Setting up staging directory..."

rm -rf "$STAGING"
mkdir -p "${STAGING}/python"

# Extract embedded Python
unzip -q "$PYTHON_ZIP" -d "${STAGING}/python"
echo "  Extracted Python to: ${STAGING}/python/"

# -----------------------------------------------
# Step 3: Configure embedded Python for site-packages
# -----------------------------------------------
echo "[3/6] Configuring embedded Python..."

PTH_FILE="${STAGING}/python/python${PYTHON_TAG}._pth"
if [ ! -f "$PTH_FILE" ]; then
    echo "  [ERROR] Path config not found: ${PTH_FILE}"
    echo "  Check Python version — embedded distribution may have a different structure"
    exit 1
fi

# Rewrite ._pth to enable site-packages and find our scripts
cat > "$PTH_FILE" << EOF
python${PYTHON_TAG}.zip
Lib\\site-packages
..
import site
EOF

# Create site-packages directory
mkdir -p "${STAGING}/python/Lib/site-packages"
echo "  Configured: ${PTH_FILE}"
echo ""

# -----------------------------------------------
# Step 4: Install dependencies
# -----------------------------------------------
echo "[4/6] Installing Python dependencies..."

# All our deps are pure Python (py3-none-any wheels), safe to install from macOS
pip3 install \
    --target="${STAGING}/python/Lib/site-packages" \
    --no-compile \
    --disable-pip-version-check \
    --quiet \
    python-dotenv pagerduty jira tqdm

# Show what was installed
echo "  Installed packages:"
for pkg_dir in "${STAGING}/python/Lib/site-packages/"*.dist-info; do
    if [ -d "$pkg_dir" ]; then
        PKG=$(basename "$pkg_dir" | sed 's/-.*//')
        printf "    - %s\n" "$PKG"
    fi
done
echo ""

# -----------------------------------------------
# Step 5: Bundle toolkit source + launcher
# -----------------------------------------------
echo "[5/6] Bundling toolkit source..."

# Copy source files
cp noc-toolkit.py "${STAGING}/"
cp -r tools "${STAGING}/"
cp .env.example "${STAGING}/"
cp README.md "${STAGING}/"
cp README_RU.md "${STAGING}/"

# Clean up unwanted files
find "$STAGING" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$STAGING" -name "*.pyc" -delete 2>/dev/null || true
find "$STAGING" -name ".pd_merge_skips.json" -delete 2>/dev/null || true
find "$STAGING" -name ".pd-monitor-state.json" -delete 2>/dev/null || true
find "$STAGING" -name ".DS_Store" -delete 2>/dev/null || true

# Create run.bat launcher
cat > "${STAGING}/run.bat" << 'BATEOF'
@echo off
echo ============================================
echo NOC Toolkit for Windows ARM64
echo ============================================
echo.
"%~dp0python\python.exe" "%~dp0noc-toolkit.py"
echo.
echo ============================================
pause
BATEOF

echo "  Source files copied + run.bat created"
echo ""

# -----------------------------------------------
# Step 6: Create ZIP archive
# -----------------------------------------------
echo "[6/6] Creating ZIP archive..."

mkdir -p "$RELEASE_DIR"
(cd "$RELEASE_DIR" && zip -rq "${PACKAGE_NAME}.zip" "${PACKAGE_NAME}/")

# Clean up staging directory
rm -rf "$STAGING"

# -----------------------------------------------
# Summary
# -----------------------------------------------
ARCHIVE="${RELEASE_DIR}/${PACKAGE_NAME}.zip"
SIZE=$(ls -lh "$ARCHIVE" | awk '{print $5}')
MD5=$(md5 -q "$ARCHIVE" 2>/dev/null || md5sum "$ARCHIVE" | awk '{print $1}')

echo ""
echo "============================================"
echo "Build complete!"
echo "============================================"
echo ""
printf "  %-50s %8s\n" "${PACKAGE_NAME}.zip" "$SIZE"
echo "  MD5: ${MD5}"
echo ""
echo "Contents:"
echo "  python/python.exe     — Python ${PYTHON_VERSION} ARM64 (native)"
echo "  python/Lib/           — bundled dependencies"
echo "  noc-toolkit.py        — toolkit v${VERSION}"
echo "  tools/                — all 4 tools"
echo "  run.bat               — double-click to launch"
echo ""
echo "Next steps:"
echo "  1. Send ${ARCHIVE} to colleague"
echo "  2. Extract ZIP on Windows ARM64 machine"
echo "  3. Copy .env.example -> .env and configure tokens"
echo "  4. Double-click run.bat"
echo "============================================"
