"""Record portable release hashes and dependency versions, without local paths."""
import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / 'dist'
exe = DIST / 'PDFCleanEditor-1.1.1-windows-x64.exe'
sha = hashlib.sha256(exe.read_bytes()).hexdigest()
manifest = {
    'application': 'PDFCleanEditor', 'version': '1.1.1',
    'platform': 'windows-x64', 'python': platform.python_version(),
    'dependencies': {x: metadata.version(x) for x in ('PyMuPDF', 'Pillow', 'PyInstaller', 'pyinstaller-hooks-contrib')},
    'asset': {'filename': exe.name, 'bytes': exe.stat().st_size, 'sha256': sha},
    'source_hash_format': 'SHA-256 of source bytes normalized to LF line endings',
    'sources': {x: hashlib.sha256((ROOT / x).read_bytes().replace(b'\r\n', b'\n')).hexdigest() for x in
                ('app.py', 'pdf_engine.py', 'text_selection.py', 'pdf-clean-editor.spec', 'requirements.txt', 'requirements-build.txt')},
}
(DIST / 'build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
(DIST / 'SHA256SUMS.txt').write_text(sha + '  ' + exe.name + '\n', encoding='ascii')
print(json.dumps(manifest['asset']))
