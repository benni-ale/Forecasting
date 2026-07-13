@echo off
set BIN=%LOCALAPPDATA%\Programs\MiKTeX\miktex\bin\x64
set ROOT=%~dp0latex

cd /d "%ROOT%"
if not exist build mkdir build

rem Rimuove artefatti stale nella root: pdflatex li preferirebbe a quelli in build/
rem causando citazioni/riferimenti irrisolti (main.bbl vecchio letto al posto del nuovo).
del /q main.aux main.bbl main.bcf main.blg main.out main.toc main.run.xml main.log main.synctex.gz 2>nul

echo [1/4] pdflatex...
"%BIN%\pdflatex.exe" -interaction=nonstopmode -output-directory=build main.tex
if errorlevel 1 goto :error

echo [2/4] biber...
"%BIN%\biber.exe" build/main
if errorlevel 1 goto :error

echo [3/4] pdflatex...
"%BIN%\pdflatex.exe" -interaction=nonstopmode -output-directory=build main.tex
if errorlevel 1 goto :error

echo [4/4] pdflatex...
"%BIN%\pdflatex.exe" -interaction=nonstopmode -output-directory=build main.tex
if errorlevel 1 goto :error

echo.
echo OK: %ROOT%\build\main.pdf
start "" "%ROOT%\build\main.pdf"
exit /b 0

:error
echo.
echo Compilazione fallita. Controlla i messaggi sopra.
pause
exit /b 1
