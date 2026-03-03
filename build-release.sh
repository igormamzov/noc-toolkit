#!/bin/bash
# NOC Toolkit — Local Release Builder
# Creates distributable packages for macOS and source archives
#
# Usage:
#   ./build-release.sh          # Build all packages
#   ./build-release.sh --skip-binary  # Source archives only (no PyInstaller)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Extract version from noc-toolkit.py
VERSION=$(grep 'VERSION *= *"' noc-toolkit.py | head -1 | sed 's/.*"\(.*\)"/\1/')

if [ -z "$VERSION" ]; then
    echo "[ERROR] Could not extract VERSION from noc-toolkit.py"
    exit 1
fi

SKIP_BINARY=false
if [ "${1:-}" = "--skip-binary" ]; then
    SKIP_BINARY=true
fi

RELEASE_DIR="release"
ARCH=$(uname -m)
OS=$(uname -s | tr '[:upper:]' '[:lower:]')

echo "============================================"
echo "NOC Toolkit — Release Builder"
echo "============================================"
echo "Version:  v${VERSION}"
echo "Platform: ${OS} ${ARCH}"
echo "Output:   ${RELEASE_DIR}/"
echo "============================================"
echo ""

# Clean previous release
rm -rf "${RELEASE_DIR}"
mkdir -p "${RELEASE_DIR}"

# -----------------------------------------------
# Source archives (platform-independent)
# -----------------------------------------------
echo "[1/3] Creating source archives..."

SOURCE_NAME="noc-toolkit-v${VERSION}-source"
SOURCE_DIR="${RELEASE_DIR}/${SOURCE_NAME}"
mkdir -p "${SOURCE_DIR}"

# Copy source files
cp noc-toolkit.py "${SOURCE_DIR}/"
cp -r tools "${SOURCE_DIR}/"
cp .env.example "${SOURCE_DIR}/"
cp requirements.txt "${SOURCE_DIR}/"
cp README.md "${SOURCE_DIR}/"
cp README_RU.md "${SOURCE_DIR}/"
cp NOC-Toolkit.spec "${SOURCE_DIR}/"
cp build-windows.bat "${SOURCE_DIR}/"

# Clean up unwanted files from source
find "${SOURCE_DIR}" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "${SOURCE_DIR}" -name "*.pyc" -delete 2>/dev/null || true
find "${SOURCE_DIR}" -name ".pd_merge_skips.json" -delete 2>/dev/null || true
find "${SOURCE_DIR}" -name ".pd-monitor-state.json" -delete 2>/dev/null || true
find "${SOURCE_DIR}" -name ".DS_Store" -delete 2>/dev/null || true

# Create .zip (for Windows users)
(cd "${RELEASE_DIR}" && zip -rq "${SOURCE_NAME}.zip" "${SOURCE_NAME}/")
echo "  -> ${RELEASE_DIR}/${SOURCE_NAME}.zip"

# Create .tar.gz (for Mac/Linux users)
(cd "${RELEASE_DIR}" && tar -czf "${SOURCE_NAME}.tar.gz" "${SOURCE_NAME}/")
echo "  -> ${RELEASE_DIR}/${SOURCE_NAME}.tar.gz"

# Clean up temp source dir
rm -rf "${SOURCE_DIR}"

echo "  Done."
echo ""

# -----------------------------------------------
# macOS binary package (PyInstaller)
# -----------------------------------------------
if [ "$SKIP_BINARY" = true ]; then
    echo "[2/3] Skipping binary build (--skip-binary)"
    echo ""
elif [ "$OS" != "darwin" ]; then
    echo "[2/3] Skipping macOS binary (not on macOS)"
    echo ""
else
    echo "[2/3] Building macOS ${ARCH} binary..."

    # Check PyInstaller
    if ! python3 -c "import PyInstaller" 2>/dev/null; then
        echo "  [WARN] PyInstaller not installed. Run: pip3 install pyinstaller"
        echo "  Skipping binary build."
        echo ""
    else
        # Build
        pyinstaller NOC-Toolkit.spec --clean --noconfirm 2>&1 | tail -3
        echo ""

        if [ ! -f "dist/NOC-Toolkit" ]; then
            echo "  [ERROR] Build failed — dist/NOC-Toolkit not found"
            exit 1
        fi

        BINARY_NAME="noc-toolkit-v${VERSION}-macos-${ARCH}"
        BINARY_DIR="${RELEASE_DIR}/${BINARY_NAME}"
        mkdir -p "${BINARY_DIR}"

        cp dist/NOC-Toolkit "${BINARY_DIR}/"
        chmod +x "${BINARY_DIR}/NOC-Toolkit"
        cp .env.example "${BINARY_DIR}/"
        cp README.md "${BINARY_DIR}/"
        cp README_RU.md "${BINARY_DIR}/"

        # Create run.sh
        cat > "${BINARY_DIR}/run.sh" << 'RUNEOF'
#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "============================================"
echo "NOC Toolkit"
echo "============================================"
echo ""
"${SCRIPT_DIR}/NOC-Toolkit"
echo ""
echo "============================================"
read -p "Press Enter to exit..."
RUNEOF
        chmod +x "${BINARY_DIR}/run.sh"

        # Create .tar.gz
        (cd "${RELEASE_DIR}" && tar -czf "${BINARY_NAME}.tar.gz" "${BINARY_NAME}/")
        echo "  -> ${RELEASE_DIR}/${BINARY_NAME}.tar.gz"

        # Clean up temp dir
        rm -rf "${BINARY_DIR}"

        echo "  Done."
        echo ""
    fi
fi

# -----------------------------------------------
# Summary
# -----------------------------------------------
echo "[3/3] Release summary"
echo "============================================"
echo ""

for f in "${RELEASE_DIR}"/*; do
    case "$f" in
        *.tar.gz|*.zip)
            if [ -f "$f" ]; then
                SIZE=$(ls -lh "$f" | awk '{print $5}')
                MD5=$(md5 -q "$f" 2>/dev/null || md5sum "$f" | awk '{print $1}')
                BASENAME=$(basename "$f")
                printf "  %-50s %8s  MD5: %s\n" "$BASENAME" "$SIZE" "$MD5"
            fi
            ;;
    esac
done

echo ""
echo "============================================"
echo "Packages ready in: ${RELEASE_DIR}/"
echo ""
echo "Next steps:"
echo "  1. Test macOS binary: tar xzf ${RELEASE_DIR}/noc-toolkit-v${VERSION}-macos-${ARCH}.tar.gz && ./noc-toolkit-v${VERSION}-macos-${ARCH}/NOC-Toolkit"
echo "  2. Push to GitHub for Windows x86_64 build: git push origin main"
echo "  3. Windows ARM64 portable: ./build-win-arm64.sh"
echo "============================================"
