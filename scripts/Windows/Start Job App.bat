@echo off
rem Double-click to run: jobapp
title Job App
call "%~dp0..\..\jobapp.cmd"
rem Keep the window open if Job App stopped with an error, so the message can be read.
if errorlevel 1 pause
