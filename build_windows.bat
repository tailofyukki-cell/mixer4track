@echo off
echo ============================================
echo  Mixer4Track - Windows Build Script
echo ============================================
echo.

echo [1/3] Installing dependencies (pygame PyQt5 numpy pyinstaller)...
rem eq_engine.py uses scipy.signal.sosfilt for fast IIR filtering (falls back to numpy if unavailable)
python -m pip install --upgrade pip
python -m pip install pygame PyQt5 numpy scipy pyinstaller
if %errorlevel% neq 0 (
    echo ERROR: Failed to install packages.
    pause
    exit /b 1
)

echo.
echo [2/3] Building exe...
python -m PyInstaller --noconfirm mixer4track.spec
if %errorlevel% neq 0 (
    echo ERROR: Build failed.
    pause
    exit /b 1
)

echo.
echo [3/3] Build complete!
echo.
echo Output: dist\Mixer4Track.exe
echo.
pause
