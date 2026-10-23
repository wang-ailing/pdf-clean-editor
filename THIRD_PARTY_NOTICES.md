# 第三方组件与许可

本程序使用下列开源组件。各组件的版权和许可归其原作者所有；本文件是索引，不替代第三方原始许可文本。原始许可保存在源码仓库的 `licenses/`，并随发布的单文件程序打包。

| 组件 | 用途 | 许可说明 |
| --- | --- | --- |
| [PyMuPDF 1.26.7 / MuPDF](https://github.com/pymupdf/PyMuPDF) | PDF 读取、文字提取、渲染、删除与保存 | Artifex 提供 AGPL 与商业双许可。本项目使用其开源 AGPL 许可版本。 |
| [Pillow](https://github.com/python-pillow/Pillow) | 页面图像显示与选区绘制 | Pillow / PIL 的 HPND 风格许可，并包含其依赖组件的独立许可。 |
| [CPython](https://www.python.org/) | Windows 单文件程序内的 Python 运行时 | Python Software Foundation License 及运行时所含组件的许可。 |
| [Tcl / Tk](https://www.tcl.tk/) | Tkinter 桌面界面 | Tcl / Tk 原始许可及其所含组件的许可。 |
| [PyInstaller](https://pyinstaller.org/) | Windows 单文件打包及启动引导程序 | GPL 许可附带允许打包应用程序的特别例外；以随附原文为准。 |

PyMuPDF 和 MuPDF 由 Artifex Software, Inc. 及其贡献者维护。Pillow、CPython、Tcl / Tk 和 PyInstaller 的版权声明，以及二进制依赖中的图像、压缩、字体等组件声明，保留在随附的原始许可文本中。

依赖的实际安装版本以 `requirements.txt` 和对应 Release 的构建信息为准。本项目本身采用 [GNU AGPL-3.0-or-later](LICENSE)。

## 对应源码

v1.0.0 的 Release 除应用源码标签外，同时提供以下官方对应源码副本：

- [PyMuPDF 1.26.7 源码](https://github.com/wang-ailing/pdf-clean-editor/releases/download/v1.0.0/pymupdf-1.26.7.tar.gz)
- [MuPDF 1.26.12 源码（包含第三方源码）](https://github.com/wang-ailing/pdf-clean-editor/releases/download/v1.0.0/mupdf-1.26.12-source.tar.gz)

来源与 SHA-256 记录在 Release 的 `SOURCE_MANIFEST.json`、`SHA256SUMS.txt` 中。完整依赖许可原文保存在 `licenses/`，单文件程序解包后同样包含此目录。
