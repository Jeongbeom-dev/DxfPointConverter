@echo off
chcp 65001 >nul
REM ===== DXF 포인트 변환기 EXE 빌드 스크립트 =====
cd /d "%~dp0"

echo [1/2] PyInstaller 확인...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo   PyInstaller 미설치 - 설치를 시작합니다.
    python -m pip install --upgrade pyinstaller
)

echo [2/2] EXE 빌드 중... (수십 초 소요)
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name DxfPointConverter ^
    --distpath dist ^
    --workpath build ^
    --specpath build ^
    gui.py

echo.
if exist "dist\DxfPointConverter.exe" (
    echo ================================================
    echo  빌드 완료:  dist\DxfPointConverter.exe
    echo ================================================
) else (
    echo [오류] 빌드 실패 - 위 로그를 확인하세요.
)
pause
