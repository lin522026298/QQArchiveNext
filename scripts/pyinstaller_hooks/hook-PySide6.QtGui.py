"""The viewer renders PNG/JPEG/GIF/WebP; PDF image plugins are unused.

Filter the plugin before binary dependency analysis, avoiding an unrelated
Poppler/ICU runtime discovered on the build machine's global PATH.
"""
from pathlib import Path
from PyInstaller.utils.hooks.qt import add_qt6_dependencies

hiddenimports,binaries,datas=add_qt6_dependencies(__file__)
binaries=[item for item in binaries if Path(item[0]).name.casefold()!='qpdf.dll']
