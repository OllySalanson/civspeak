# PyInstaller spec for Civspeak.exe: one folder (dist\Civspeak), no console window.
# Build on Windows from the repo root, in a venv with packaging\requirements-windows.txt and Civspeak
# installed (pip install --no-deps .):  pyinstaller --noconfirm packaging\civspeak.spec
# One folder, not one file: it starts faster and antivirus programs flag it less often.
# Then check it:  dist\Civspeak\Civspeak.exe --selftest --report selftest.txt

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

import civspeak
from civspeak import winhelper

HERE = Path(SPECPATH)  # noqa: F821 (PyInstaller defines it)
WORK = Path(workpath)  # noqa: F821
WORK.mkdir(parents=True, exist_ok=True)

# The tray's star, drawn by Civspeak itself, is the program's icon too.
ICON = WORK / "civspeak.ico"
ICON.write_bytes(winhelper.ico_file())

numbers = tuple(int(n) for n in civspeak.__version__.split(".")[:3]) + (0,)
VERSION = VSVersionInfo(
    ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "080904B0",  # English (United Kingdom), Unicode
                    [
                        StringStruct("CompanyName", "Civspeak"),
                        StringStruct("FileDescription", "Civspeak: ask the Civ VI Civilopedia out loud"),
                        StringStruct("FileVersion", civspeak.__version__),
                        StringStruct("InternalName", "Civspeak"),
                        StringStruct("LegalCopyright", "Civspeak contributors. An unofficial fan tool, not made by Firaxis or 2K."),
                        StringStruct("OriginalFilename", "Civspeak.exe"),
                        StringStruct("ProductName", "Civspeak"),
                        StringStruct("ProductVersion", civspeak.__version__),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [0x0809, 1200])]),
    ],
)

datas = (
    collect_data_files("civspeak", includes=["web/*"])  # the answer page
    + collect_data_files("espeakng_loader")  # espeak-ng's pronunciation data, for George
    + collect_data_files("kokoro_onnx")  # Kokoro's own config.json (its phoneme vocabulary)
    + collect_data_files("faster_whisper")  # the silence detector's model (assets/silero_vad_*.onnx)
    + collect_data_files("webview")  # the page window's WebView2 libraries and scripts
    + collect_data_files("_sounddevice_data")  # PortAudio
    + copy_metadata("kokoro-onnx")  # kokoro-onnx asks for its own version
)
binaries = (
    collect_dynamic_libs("espeakng_loader")
    + collect_dynamic_libs("onnxruntime")
    + collect_dynamic_libs("ctranslate2")
    + collect_dynamic_libs("webview")
    + collect_dynamic_libs("_sounddevice_data")
)
hiddenimports = collect_submodules("civspeak") + collect_submodules("phonemizer") + ["webview.platforms.winforms", "clr"]

a = Analysis(  # noqa: F821
    [str(HERE / "launch.py")],
    pathex=[str(HERE / "stubs")],  # PyAV's stand-in (stubs\av); the real one is never installed for the build
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "matplotlib", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Civspeak",
    console=False,
    icon=str(ICON),
    version=VERSION,
    upx=False,  # UPX-packed programs are flagged by antivirus far more often
)
COLLECT(exe, a.binaries, a.datas, name="Civspeak", upx=False)  # noqa: F821
