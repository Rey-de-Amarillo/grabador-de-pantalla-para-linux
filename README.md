# Grabador de pantalla de Linux

Grabá una parte de la pantalla con audio del equipo y micrófono, o grabá solamente tu voz, desde la misma barra pequeña que podés mover.

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

El acceso **Grabador de voz de Linux** y el comando `grabador-voz` abren la misma aplicación en modo voz. Ambos accesos comparten una única barra de **299 × 48 píxeles**; los botones de cámara y micrófono permiten elegir el modo sin abrir otra ventana grande. La instalación reemplaza el acceso del grabador de voz anterior y conserva sus datos locales.

## Controles

| Control | Función |
| --- | --- |
| Cámara | Elegir grabación de pantalla. Elegir el modo no inicia una grabación. |
| Micrófono | Elegir grabación de voz. Elegir el modo no inicia una grabación. |
| Play / Pausa | Play comienza a grabar el modo elegido; en pantalla, primero permite seleccionar un área. Durante la grabación, pausa y reanuda. |
| Botón rojo de detener | El botón del modo elegido se vuelve rojo mientras se graba. Presionarlo detiene y guarda el archivo. |
| Contador | Tiempo grabado, sin contar las pausas. Al abrir el grabador empieza en `00:00`. |
| Cuadrado | Descartar la grabación en curso o enviar el último archivo guardado a la Papelera. |
| Carpeta | Abrir la carpeta de grabaciones del modo elegido. |
| X, a la derecha de la carpeta | Cerrar el grabador. Si está grabando, termina de guardar el archivo. |

Arrastrá la barra manteniendo apretado el botón izquierdo del mouse. Su posición se conserva entre aperturas. `Esc` cancela la selección del área. El atajo opcional `Ctrl + Mayús + F10` detiene y guarda; para habilitarlo instalá `gir1.2-keybinder-3.0`.

Los videos se guardan en **Vídeos → Grabaciones de pantalla**, en formato MP4 con video H.264 y audio AAC. La voz se guarda en **Música → Grabaciones de voz**, en WAV mono de 24 bits y 48 kHz. El modo voz usa únicamente un micrófono físico disponible; no mezcla el audio de los parlantes. El modo se cambia antes de comenzar una grabación.

Al detener, el modo voz reduce el ruido de fondo estable y atenúa algunos clics aislados que quedan entre palabras. Después, sólo si detecta voz, ajusta el nivel de las grabaciones bajas, con una ganancia limitada de hasta 18 dB y un pico objetivo de −3 dBFS. Una grabación que contiene sólo ruido no recibe ese aumento de nivel. El procesamiento conserva el formato WAV y no cambia el volumen global del micrófono. Los clics que coinciden con la voz pueden seguir oyéndose; no se elimina la reverberación de la habitación.

Al detener se guarda el archivo y se abre el reproductor predeterminado. La barra del grabador y sus descripciones se excluyen de los videos. Los dos modos conservan los mismos controles de pausa, reanudar, descartar y guardar.

## Requisitos

- Python 3.10 o posterior, GTK 3 y GStreamer 1.
- Sesión gráfica **X11** con composición y las extensiones XComposite, XRender y XShape. Probado en Linux Mint 22.3 con Cinnamon.
- PulseAudio o PipeWire con compatibilidad PulseAudio, una salida de audio y una entrada de micrófono disponibles.
- Reproductor predeterminado para abrir MP4 y WAV.
- Biblioteca opcional `libspeexdsp1` para reducir ruido en modo voz. Si falta o el filtro falla, se conserva el audio original sin limpieza ni amplificación; el instalador avisa si la biblioteca no está disponible.

Wayland no está soportado. En la pantalla de inicio de sesión elegí una sesión X11. El modo pantalla captura el audio del sistema y el micrófono predeterminados; el modo voz usa sólo el micrófono. La aplicación no cambia el volumen ni la configuración global de los dispositivos.

Para comprobar las dependencias sin instalar archivos:

```sh
./instalar.sh --check
```

## Desinstalar

Cerrá el grabador y ejecutá, desde esta carpeta:

```sh
./desinstalar.sh
```

La desinstalación conserva tus videos, audios y tu estado local. La instalación hace una copia fechada y verificada de los archivos propios que reemplaza, restaura esos archivos si falla y permite indicar otro destino con `--prefix /ruta`.

## Licencia

MIT. Podés descargar, usar, modificar y distribuir el programa.
