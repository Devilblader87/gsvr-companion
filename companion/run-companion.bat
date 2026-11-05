@echo off
rem GSVR companion v0 launcher (needs Python 3 from python.org)
cd /d "%~dp0.."
py -3 -I companion\companion.py %*
pause
