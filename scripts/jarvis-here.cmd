@echo off
rem Marks the current folder as Jarvis's active project.
rem Run this in any project folder, then say things like
rem "Hey Jarvis, run the tests" and Jarvis works in this folder.
if not exist "%LOCALAPPDATA%\OpenJarvis" mkdir "%LOCALAPPDATA%\OpenJarvis"
echo %CD%> "%LOCALAPPDATA%\OpenJarvis\active_project.txt"
echo Jarvis active project is now: %CD%
