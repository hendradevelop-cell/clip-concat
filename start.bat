@echo off
setlocal EnableExtensions

rem ============================================================
rem  videoflow - start.bat
rem
rem  Menyalakan infra (postgres + redis), API FastAPI, worker
rem  Celery, dan dashboard Next.js, masing-masing di jendela
rem  sendiri supaya log-nya bisa dibaca sambil jalan.
rem
rem  Semua path dihitung dari lokasi file ini, jadi file ini boleh
rem  dibuat shortcut-nya di desktop.
rem ============================================================

cd /d "%~dp0"
set "PYDIR=%~dp0services\py"
set "PY=%PYDIR%\.venv\Scripts\python.exe"
set "DASH=%~dp0dashboard"

rem --no-pause = jangan tunggu tombol saat dijalankan dari skrip.
set "NO_PAUSE="
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"

echo ============================================================
echo  videoflow  ^|  API :8000  worker  dashboard :3000
echo ============================================================
echo.

if exist "%PY%" goto :have_venv
echo [ERROR] virtualenv belum ada: %PYDIR%\.venv
echo.
echo   cd /d "%PYDIR%"
echo   python -m venv .venv
echo   .venv\Scripts\python.exe -m pip install -e ".[captions,dev]"
echo   copy .env.example .env
echo.
call :wait_ok
exit /b 1

:have_venv

if exist "%LOCALAPPDATA%\Programs\Concat\concat.exe" goto :have_concat
echo [!] concat.exe tidak ada di %LOCALAPPDATA%\Programs\Concat.
echo     Worker tetap jalan, tapi tahap render akan gagal. Kalau binary-nya
echo     ada di tempat lain, isi VF_CONCAT_BIN di %PYDIR%\.env
echo.
:have_concat

rem ---- 1. Docker Desktop + postgres/redis -------------------
where docker >nul 2>&1
if errorlevel 1 goto :no_docker

docker info >nul 2>&1
if not errorlevel 1 goto :compose_up

echo [..] Docker Desktop belum jalan, dimulai...
start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
for /l %%i in (1,1,30) do (
  docker info >nul 2>&1 && goto :compose_up
  timeout /t 3 /nobreak >nul
)
echo [ERROR] Docker daemon tidak siap dalam 90 detik. Buka Docker Desktop
echo         sendiri, lalu jalankan ulang file ini.
call :wait_ok
exit /b 1

:compose_up
docker compose up -d postgres redis
if errorlevel 1 goto :compose_fail

echo [..] menunggu Postgres menerima koneksi di :5432...
for /l %%i in (1,1,40) do (
  call :port_busy 5432
  if not errorlevel 1 goto :pg_ready
  timeout /t 2 /nobreak >nul
)
echo [ERROR] Postgres tidak muncul di :5432. Isi folder volume mungkin rusak:
echo         docker compose down dan mulai ulang.
call :wait_ok
exit /b 1

:pg_ready
echo [ok] postgres + redis jalan.
echo.
goto :services

:compose_fail
echo [ERROR] "docker compose up -d postgres redis" gagal. Lihat pesan di atas.
call :wait_ok
exit /b 1

:no_docker
echo [ERROR] docker tidak ada di PATH. Postgres + Redis harus sudah jalan
echo         sendiri, atau pasang Docker Desktop.
call :wait_ok
exit /b 1

:services
rem ---- 2. API ------------------------------------------------
rem Dua uvicorn pada port yang sama membuat yang kedua mati diam-diam,
rem dan dua worker pada satu antrean membuat job diambil proses yang mana
rem saja -- karena itu tiap proses dicek dulu sebelum memulai.
call :port_busy 8000
if errorlevel 1 goto :start_api
echo [skip] API sudah mendengarkan :8000
goto :api_done
:start_api
start "videoflow API :8000" /D "%PYDIR%" cmd /k ""%PY%" -m uvicorn videoflow.api.server:app --host 127.0.0.1 --port 8000"
echo [ok] API: http://127.0.0.1:8000
:api_done

rem ---- 3. Worker Celery --------------------------------------
rem --pool=solo itu wajib di Windows: pool bawaan mati dengan
rem PermissionError [WinError 5] pada semlock. Penyaringnya harus dibatasi ke
rem python.exe: command line proses pengecek ini sendiri juga memuat kata
rem "videoflow.worker", tanpa filter ia selalu menyimpulkan worker sudah jalan.
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -like '*videoflow.worker*' }) { exit 1 }"
if errorlevel 1 goto :worker_skip
start "videoflow worker" /D "%PYDIR%" cmd /k ""%PY%" -m celery -A videoflow.worker:celery_app worker -Q videoflow --pool=solo -l info --logfile=worker.log"
echo [ok] worker: antrean videoflow (log: services\py\worker.log)
goto :worker_done
:worker_skip
echo [skip] worker Celery sudah jalan
:worker_done

rem ---- 4. Dashboard ------------------------------------------
if exist "%DASH%\node_modules" goto :dash_run
echo [..] dependency dashboard belum terpasang, menjalankan npm install...
pushd "%DASH%"
call npm install
popd
:dash_run
call :port_busy 3000
if errorlevel 1 goto :start_dash
echo [skip] dashboard sudah mendengarkan :3000
goto :dash_done
:start_dash
start "videoflow dashboard :3000" /D "%DASH%" cmd /k call npm run dev
echo [ok] dashboard: http://localhost:3000
:dash_done

echo.
echo ============================================================
echo  Buka http://localhost:3000  (API docs: http://127.0.0.1:8000/docs)
echo  Job berstatus running dari sesi sebelumnya tidak diambil ulang otomatis:
echo  batalkan lalu kirim ulang dari dashboard.
echo  Menutup jendela = menghentikan prosesnya. Untuk merapikan semua:
echo  stop.bat
echo ============================================================
call :wait_ok
exit /b 0

rem ============================================================
rem  :port_busy <port> - errorlevel 1 kalau ada yang LISTENING.
rem  Spasi setelah port ikut dicari supaya :8000 tidak cocok
rem  dengan :80001 di kolom alamat netstat.
rem ============================================================
:port_busy
netstat -ano | findstr /C:":%~1 " | findstr /C:"LISTENING" >nul 2>&1
exit /b %errorlevel%

rem ============================================================
rem  :wait_ok - jeda sampai tombol ditekan, kecuali --no-pause.
rem ============================================================
:wait_ok
if "%NO_PAUSE%"=="1" exit /b 0
pause
exit /b 0
