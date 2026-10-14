@echo off
rem Windows launcher: PowerShell and cmd pick a runner by file extension,
rem so the extensionless Python launcher needs this to run as .	idal-daemon
python "%~dp0tidal-daemon" %*
