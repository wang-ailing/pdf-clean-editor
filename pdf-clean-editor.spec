# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, copy_metadata

root = Path(SPECPATH).resolve()
pdf_datas, pdf_binaries, pdf_hidden = collect_all('pymupdf')
datas = pdf_datas + copy_metadata('PyMuPDF') + copy_metadata('Pillow')
datas += [(str(root / 'editor.ico'), '.'), (str(root / 'licenses'), 'licenses')]
if (root / 'LICENSE').is_file():
    datas.append((str(root / 'LICENSE'), '.'))

a = Analysis(
    [str(root / 'app.py')],
    pathex=[str(root)], binaries=pdf_binaries, datas=datas,
    hiddenimports=pdf_hidden, hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['numpy', 'onnxruntime', 'pymupdf4llm', 'pymupdf_layout',
              'pandas', 'scipy', 'matplotlib', 'pytest', 'IPython'],
    noarchive=False, optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='PDFCleanEditor-1.1.1-windows-x64',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
    icon=str(root / 'editor.ico'), version=str(root / 'version_info.txt'),
)
