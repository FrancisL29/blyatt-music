# Skill de Alexa para Blyatt

Pide canciones, artistas, álbumes y las playlists de tu cuenta, y suenan en el Echo sin tocar otro dispositivo.

```
Alexa, abre blyatt                                   -> "¿Qué quieres escuchar?"
Alexa, pídele a blyatt que ponga Bohemian Rhapsody
Alexa, dile a blyatt que ponga mi playlist bossa nova
Alexa, pídele a blyatt que ponga música de Queen
Alexa, pídele a blyatt que ponga el álbum Abbey Road
Alexa, pídele a blyatt que ponga mis me gusta
Mientras suena: "Alexa, siguiente / anterior / pausa / continúa / aleatorio / repite"
Alexa, pregúntale a blyatt qué está sonando
Alexa, dile a blyatt que me gusta esta canción        (la guarda en tus me gusta)
```

El servidor (`alexa.py`, rutas `/alexa` y `/alexa/a/...`) va con Blyatt; la skill solo hay que crearla una vez.

## 1. Cloudflare: dejar pasar a Alexa solo por `/alexa`

`blyatt.stream` está detrás de Cloudflare Access y Alexa no puede iniciar sesión. Zero Trust → Access → Applications →
**Add an application** → Self-hosted:

- Name: `Blyatt Alexa`
- Domain: `blyatt.stream`, path: `alexa`
- Policy: **Bypass**, Include → **Everyone**

Es seguro: cada petición a `/alexa` debe venir firmada por Amazon, y cada canción lleva su propia firma (el resto de
`blyatt.stream` sigue protegido).

## 2. Amazon: crear la skill (modo desarrollo, solo para tu cuenta)

Con la MISMA cuenta de Amazon de tu Echo, en <https://developer.amazon.com/alexa/console/ask>:

1. **Create Skill** → nombre `Blyatt`, idioma **Spanish (US)** (o el de tu Echo) → modelo **Custom** →
   hosting **Provision your own** → plantilla **Start from Scratch**.
2. **Interaction Model → JSON Editor**: pega el contenido de `skill-package/interactionModels/custom/es-US.json`
   (o `es-MX.json` / `es-ES.json` según el idioma) → **Save** → **Build skill**.
3. **Interfaces**: activa **Audio Player** → Save.
4. **Endpoint**: HTTPS → Default Region: `https://blyatt.stream/alexa` → certificado:
   *My development endpoint has a certificate from a trusted certificate authority* → Save.
5. **Test**: cambia "Off" por **Development**.

Si tu Echo está en otro idioma (español de México o España), añade ese idioma en *Language settings* y pega el mismo JSON.

## 3. Vincular tu cuenta

En la app de Blyatt (blyatt.stream o la app Android): tu foto → **Conectar Alexa** → código de 4 números.
Di "Alexa, abre blyatt" y, cuando te lo pida, "el código es 1 2 3 4". Listo: queda unida a tu cuenta de YouTube Music.

## Notas

- Si Alexa no entiende "blyatt", cambia el nombre de invocación (`invocationName`) por otro fácil de pronunciar
  (por ejemplo `mi música`) y vuelve a construir el modelo.
- Una skill propia no puede usar "Alexa, pon X en blyatt" como Spotify (eso es solo para servicios de música con
  acuerdo con Amazon): se dice "pídele a blyatt que ponga X".
- La primera vez que suena una canción el servidor la prepara (~2 s); después sale de la caché.
