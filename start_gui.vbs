' 无窗口启动用例编排器
' 双击本文件即可。比 .bat 更干净:.bat 运行时会闪一个 cmd 窗口,这个完全不会。
Option Explicit

Dim fso, shell, here, pyw, script
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = here & "\.venv\Scripts\pythonw.exe"
script = here & "\gui\main.py"

If Not fso.FileExists(pyw) Then
    MsgBox "找不到 " & pyw & vbCrLf & vbCrLf & _
           "请先创建虚拟环境并安装依赖:" & vbCrLf & _
           "  python -m venv .venv" & vbCrLf & _
           "  .venv\Scripts\pip install -r requirements.txt", 16, "启动失败"
    WScript.Quit 1
End If

' 0 = 隐藏窗口, False = 不等待退出
shell.CurrentDirectory = here
shell.Run """" & pyw & """ """ & script & """", 0, False
