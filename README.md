# PDF 擦除与高亮

一个轻量的 Windows PDF 文字编辑工具。划选文字后，选区上方只显示两个操作：**高亮**、**擦除**。

擦除会实际删除选中的页面文字，并在保存时完整重写 PDF、清理废弃对象；图片和背景予以保留。文件大小取决于原始 PDF 与修改内容，**不保证每次擦除后都缩小**。

## 下载使用

从 [最新 Release](https://github.com/wang-ailing/pdf-clean-editor/releases/latest) 下载 Windows 64 位 `.exe`，双击运行即可。支持 Windows 10 / 11，不需要安装 Python。单文件版本首次启动需要解包，请稍候。

所有 PDF 处理均在本机完成，不上传文件，不需要联网。

1. 按 **Ctrl+O** 打开 PDF，或使用左上角菜单。
2. 按住鼠标左键划选文字，松手后点击选区上方的 **高亮** 或 **擦除**。
3. 按 **Ctrl+S** 保存到一个新的 PDF 文件。原文件保持不变。

| 操作 | 快捷方式 |
| --- | --- |
| 打开 PDF | Ctrl+O |
| 保存副本 | Ctrl+S |
| 撤销 / 重做 | Ctrl+Z / Ctrl+Y |
| 缩放 | Ctrl+鼠标滚轮 |
| 上一页 / 下一页 | PgUp / PgDn，或右下角翻页按钮 |

## 支持范围

- 黄色高亮按照所选文字范围添加。
- 擦除只针对选中的文字，不采用白色矩形遮盖文字。
- 支持在保存前撤销、重做；关闭程序后不保留本次撤销历史。
- PDF 必须具有可提取的文字层。纯扫描件或已将文字转为图形的页面无法划选；本工具不包含 OCR。
- 保存后显示原文件与副本的大小，便于实际比较。

## 从源码运行

需要 Python 3.10 或更新版本，并安装带有 Tkinter 的 Python 发行版。建议使用虚拟环境。

```powershell
python -m pip install -r requirements.txt
python app.py
```

也可以在启动时传入 PDF 路径：

```powershell
python app.py "example.pdf"
```

## 构建 Windows 单文件程序

在 Windows 64 位 Python 环境中安装依赖后执行：

```powershell
.\build.ps1
```

构建使用 PyInstaller，输出为 `dist\PDFCleanEditor-1.0.0-windows-x64.exe`。发布时应同时提供本仓库的对应版本源码、应用许可和第三方许可说明。第三方原始许可也随程序打包。

## 测试

```powershell
python test_editor.py
python test_selection.py
```

测试覆盖 PDF 编辑、文字选择、保存和原文件保护等行为。

## 许可

本项目采用 **GNU AGPL-3.0-or-later**，详见 [LICENSE](LICENSE)。PDF 引擎使用 PyMuPDF 1.26.7 / MuPDF 的开源许可版本；其他依赖许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 `licenses/`。

版本变化见 [CHANGELOG.md](CHANGELOG.md)。
