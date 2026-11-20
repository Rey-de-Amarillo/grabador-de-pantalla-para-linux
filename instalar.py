#!/usr/bin/python3
"""Install only this user's recorder files, with a dated reversible backup."""
from __future__ import annotations

import argparse
import ctypes.util
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


APP_ID = "ar.workraken.GrabadorPantalla"
APP_NAME = "Grabador de pantalla de Linux"
FILES = ("app.py", "capture.py", "geometry.py", "exclusion.py",
         "pixel_filter.py", "x11_underlay.py", "icon.svg")
GST_FACTORIES = ("ximagesrc", "queue", "videorate", "videoconvert", "capsfilter",
                 "x264enc", "h264parse", "mp4mux", "filesink", "adder",
                 "audioconvert", "audioresample", "avenc_aac", "aacparse", "pulsesrc")


def check_dependencies():
    if sys.version_info < (3, 10):
        raise RuntimeError("Se necesita Python 3.10 o posterior.")
    try:
        import cairo
        import gi
        for namespace, version in (("Gtk", "3.0"), ("Gdk", "3.0"),
                                   ("Gst", "1.0"), ("GstVideo", "1.0")):
            gi.require_version(namespace, version)
        from gi.repository import Gst, GstVideo, Gtk
        Gst.init(None)
    except (ImportError, ValueError) as error:
        raise RuntimeError("Faltan dependencias de Python, GTK o GStreamer. Consultá el README.") from error
    missing = [name for name in GST_FACTORIES if Gst.ElementFactory.find(name) is None]
    missing += [name for name in ("X11", "Xcomposite", "Xrender", "Xext")
                if ctypes.util.find_library(name) is None]
    if shutil.which("pactl") is None:
        missing.append("pactl (pulseaudio-utils)")
    if missing:
        raise RuntimeError("Faltan componentes: " + ", ".join(missing) + ". Consultá el README.")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def desktop_quote(value):
    # Desktop Exec quoting is separate from POSIX shell quoting.
    escaped = value.replace("%", "%%")
    for character in ("\\", '"', "`", "$"):
        escaped = escaped.replace(character, "\\" + character)
    return '"' + escaped + '"'


def main():
    parser = argparse.ArgumentParser(description=f"Instalar {APP_NAME} para este usuario.")
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local",
                        help="Destino de instalación; predeterminado: ~/.local")
    parser.add_argument("--check", action="store_true", help="Solo comprobar dependencias")
    parser.add_argument("--uninstall", action="store_true", help="Quitar únicamente los archivos del grabador")
    options = parser.parse_args()
    if options.check:
        check_dependencies()
        print("Dependencias disponibles.")
        return 0
    prefix = options.prefix.expanduser().resolve()
    if any(character in str(prefix) for character in ("\n", "\r", "\0")):
        raise RuntimeError("El destino contiene caracteres no admitidos.")
    if prefix in (Path("/"), Path.home(), Path("/usr"), Path("/usr/local")):
        raise RuntimeError("Elegí una carpeta de instalación propia, como ~/.local.")
    destination = prefix / "share/grabador-pantalla"
    executable = prefix / "bin/grabador-pantalla"
    desktop = prefix / "share/applications" / (APP_ID + ".desktop")
    targets = [destination / name for name in FILES] + [executable, desktop]
    # Read and validate the payload before changing any installed file.
    source = Path(__file__).resolve().parent / "grabador"
    if not options.uninstall:
        check_dependencies()
        for name in FILES:
            path = source / name
            if not path.is_file():
                raise RuntimeError(f"El paquete está incompleto: falta grabador/{name}.")
            if name.endswith(".py"):
                compile(path.read_bytes(), str(path), "exec")
    # Fixed allowlist only. Refuse links that could point at unrelated files.
    for target in targets:
        if target.is_symlink():
            raise RuntimeError(f"No se reemplazará el enlace: {target}")
        if target.exists() and not target.is_file():
            raise RuntimeError(f"El destino no es un archivo: {target}")
    for folder in (destination, executable.parent, desktop.parent):
        if folder.is_symlink():
            raise RuntimeError(f"No se instalará dentro de un enlace: {folder}")
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    backup = prefix / "share/grabador-pantalla-backups" / (stamp + ("-desinstalar" if options.uninstall else "-instalar"))
    suffix = 1
    while backup.exists():
        backup = backup.with_name(f"{stamp}-{suffix}")
        suffix += 1
    backup.mkdir(parents=True)
    existed = {}
    for target in targets:
        relative = target.relative_to(prefix)
        existed[str(relative)] = target.exists()
        if target.exists():
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
    (backup / "manifest.json").write_text(json.dumps({"prefix": str(prefix), "files": existed}, ensure_ascii=False, indent=2) + "\n")
    changed = []

    def replace(target, content, mode):
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".grabador-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
            temporary.chmod(mode)
            temporary.replace(target)
            changed.append(target)
            if target.read_bytes() != content:
                raise RuntimeError(f"No se verificó el archivo: {target}")
        finally:
            if temporary.exists():
                temporary.unlink()

    try:
        if options.uninstall:
            for target in targets:
                if target.exists():
                    target.unlink()
                    changed.append(target)
                    if target.exists():
                        raise RuntimeError(f"No se pudo quitar: {target}")
        else:
            for name in FILES:
                replace(destination / name, (source / name).read_bytes(), 0o644)
            wrapper = ("#!/bin/sh\nset -eu\n"
                       'if [ "${XDG_SESSION_TYPE:-}" = wayland ]; then\n'
                       "    echo 'El grabador necesita una sesión X11.' >&2\n    exit 1\nfi\n"
                       f"exec /usr/bin/python3 {shlex.quote(str(destination / 'app.py'))} \"$@\"\n")
            replace(executable, wrapper.encode(), 0o755)
            entry = (f"[Desktop Entry]\nType=Application\nName={APP_NAME}\n"
                     "Comment=Grabar una región de pantalla con audio\n"
                     f"Exec={desktop_quote(str(executable))}\nIcon={destination / 'icon.svg'}\n"
                     "Terminal=false\nCategories=AudioVideo;Recorder;\nStartupNotify=false\n"
                     "StartupWMClass=GrabadorPantalla\n")
            replace(desktop, entry.encode(), 0o644)
            validator = shutil.which("desktop-file-validate")
            if validator:
                subprocess.run([validator, str(desktop)], check=True, capture_output=True, text=True)
            assert all(digest(source / name) == digest(destination / name) for name in FILES)
    except BaseException:
        for target in reversed(changed):
            relative = target.relative_to(prefix)
            if existed[str(relative)]:
                shutil.copy2(backup / relative, target)
            elif target.exists():
                target.unlink()
        raise
    updater = shutil.which("update-desktop-database")
    if updater and desktop.parent.exists():
        subprocess.run([updater, str(desktop.parent)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if options.uninstall:
        print("Grabador desinstalado. Tus videos y tu estado se conservaron.")
    else:
        print(f"Instalado: {APP_NAME}\nEjecutable: {executable}")
        if executable.parent != Path.home() / ".local/bin":
            print(f"Usá esa ruta para abrirlo, o agregá {executable.parent} a PATH.")
    print(f"Copia de seguridad: {backup}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"No se completó la operación: {error}", file=sys.stderr)
        raise SystemExit(1)
