#!/usr/bin/python3
"""Build a downloadable source archive from an explicit portable allowlist."""
from pathlib import Path
import argparse
import hashlib
import tarfile


ROOT = Path(__file__).resolve().parent
FILES = ("README.md", "LICENSE", ".gitignore", "VERSION", "instalar.sh", "instalar.py",
         "desinstalar.sh", "iniciar.sh", "empaquetar.py", "grabador/app.py", "grabador/capture.py",
         "grabador/geometry.py", "grabador/exclusion.py", "grabador/pixel_filter.py",
         "grabador/x11_underlay.py", "grabador/icon.svg")


def main():
    parser = argparse.ArgumentParser(description="Crear el archivo de distribución.")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    options = parser.parse_args()
    version = (ROOT / "VERSION").read_text().strip()
    if not version or any(character not in "0123456789.-" for character in version):
        raise SystemExit("VERSION no es válida.")
    for name in FILES:
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"Falta un archivo de la distribución: {name}")
        if name.endswith(".py"):
            compile(path.read_bytes(), str(path), "exec")
    options.output.mkdir(parents=True, exist_ok=True)
    title = f"grabador-de-pantalla-para-linux-{version}"
    archive = options.output / (title + ".tar.gz")
    with tarfile.open(archive, "w:gz") as bundle:
        for name in FILES:
            bundle.add(ROOT / name, arcname=f"{title}/{name}", recursive=False)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(archive.suffix + ".sha256").write_text(f"{checksum}  {archive.name}\n")
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
