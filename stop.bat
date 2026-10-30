@echo off
setlocal EnableExtensions

rem ============================================================
rem  videoflow - stop.bat
rem  Pasangan start.bat: menutup API, worker Celery, dashboard,
rem  lalu (kalau diminta) container postgres + redis.
rem ============================================================

cd /d "%~dp0"

echo Yang akan dihentikan:
echo   - uvicorn / API :8000
echo   - worker Celery (antrean videoflow)
echo   - node / dashboard :3000
echo   - proses concat.exe anak worker, kalau masih tertinggal
echo.
echo Job yang sedang berjalan TIDAK hilang: dengan acks_late pesan tugas
echo kembali ke antrean dan worker berikutnya melanjutkan dari artifact
echo terakhir (potongan/transkrip yang sudah jadi tidak diulang).
echo.
choice /C YN /M "Lanjutkan?"
if errorlevel 2 exit /b 0

powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match 'uvicorn|videoflow\.worker' } | ForEach-Object { Write-Host ('  berhenti PID ' + $_.ProcessId + ' : ' + $_.Name); Stop-Process -Id $_.ProcessId -Force }"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'node.exe' -and $_.CommandLine -match 'next' } | ForEach-Object { Write-Host ('  berhenti PID ' + $_.ProcessId + ' : node/next'); Stop-Process -Id $_.ProcessId -Force }"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'concat.exe' } | ForEach-Object { Write-Host ('  berhenti PID ' + $_.ProcessId + ' : concat'); Stop-Process -Id $_.ProcessId -Force }"

rem concat yang di-spawn worker adalah proses anak; kalau worker dimatikan
rem mendadak, anak ini bisa tetap memegang folder proyek di VF_DATA_DIR\work.
echo.
echo Kalau tadi render sedang berlangsung, folder di %USERPROFILE%\videoflow\work
echo akan dibersihkan worker berikutnya sebelum dipakai ulang.

echo.
choice /C YN /M "Sekalian stop container postgres + redis?"
if errorlevel 2 goto :done
where docker >nul 2>&1
if errorlevel 1 goto :no_docker
docker compose stop postgres redis
goto :done

:no_docker
echo [!] docker tidak di PATH; postgres/redis dibiarkan jalan.

:done
echo Selesai.
pause
exit /b 0
