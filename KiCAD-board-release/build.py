#!/usr/bin/env python3
"""Build the KiCad PCM package zip into dist/."""

import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
CONTENTS = ("metadata.json", "plugins", "resources")


def version() -> str:
    with open(os.path.join(ROOT, "metadata.json"), encoding="utf-8") as fh:
        meta_version = json.load(fh)["versions"][0]["version"]

    src = os.path.join(ROOT, "plugins", "kicad_release.py")
    code_version = ""
    with open(src, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("__version__"):
                code_version = line.split("=", 1)[1].strip().strip('"\'')
                break

    if meta_version != code_version:
        sys.exit(f"version mismatch: metadata.json says {meta_version}, "
                 f"kicad_release.py says {code_version}")
    return meta_version


def check_syntax() -> None:
    """Refuse to package a module that will not even parse."""
    for name in sorted(os.listdir(os.path.join(ROOT, "plugins"))):
        if not name.endswith(".py"):
            continue
        path = os.path.join(ROOT, "plugins", name)
        with open(path, encoding="utf-8") as fh:
            compile(fh.read(), path, "exec")


def build() -> str:
    ver = version()
    check_syntax()
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, f"BoardRelease-{ver}-pcm.zip")

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in CONTENTS:
            path = os.path.join(ROOT, item)
            if os.path.isfile(path):
                z.write(path, item)
                continue
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for f in sorted(filenames):
                    if f.endswith((".pyc", ".pyo")):
                        continue
                    full = os.path.join(dirpath, f)
                    z.write(full, os.path.relpath(full, ROOT))
    return out


if __name__ == "__main__":
    path = build()
    print(f"{os.path.relpath(path, ROOT)}  {os.path.getsize(path):,} bytes")
