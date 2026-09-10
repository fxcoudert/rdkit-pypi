"""Make an auditwheel-repaired RDKit wheel safe to load in Pyodide.

Pyodide eagerly loads every ``*.so`` under site-packages before running Python.
RDKit's Boost.Python modules must instead be initialized in Python import order,
after their shared C++ core is loaded.  Rename only the Python wrappers to
``*.so.wasm`` and install a small import finder for that suffix.  Flatten the
wrappers beside the shared core so Emscripten resolves every dependency using
the same canonical path, without the ``..`` components produced by auditwheel.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import sys
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


IMPORT_HOOK = '''# Installed by the rdkit-pypi Pyodide wheel build.
import sys as _sys

if _sys.platform == "emscripten":
    import importlib.abc as _importlib_abc
    import importlib.machinery as _importlib_machinery
    import importlib.util as _importlib_util
    import os as _os

    class _RDKitExtensionFinder(_importlib_abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname.split(".", 1)[0] != "rdkit" or not path:
                return None
            module_name = fullname.rsplit(".", 1)[-1]
            candidate = _os.path.normpath(
                _os.path.join(
                    _os.path.dirname(__file__),
                    "..",
                    "rdkit.libs",
                    module_name + ".so.wasm",
                )
            )
            if not _os.path.exists(candidate):
                return None
            loader = _importlib_machinery.ExtensionFileLoader(fullname, candidate)
            return _importlib_util.spec_from_file_location(
                fullname, candidate, loader=loader
            )

    _sys.meta_path.insert(0, _RDKitExtensionFinder())

'''


def _renamed_path(name: str) -> str:
    if name.startswith("rdkit/") and name.endswith(".so"):
        return "rdkit.libs/" + Path(name).name + ".wasm"
    return name


def _record(contents: dict[str, bytes], record_path: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name in contents:
        if name == record_path:
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(contents[name]).digest())
        writer.writerow(
            (name, "sha256=" + digest.rstrip(b"=").decode(), len(contents[name]))
        )
    writer.writerow((record_path, "", ""))
    return output.getvalue().encode()


def _canonicalize_wrapper_rpath(data: bytes, name: str, parent: Path) -> bytes:
    from auditwheel_emscripten.module import ModuleWritable

    with tempfile.TemporaryDirectory(prefix="rdkit-wrapper-", dir=parent) as tmp:
        wrapper = Path(tmp) / Path(name).name
        wrapper.write_bytes(data)
        with ModuleWritable(wrapper) as module:
            dylink = module.parse_dylink_section()
            if dylink.needed != ["librdkit_core.so"]:
                raise RuntimeError(
                    f"Expected {name} to depend only on librdkit_core.so; "
                    f"found {dylink.needed}"
                )
            dylink = dylink._replace(runtime_paths=["$ORIGIN"])
            return module.patch_dylink(module.encode_dylink_section(dylink))


def finalize(wheel: Path) -> int:
    with ZipFile(wheel) as source:
        infos: dict[str, ZipInfo] = {}
        contents: dict[str, bytes] = {}
        renamed = 0
        for original_info in source.infolist():
            data = source.read(original_info)
            name = _renamed_path(original_info.filename)
            if name != original_info.filename:
                renamed += 1
                data = _canonicalize_wrapper_rpath(
                    data, original_info.filename, wheel.parent
                )
            if name in contents:
                raise RuntimeError(f"Duplicate flattened wheel path: {name}")
            original_info.filename = name
            infos[name] = original_info
            contents[name] = data

    init_path = "rdkit/__init__.py"
    if init_path not in contents:
        raise RuntimeError(f"{wheel.name} does not contain {init_path}")
    if not renamed:
        raise RuntimeError(f"{wheel.name} does not contain RDKit extension modules")
    if b"_RDKitExtensionFinder" not in contents[init_path]:
        contents[init_path] = IMPORT_HOOK.encode() + contents[init_path]

    record_paths = [name for name in contents if name.endswith(".dist-info/RECORD")]
    if len(record_paths) != 1:
        raise RuntimeError(
            f"Expected one .dist-info/RECORD in {wheel.name}, found {len(record_paths)}"
        )
    record_path = record_paths[0]
    contents[record_path] = _record(contents, record_path)

    fd, temporary_name = tempfile.mkstemp(
        prefix=wheel.name + ".", suffix=".tmp", dir=wheel.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as destination:
            for name, data in contents.items():
                destination.writestr(infos[name], data)
        os.replace(temporary, wheel)
    finally:
        temporary.unlink(missing_ok=True)

    print(f"Finalized {wheel.name}: deferred {renamed} RDKit extension modules")
    return renamed


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} WHEEL_OR_DIRECTORY")
    target = Path(sys.argv[1])
    wheels = sorted(target.glob("*.whl")) if target.is_dir() else [target]
    if len(wheels) != 1:
        raise RuntimeError(f"Expected one wheel in {target}, found {len(wheels)}")
    finalize(wheels[0])


if __name__ == "__main__":
    main()
