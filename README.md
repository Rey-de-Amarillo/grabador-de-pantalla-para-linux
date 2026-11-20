# Grabador de pantalla de Linux

Grabá una parte de la pantalla con audio del equipo y micrófono desde una barra pequeña que podés mover.

## Descargar e instalar

Descargá el archivo `.tar.gz` de la sección **Releases**, extraelo y abrí una terminal en esa carpeta. También podés descargar el repositorio con **Code → Download ZIP**.

En Linux Mint, Ubuntu o Debian, instalá las dependencias:

```sh
sudo apt install python3 python3-gi python3-gi-cairo python3-cairo \
  gir1.2-gtk-3.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  gstreamer1.0-x pulseaudio-utils libx11-6 libxcomposite1 libxrender1 libxext6
```

Instalá el grabador para tu usuario:

```sh
./instalar.sh
```

Abrilo desde el menú de aplicaciones como **Grabador de pantalla de Linux**, o con:

```sh
grabador-pantalla
```

Podés usarlo directamente desde la carpeta, sin instalarlo, ejecutando `./iniciar.sh`. La instalación del grabador no necesita `sudo`; el comando de dependencias anterior sí.

## Controles

| Control | Función |
| --- | --- |
| Cámara | Seleccionar un área y comenzar. Durante la grabación, detener y guardar el video. |
| Contador | Tiempo grabado, sin contar las pausas. Al abrir el grabador empieza en `00:00`. |
| Pausa / Play | Pausar y reanudar la misma grabación. |
| Cuadrado | Descartar la grabación en curso o enviar el último video guardado a la Papelera. |
| Carpeta | Abrir la carpeta donde se guardan los videos. |
| X, a la derecha de la carpeta | Cerrar el grabador. Si está grabando, termina de guardar el video. |

Arrastrá la barra manteniendo apretado el botón izquierdo del mouse. Su posición se conserva entre aperturas. `Esc` cancela la selección del área. El atajo opcional `Ctrl + Mayús + F10` detiene y guarda; para habilitarlo instalá `gir1.2-keybinder-3.0`.

Los videos se guardan en **Vídeos → Grabaciones de pantalla**, en formato MP4 con video H.264 y audio AAC. Al guardar, se abre el reproductor predeterminado. La barra del grabador y sus descripciones se excluyen de la grabación.

## Requisitos

- Python 3.10 o posterior, GTK 3 y GStreamer 1.
- Sesión gráfica **X11** con composición y las extensiones XComposite, XRender y XShape. Probado en Linux Mint 22.3 con Cinnamon.
- PulseAudio o PipeWire con compatibilidad PulseAudio, una salida de audio y una entrada de micrófono disponibles.
- Reproductor predeterminado para abrir MP4.

Wayland no está soportado. En la pantalla de inicio de sesión elegí una sesión X11. El programa captura el audio del sistema y el micrófono predeterminados, sin cambiar su volumen ni su configuración.

Para comprobar las dependencias sin instalar archivos:

```sh
./instalar.sh --check
```

## Desinstalar

Cerrá el grabador y ejecutá, desde esta carpeta:

```sh
./desinstalar.sh
```

La desinstalación conserva tus videos y tu estado local. La instalación hace una copia fechada de los archivos propios que reemplaza y permite indicar otro destino con `--prefix /ruta`.

## Licencia

MIT. Podés descargar, usar, modificar y distribuir el programa.
