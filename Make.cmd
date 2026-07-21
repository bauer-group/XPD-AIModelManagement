@echo off
REM Windows twin of the Makefile. Keep the two in sync: a target that exists in
REM only one is a trap for whoever is on the other platform.
REM
REM The command strings here are identical to the ones CI runs. If they ever
REM diverge, CI is right and this file is the bug.
REM
REM Exit codes are propagated with `if errorlevel 1 exit /b 1`, NOT with
REM `cmd & exit /b %ERRORLEVEL%`. cmd.exe expands %VAR% when it parses a whole
REM line, so the latter returns the errorlevel from BEFORE the command ran, and
REM every target reports success no matter what happened.
setlocal
if "%~1"=="" goto help
if /I "%~1"=="help" goto help
if /I "%~1"=="install" goto install
if /I "%~1"=="install-dev" goto install-dev
if /I "%~1"=="lint" goto lint
if /I "%~1"=="format" goto format
if /I "%~1"=="type-check" goto type-check
if /I "%~1"=="test" goto test
if /I "%~1"=="test-cov" goto test-cov
if /I "%~1"=="integration" goto integration
if /I "%~1"=="minio-up" goto minio-up
if /I "%~1"=="minio-down" goto minio-down
if /I "%~1"=="render-docs" goto render-docs
if /I "%~1"=="docs" goto docs
if /I "%~1"=="build" goto build
if /I "%~1"=="clean" goto clean
if /I "%~1"=="pre-commit" goto pre-commit
if /I "%~1"=="all-checks" goto all-checks
echo Unknown target: %~1
exit /b 1

:help
echo install      - install the package
echo install-dev  - install with dev + docs extras, then pre-commit hooks
echo lint         - ruff check
echo format       - ruff check --fix
echo type-check   - mypy strict
echo test         - pytest, integration deselected
echo test-cov     - pytest with coverage (gate: 80%%)
echo integration  - pytest against the MinIO rig (needs minio-up)
echo minio-up     - start the local MinIO rig
echo minio-down   - stop the rig and delete its volumes
echo render-docs  - render README.MD + SECURITY.MD from docs/*.template.MD
echo docs         - render the templates, then build the MkDocs site
echo build        - build sdist + wheel
echo clean        - remove build/test artefacts
echo pre-commit   - run every pre-commit hook over the whole tree
echo all-checks   - lint + type-check + test-cov
exit /b 0

:install
python -m pip install -e .
exit /b %errorlevel%

:install-dev
python -m pip install -e ".[dev,docs]"
if errorlevel 1 exit /b 1
pre-commit install
exit /b %errorlevel%

:lint
ruff check src tests
exit /b %errorlevel%

:format
ruff check src tests --fix
exit /b %errorlevel%

:type-check
mypy src/bg_ai_model_management
exit /b %errorlevel%

:test
pytest -q -m "not integration"
exit /b %errorlevel%

:test-cov
pytest -q -m "not integration" --cov=src/bg_ai_model_management --cov-report=term-missing
exit /b %errorlevel%

REM Skips cleanly when the rig is down: the suite gates on AIMM_IT_ENDPOINT, so a
REM laptop without Docker sees skips rather than errors.
:integration
pytest -q -m integration
exit /b %errorlevel%

:minio-up
docker compose -f tests/integration/docker-compose.yml up -d --wait
exit /b %errorlevel%

:minio-down
docker compose -f tests/integration/docker-compose.yml down -v
exit /b %errorlevel%

:render-docs
python scripts/generate-docs.py
exit /b %errorlevel%

:docs
call "%~f0" render-docs
if errorlevel 1 exit /b 1
mkdocs build --strict
exit /b %errorlevel%

:build
python -m build
exit /b %errorlevel%

:clean
for %%d in (build dist .pytest_cache .mypy_cache .ruff_cache htmlcov site) do if exist %%d rmdir /s /q %%d
for /d %%d in (*.egg-info) do rmdir /s /q "%%d"
if exist .coverage del /q .coverage
exit /b 0

:pre-commit
pre-commit run --all-files
exit /b %errorlevel%

:all-checks
call "%~f0" lint
if errorlevel 1 exit /b 1
call "%~f0" type-check
if errorlevel 1 exit /b 1
call "%~f0" test-cov
exit /b %errorlevel%
