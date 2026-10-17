@echo off
rem Deliberately NOT switching to "chcp 65001": legacy conhost mis-accounts
rem wide (CJK) glyph widths under the UTF-8 code page, printing every Chinese
rem character twice and breaking line wrapping. Keeping the system default
rem code page (usually 936 on Chinese Windows) renders correctly. The report
rem files are still written as UTF-8 by PowerShell, independent of the
rem console code page.
rem
rem Keep this file ASCII-only: cmd reads .bat in the OEM code page, so UTF-8
rem comments would show up as mojibake.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-acceptance.ps1" %*
set EXITCODE=%ERRORLEVEL%
if not "%GEN2VEC_NO_PAUSE%"=="1" pause
exit /b %EXITCODE%
