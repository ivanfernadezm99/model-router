# Handoff model-router ↔ POSFrontReform (composer)

Última actualización: 2026-11-04 — commits `cbabac3`, `f782410`, `d46d4f9`, `7266434`.

Este documento explica cómo la galería de `/generacion` manda una pieza al
composer del POS **sin pedirle el JWT al usuario**, y las trampas que ya nos
costaron una sesión. Leerlo antes de tocar `web/templates/generacion.html`,
`web/app.py` o el login de POSFrontReform.

## Topología: una sola copia sirve

| Cosa | Valor |
|---|---|
| Copia servida | `~/Descargas/model-router` (rama `master`) |
| Copia **no** servida | `/media/.../proyectos/model-router` (duplicado; no atender `:5000`) |
| Servicio | `model-router-web.service` (systemd de usuario) |
| Puerto | `:5000` → `http://100.69.147.88:5000` (IP Tailscale de esta máquina) |
| Front del POS | `https://posfrontreform-stage-prod.up.railway.app` |
| Backend del POS | `https://posbackend-staging-prod.up.railway.app` |

No hay CI/CD: **pushear a GitHub no despliega**. Lo que sirve es el archivo
local. Flask cachea templates con `debug=False`, así que después de tocar una
plantilla hay que reiniciar:

```bash
systemctl --user restart model-router-web.service
systemctl --user is-active model-router-web.service
```

## Flujo

```
/generacion (sin JWT en sessionStorage)
  → clic "Enviar al composer"
  → redirectToPOSLogin()
  → https://posfrontreform-stage-prod.up.railway.app/#/login?returnUrl=<origen>
  → login del POS
  → https://.../generacion?token=<JWT>&businessId=<id>&roleName=<rol>
  → capturePOSHandoff(): guarda posJwt/posBusinessId en sessionStorage y limpia la URL
  → clic de nuevo
  → POST /generacion/upload-to-composer (proxy same-origin, evita CORS)
  → window.open('/#/social-media/composer?mediaUrl=...&caption=...')
```

## Reglas que no se negocian

1. **Nunca `prompt()` para pedir el JWT.** Si falta, se va al login del POS y
   se vuelve automáticamente.
2. **POSFrontReform usa hash routing.** Todas las URLs de handoff van con `/#/`:
   `/#/login` y `/#/social-media/composer`. Una URL sin `#` cae en el router
   equivocado.
3. **`forceLogin()` no debe pisar un `returnUrl` externo.** Los 401 de la
   pantalla de login llaman `forceLogin('/#/login')`; sin el guard
   (`^https?://`) borran el retorno externo que el componente de login acaba de
   guardar.
4. **El upload va por el proxy same-origin** `/generacion/upload-to-composer`.
   No llamar al backend del POS desde el navegador (CORS).
5. El JWT viaja en la URL al volver del login y se limpia de inmediato con
   `history.replaceState`. Aceptable en LAN por HTTP; **no exponer `:5000` a
   internet**.

## Galería: `web/static/generacion` es un symlink

`web/static/generacion` **no es una carpeta del repo**: es un symlink a

```
/media/.../proyectos/contenido-visual-ia/output/enlaces-chaco/pro
```

La ruta `/generacion` escanea esa carpeta en cada request (la carpeta es la
fuente de verdad), así que la galería muestra el output real del pipeline.

Consecuencias:

- **No commitear el symlink**: es una ruta absoluta de esta máquina. En un
  clone limpio `is_dir()` da `False`, el código hace `mkdir(..., exist_ok=True)`
  sobre un symlink roto y responde 500.
- **No hacer `git checkout -- web/static/generacion`**: escribiría los avisos
  viejos *a través del symlink* dentro del output real.
- El `.gitignore` ya ignora `web/static/generacion` y
  `web/static/generacion_backup/` (backup local de 53 MB).

## Verificación sin credenciales

```bash
# la página servida tiene la lógica nueva
curl -s http://127.0.0.1:5000/generacion | grep -c 'redirectToPOSLogin'   # 3
curl -s http://127.0.0.1:5000/generacion | grep -c 'prompt('              # 0

# tests
python3 -m pytest tests/test_web_upload.py -q          # 4 passed
python3 -m pytest tests/test_gateway_api_key.py -q     # 3 passed
```

Playwright: para probar el composer hay que mockear a nivel de **contexto**
(`ctx.route`), no de página. La pestaña del composer se abre con `window.open`
y comparte el contexto, no la página; con `page.route` esa pestaña hace llamadas
reales, el token falso devuelve 401 y el interceptor desloguea. Es un artefacto
del test, no un bug del producto.

## Errores conocidos

| Síntoma | Causa | Acción |
|---|---|---|
| `503 {"error":"no hay credencial de subida configurada en el servidor"}` | `COMPOSER_SERVICE_TOKEN` sin configurar y request sin `Authorization` | iniciar sesión (el flujo ya redirige) o setear la env var |
| 401 al subir | JWT vencido | el botón borra `posJwt` y redirige al login |
| La galería quedó vacía | el symlink apunta a una carpeta sin PNG/MP4 | revisar el output del pipeline |
| Cambié la plantilla y no se ve | Flask cachea templates (`debug=False`) | reiniciar el servicio |
