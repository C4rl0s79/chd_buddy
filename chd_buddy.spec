# -*- mode: python ; coding: utf-8 -*-
# Budowa przenośnego .exe:  pyinstaller --noconfirm chd_buddy.spec
# Ustawienia zapisywane są OBOK exe (portable), nie w AppData.

block_cipher = None

a = Analysis(
    ["chd_buddy/main.py"],
    pathex=["."],
    binaries=[],
    datas=[
        # ("resources", "resources"),  # jeśli dodasz ikony/style
    ],
    hiddenimports=[],
    hookspath=[],
    excludes=["tkinter", "test", "unittest"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CHD Buddy",
    debug=False,
    strip=False,
    upx=True,
    console=False,          # GUI bez okna konsoli
    disable_windowed_traceback=False,
    icon=None,              # ustaw ścieżkę do .ico jeśli masz
)
