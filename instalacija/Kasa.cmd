@echo off
chcp 65001 >nul
title Kasa (s prozorom)
REM Kasa s crnim prozorom, da se vide poruke i greske. Obicno se pokrece ikonom Kasa (bez prozora).
"%~dp0python\python.exe" "%~dp0program\pokreni.py" %*
echo.
echo Program je zavrsio. Ako gore pise greska, poslikajte ovaj prozor.
pause
