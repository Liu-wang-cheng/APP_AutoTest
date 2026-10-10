@echo off
chcp 65001 >nul 2>&1
REM 一键打包: 检查环境 -> 清理旧产物 -> PyInstaller(onefile 单exe) -> 验证
REM 产物: dist\AutoTest.exe  (单个 exe, 双击即用; 用户数据在其同级目录自动生成)
setlocal
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe
if not exist "%PY%" (
    echo [ERROR] 找不到 %PY%
    echo         请先: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

echo [1/4] 打包依赖检查...
"%PY%" -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo       未安装 PyInstaller, 正在安装...
    "%PY%" -m pip install pyinstaller || exit /b 1
)

echo [2/4] 跑测试(打包前先确认代码是好的)...
"%PY%" -m pytest -q || (
    echo [ERROR] 测试未通过, 中止打包
    exit /b 1
)

echo [3/4] 清理旧产物...
if exist build rmdir /s /q build
REM ★ 只删**程序文件**(exe + _internal\), 绝不删整个 dist\AutoTest\: 那里还放着用户数据
REM   (config\ Test_cases\ Test_img\ backups\ reports\ —— 跑一次打包版就会生成), 整目录
REM   删除会连带毁掉使用者的配置与用例(实测踩过: 构建前得先把它们挪走才敢跑)。
if exist "dist\AutoTest\AutoTest.exe" del /f /q "dist\AutoTest\AutoTest.exe"
if exist "dist\AutoTest\_internal" rmdir /s /q "dist\AutoTest\_internal"

echo [4/4] PyInstaller 打包(目录模式 onedir, 首次约需几分钟)...
"%PY%" -m PyInstaller AutoTest.spec --noconfirm --distpath dist --workpath build\pyi || exit /b 1

echo.
echo 验证产物...
"%PY%" tools\verify_package.py "dist\AutoTest\AutoTest.exe"
if errorlevel 1 (
    echo.
    echo [WARN] 产物验证未全通过, 请查看上面的 [FAIL]
    exit /b 1
)

echo.
echo 打分发 zip(只装 AutoTest.exe + _internal\, 用户数据不进包)...
set /p APPVER=<VERSION
"%PY%" tools\release.py --pack-only --version %APPVER%
if errorlevel 1 (
    echo.
    echo [WARN] 打 zip 失败
    exit /b 1
)

echo.
echo ============================================
echo  打包完成:
echo    可运行目录: dist\AutoTest\  (双击里面的 AutoTest.exe)
echo    分发用 zip: dist\AutoTest_v%APPVER%.zip  ^<-- 发这个
echo  说明: 目录模式(onedir)启动约 0.7 秒; onefile 每次启动要解压 400MB(约 2.8 秒)
echo ============================================
endlocal
