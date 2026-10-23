"""Download exact upstream corresponding sources for release redistribution.

The PyMuPDF sdist does not embed MuPDF, so both archives are provided.
Downloads are verified against pinned SHA-256 values before publication.
"""
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'dist' / 'sources'
DEST.mkdir(parents=True, exist_ok=True)
SOURCES = [
    {
        'filename': 'pymupdf-1.26.7.tar.gz',
        'url': 'https://files.pythonhosted.org/packages/48/d6/09b28f027b510838559f7748807192149c419b30cb90e6d5f0cf916dc9dc/pymupdf-1.26.7.tar.gz',
        'sha256': '71add8bdc8eb1aaa207c69a13400693f06ad9b927bea976f5d5ab9df0bb489c3',
        'bytes': 84327033,
    },
    {
        'filename': 'mupdf-1.26.12-source.tar.gz',
        'url': 'https://mupdf.com/downloads/archive/mupdf-1.26.12-source.tar.gz',
        'sha256': '6baf910928f404167ba49be6340195dec340795724722b331f5a2143f5aa0d01',
        'bytes': 64536473,
    },
]
for item in SOURCES:
    target = DEST / item['filename']
    if not target.is_file():
        temporary = target.with_suffix(target.suffix + '.part')
        urllib.request.urlretrieve(item['url'], temporary)
        actual = hashlib.sha256(temporary.read_bytes()).hexdigest()
        if actual != item['sha256']:
            raise RuntimeError(f"Checksum mismatch: {item['filename']}")
        temporary.replace(target)
    if target.stat().st_size != item['bytes'] or hashlib.sha256(target.read_bytes()).hexdigest() != item['sha256']:
        raise RuntimeError(f"Existing source archive failed verification: {item['filename']}")
(DEST / 'SOURCE_MANIFEST.json').write_text(json.dumps({'sources': SOURCES}, indent=2) + '\n', encoding='utf-8')
print('Verified both upstream source archives in dist/sources/.')
