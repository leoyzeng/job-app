@echo off
rem Double-click to run: jobapp demo
title Job App (sample data)
call "%~dp0..\..\jobapp.cmd" demo
rem Keep the window open if Job App stopped with an error, so the message can be read.
if errorlevel 1 pause
