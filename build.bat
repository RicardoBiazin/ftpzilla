@echo off
setlocal
rem ---------------------------------------------------------------------------
rem Gera o FTPZilla.exe.
rem
rem   build.bat          - versao completa (FTP, FTPS, SFTP, Drive, OneDrive)
rem   build.bat basico   - so FTP/FTPS/SFTP; fica bem menor, sem requests
rem
rem O .spec NAO e versionado: ele e gerado aqui a cada vez, para nao ficar
rem desatualizado em relacao a este arquivo, que e o que as pessoas leem.
rem ---------------------------------------------------------------------------
cd /d "%~dp0"

rem O PyInstaller sobrescreve dist\FTPZilla.exe; com o programa aberto isso
rem falha la no fim, depois de dois minutos de trabalho, com um traceback de
rem PermissionError que nao diz o obvio. Melhor avisar agora.
tasklist /FI "IMAGENAME eq FTPZilla.exe" 2>nul | find /I "FTPZilla.exe" >nul
if not errorlevel 1 (
  echo.
  echo *** O FTPZilla esta aberto. Feche a janela antes de gerar o executavel
  echo     - o arquivo dist\FTPZilla.exe fica travado enquanto ele roda.
  exit /b 1
)

echo.
echo === Gerando o icone
python ferramentas\gerar_icone.py || goto :erro

echo.
echo === Conferindo se tudo importa
python -m compileall -q ftpzilla app.py || goto :erro
python -c "import ftpzilla, ftpzilla.remotes, ftpzilla.ui.janela; print('ok')" || goto :erro

set EXTRA=
if /i "%~1"=="basico" (
  echo.
  echo === Modo basico: sem os tipos de nuvem
  set EXTRA=--exclude-module requests --exclude-module urllib3
)

rem O tkdnd vem com DLLs que o PyInstaller nao acha sozinho - sem o
rem collect-all, arrastar do Explorer para a janela para de funcionar no exe.
python -c "import tkinterdnd2" 2>nul && set EXTRA=%EXTRA% --collect-all tkinterdnd2

echo.
echo === Empacotando
python -m PyInstaller ^
  --noconfirm --clean --onefile --windowed ^
  --name FTPZilla ^
  --icon ftpzilla\recursos\ftpzilla.ico ^
  --add-data "ftpzilla\recursos;recursos" ^
  --exclude-module pyftpdlib ^
  --exclude-module pytest ^
  --exclude-module numpy ^
  --exclude-module PIL ^
  %EXTRA% ^
  app.py || goto :erro

echo.
echo === Pronto: dist\FTPZilla.exe
dir /b dist\FTPZilla.exe
goto :fim

:erro
echo.
echo *** A geracao falhou. Veja a mensagem acima.
exit /b 1

:fim
endlocal
