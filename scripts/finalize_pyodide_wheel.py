"""Make an auditwheel-repaired RDKit wheel safe to load in Pyodide.

Pyodide eagerly loads every ``*.so`` under site-packages before running Python.
RDKit's Boost.Python modules must instead be initialized in Python import order,
after their shared C++ core is loaded.  Rename only the Python wrappers to
``*.so.wasm`` and install a small import finder for that suffix.  Pin each
wrapper's dependency to the wheel-relative core path so Emscripten reuses the
core which Pyodide preloaded instead of instantiating it once per wrapper.
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
            for directory in path:
                candidate = _os.path.join(directory, module_name + ".so.wasm")
                if _os.path.exists(candidate):
                    loader = _importlib_machinery.ExtensionFileLoader(
                        fullname, candidate
                    )
                    return _importlib_util.spec_from_file_location(
                        fullname, candidate, loader=loader
                    )
            return None

    _sys.meta_path.insert(0, _RDKitExtensionFinder())

'''


def _renamed_path(name: str) -> str:
    if name.startswith("rdkit/") and name.endswith(".so"):
        return name + ".wasm"
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


def _pin_core_dependencies(contents: dict[str, bytes], parent: Path) -> int:
    # The repair environment already contains auditwheel-emscripten.  Its
    # public repair API deliberately leaves NEEDED entries as bare filenames;
    # for a shared Boost.Python registry we need its lower-level, relocatable
    # wheel-path form so every wrapper resolves exactly the same module.
    from auditwheel_emscripten.module import ModuleWritable

    core_names = [
        name for name in contents if name.endswith(".libs/librdkit_core.so")
    ]
    if len(core_names) != 1:
        raise RuntimeError(
            f"Expected one vendored librdkit_core.so, found {len(core_names)}"
        )
    core_name = core_names[0]

    patched = 0
    with tempfile.TemporaryDirectory(prefix="rdkit-wheel-", dir=parent) as tmp:
        root = Path(tmp)
        core_path = root / core_name
        core_path.parent.mkdir(parents=True)
        core_path.write_bytes(contents[core_name])

        wrapper_names = [
            name
            for name in contents
            if name.startswith("rdkit/") and name.endswith(".so")
        ]
        for name in wrapper_names:
            wrapper_path = root / name
            wrapper_path.parent.mkdir(parents=True, exist_ok=True)
            wrapper_path.write_bytes(contents[name])
            with ModuleWritable(wrapper_path) as module:
                needed = module.parse_dylink_section().needed
                core_dependencies = [
                    dependency
                    for dependency in needed
                    if Path(dependency).name == "librdkit_core.so"
                ]
                if len(needed) != 1 or len(core_dependencies) != 1:
                    raise RuntimeError(
                        f"Expected {name} to depend only on librdkit_core.so; "
                        f"found {needed}"
                    )
                contents[name] = module.patch_needed_path(
                    {core_dependencies[0]: core_path.resolve()}
                )
            patched += 1
    return patched


def finalize(wheel: Path) -> int:
    with ZipFile(wheel) as source:
        entries = [(info, source.read(info)) for info in source.infolist()]

    original_contents = {info.filename: data for info, data in entries}
    pinned = _pin_core_dependencies(original_contents, wheel.parent)

    infos: dict[str, ZipInfo] = {}
    contents: dict[str, bytes] = {}
    renamed = 0
    for original_info, _ in entries:
        original_name = original_info.filename
        data = original_contents[original_name]
        name = _renamed_path(original_name)
        if name != original_name:
            renamed += 1
        original_info.filename = name
        infos[name] = original_info
        contents[name] = data

    init_path = "rdkit/__init__.py"
    if init_path not in contents:
        raise RuntimeError(f"{wheel.name} does not contain {init_path}")
    if not renamed:
        raise RuntimeError(f"{wheel.name} does not contain RDKit extension modules")
    if pinned != renamed:
        raise RuntimeError(f"Pinned {pinned} wrappers but renamed {renamed}")
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

    print(
        f"Finalized {wheel.name}: pinned and deferred {renamed} "
        "RDKit extension modules"
    )
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
