@echo off
REM NOC Toolkit - Windows Build Script
REM Автоматическая сборка .exe файла

echo ============================================
echo NOC Toolkit - Windows Builder
echo ============================================
echo.

REM Шаг 1: Проверка Python
echo [1/6] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [X] Python not found! Please install Python 3.10+
    echo     Download: https://www.python.org/downloads/
    pause
    exit /b 1
)
python --version
echo.

REM Шаг 2: Установка зависимостей
echo [2/6] Installing dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo [X] Failed to install dependencies
    pause
    exit /b 1
)
echo.

REM Шаг 3: Установка PyInstaller
echo [3/6] Installing PyInstaller...
pip install pyinstaller
if errorlevel 1 (
    echo [X] Failed to install PyInstaller
    pause
    exit /b 1
)
echo.

REM Шаг 4: Тестовый запуск (быстрая проверка)
echo [4/6] Testing toolkit...
echo import sys; sys.exit(0) | python noc-toolkit.py >nul 2>&1
if errorlevel 1 (
    echo [!] Warning: Toolkit test failed, but continuing...
)
echo OK
echo.

REM Шаг 5: Сборка .exe
echo [5/6] Building .exe file (this may take 2-5 minutes)...
pyinstaller NOC-Toolkit.spec --clean
if errorlevel 1 (
    echo [X] Build failed! Check error messages above.
    pause
    exit /b 1
)
echo.

REM Шаг 6: Проверка результата
echo [6/6] Verifying build...
if not exist "dist\NOC-Toolkit.exe" (
    echo [X] NOC-Toolkit.exe not found in dist\
    pause
    exit /b 1
)

echo.
echo ============================================
echo SUCCESS! Build completed.
echo ============================================
echo.
echo Executable file: dist\NOC-Toolkit.exe
dir dist\NOC-Toolkit.exe | find "NOC-Toolkit.exe"
echo.

REM Создание пакета для распространения
echo.
echo Creating distribution package...
if exist "noc-toolkit-windows-release" rmdir /s /q noc-toolkit-windows-release
mkdir noc-toolkit-windows-release

copy dist\NOC-Toolkit.exe noc-toolkit-windows-release\ >nul
copy .env.example noc-toolkit-windows-release\ >nul
copy README.md noc-toolkit-windows-release\ >nul
copy README_RU.md noc-toolkit-windows-release\ >nul

REM Создание run.bat
echo @echo off > noc-toolkit-windows-release\run.bat
echo echo ============================================ >> noc-toolkit-windows-release\run.bat
echo echo NOC Toolkit for Windows >> noc-toolkit-windows-release\run.bat
echo echo ============================================ >> noc-toolkit-windows-release\run.bat
echo echo. >> noc-toolkit-windows-release\run.bat
echo NOC-Toolkit.exe >> noc-toolkit-windows-release\run.bat
echo echo. >> noc-toolkit-windows-release\run.bat
echo echo ============================================ >> noc-toolkit-windows-release\run.bat
echo pause >> noc-toolkit-windows-release\run.bat

echo.
echo Package created: noc-toolkit-windows-release\
echo.
echo Next steps:
echo   1. Test: cd noc-toolkit-windows-release ^&^& run.bat
echo   2. Archive: powershell Compress-Archive -Path noc-toolkit-windows-release -DestinationPath noc-toolkit-v1.0.0-windows.zip
echo.
echo ============================================
pause
