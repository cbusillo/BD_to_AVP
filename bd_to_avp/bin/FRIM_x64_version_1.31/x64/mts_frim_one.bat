@echo off

set FILENAME=%1
set LAYOUT=%2

if %LAYOUT%A==A (
set FRIMFILE=%~n1.frim
) else (
set FRIMFILE=%~n1_%LAYOUT%.frim
)

echo %FRIMFILE%

echo [Video] >%FRIMFILE%
echo codec=mvc >>%FRIMFILE%
if NOT %LAYOUT%A==A (
echo layout=%LAYOUT% >>%FRIMFILE%
)
echo container=TS >>%FRIMFILE%
echo filename=%FILENAME% >>%FRIMFILE%
echo filename_dep=%FILENAME% >>%FRIMFILE%
echo. >>%FRIMFILE%

echo [Audio] >>%FRIMFILE%
echo endian=big >>%FRIMFILE%
echo container=TS >>%FRIMFILE%
echo filename=%FILENAME% >>%FRIMFILE%
