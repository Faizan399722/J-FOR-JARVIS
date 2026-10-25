@echo off
title Building JARVIS.exe
echo ============================================
echo   JARVIS - Native Edition  ^|  EXE builder
echo ============================================
python --version >nul 2>&1 || (echo Python not found. Install Python 3.11+ from python.org and tick "Add to PATH". & pause & exit /b 1)
echo Installing what the app needs...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo Building JARVIS.exe (this takes a minute or two)...
python -m PyInstaller --noconfirm --onefile --windowed --name JARVIS --icon icon.ico --add-data "jarvis-hud.html;." --add-data "icon.ico;." --collect-all webview --collect-all pystray --collect-all pyttsx3 --hidden-import pyttsx3.drivers.sapi5 --hidden-import speech_recognition --hidden-import pyaudio --hidden-import pystray jarvis.py
echo.
echo DONE!  Your app is here:  dist\JARVIS.exe
echo You can move JARVIS.exe anywhere - Desktop, USB, another PC.
pause
