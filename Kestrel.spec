# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Kestrel — builds a one-folder windowed app (fast startup).
#
#   py -m PyInstaller Kestrel.spec      (or run build.bat)
#
# Output: dist\Kestrel\Kestrel.exe  (distribute the whole dist\Kestrel folder, zipped)

from PyInstaller.utils.hooks import collect_all

datas = [("assets", "assets")]
binaries = []
hiddenimports = [
    "mapview",
    "kestrel", "kestrel.inspector", "kestrel.diagnostics",
    "kestrel.models", "kestrel.textreport", "kestrel.tabular", "kestrel.repair",
    "kestrel.crsguess", "kestrel.arcgis", "kestrel.esri",
    "xlrd",                      # old .xls reader; no GDAL driver exists for it
    "kestrel.pointcloud", "laspy", "lazrs",      # LAS / LAZ point clouds
    "kestrel.batch",                             # folder auditing
]

# Pull data files (proj.db, GDAL data), native libraries, and submodules for the
# geospatial stack so the frozen app can read CRS/UTM info and open files.
# (openpyxl is for the CSV/Excel coordinate reader.)
for pkg in ("pyogrio", "pyproj", "rasterio", "shapely", "openpyxl"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# Trim things the GUI never uses (keeps the exe smaller). NOTE: pandas must stay —
# pyogrio imports it at load time.
excludes = [
    "geopandas", "fiona", "matplotlib", "tkinter",
    "PyQt5", "PyQt6", "PySide2", "IPython", "jupyter",
    "notebook", "pytest", "sphinx",
    # Optional rasterio deps Kestrel never uses — ~95 MB of dead weight.
    "scipy", "PIL", "Pillow",
    "botocore", "boto3", "s3transfer", "awscrt", "jmespath",
    # pyogrio's optional Arrow bridge (read/write_arrow, use_arrow=True). Kestrel reads
    # and writes through pyogrio.raw only, so this is ~78 MB we never execute.
    "pyarrow",
]

a = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,         # one-folder build: binaries/data live alongside, not inside
    name="Kestrel",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                 # windowed app (no console)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
)

# One-folder (onedir) layout -> dist\Kestrel\Kestrel.exe. This runs directly from its files
# with NO unpacking at launch, so startup is near-instant (onefile re-extracts ~179 MB every
# time, which is slow especially with antivirus / network profiles).
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Kestrel",
)
