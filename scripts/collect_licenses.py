"""Copy verbatim licenses from this build's installed runtime; no network needed."""
from importlib import metadata
import json
from pathlib import Path
import shutil
import sys
import tkinter

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'licenses'
DEST.mkdir(exist_ok=True)


def copy_distribution(name, destination):
    dist = metadata.distribution(name)
    copied = []
    for item in dist.files or ():
        rel = Path(str(item))
        if 'license' not in rel.name.lower() and 'copying' not in rel.name.lower():
            continue
        source = Path(dist.locate_file(item))
        if not source.is_file():
            continue
        parts = rel.parts
        # Only strip this distribution's leading dist-info directory. Vendored
        # libraries have their own dist-info folders and must keep distinct paths.
        tail = parts[1:] if parts[0].endswith('.dist-info') else ('package-files', *parts)
        target = DEST / destination / Path(*tail)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.append(str(target.relative_to(DEST)).replace('\\', '/'))
    if not copied:
        raise RuntimeError(f'No original license found for {name}')
    return {'version': dist.version, 'license_files': sorted(copied)}


manifest = {}
for package in ('PyMuPDF', 'Pillow', 'PyInstaller', 'pyinstaller-hooks-contrib', 'altgraph', 'packaging', 'pefile', 'pywin32-ctypes', 'setuptools'):
    manifest[package] = copy_distribution(package, package)

python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if not python_license.is_file():
    raise RuntimeError('The Python installation must include its original LICENSE.txt')
shutil.copyfile(python_license, DEST / 'Python-LICENSE.txt')
manifest['Python'] = {'version': sys.version.split()[0], 'license_files': ['Python-LICENSE.txt']}

tcl = tkinter.Tcl()
tcl_version = str(tcl.call('info', 'patchlevel'))
tcl_library = Path(tcl.call('info', 'library'))
for name, folder in [('Tcl', tcl_library), ('Tk', tcl_library.parent / ('tk' + str(tkinter.TkVersion)))]:
    local = folder / 'license.terms'
    target = DEST / (name + '-license.terms')
    if local.is_file():
        shutil.copyfile(local, target)
    if not target.is_file():
        raise RuntimeError(f'Missing {name} license: obtain the verbatim upstream license before building')
    manifest[name] = {'version': tcl_version, 'license_files': [target.name]}

for filename in ('AGPL-3.0.txt', 'MuPDF-COPYING', 'MuPDF-THIRD-PARTY-NOTICES.txt'):
    if not (DEST / filename).is_file():
        raise RuntimeError(f'Missing checked-in upstream license: {filename}')
(DEST / 'runtime-versions.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
print('Collected original runtime and build-tool licenses.')
