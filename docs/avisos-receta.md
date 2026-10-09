# Receta: avisos publicitarios con texto exacto (Qwen-Image + overlays)

Fecha: 2026-10-07. Estado: funcionando, verificado por OCR.

## La onda (resumen)

El modelo de difusión **nunca** escribe el texto: genera solo el fondo SIN texto.
Todo lo legible lo pega **código (PIL)** encima: 100% exacto en español,
marcas, precios y teléfonos. Así se evita el "Encules Chacho".

## Pipeline (flujo `image` del front)

1. Front (`image`): prompt de FONDO + 4 textos + chat opcional.
2. Gateway `/jobs/image` → worker `_run_comfyui_t2i()`:
   - al prompt se le agrega `BG_NO_TEXT_SUFFIX` ("Clean background only,
     absolutely no text, no letters, no numbers, no watermark, no logo").
   - ComfyUI `:8188` (Qwen-Image base 20B Q4 + VL fp8 + VAE), 768–1024px,
     30–40 steps euler/simple cfg 4.0 → ~7–13 min en 3090.
3. `apply_text_overlays()`: título (arriba, grande, mayúsculas), subtítulo
   (amarillo), contacto (botón verde a 0.80H), marca (abajo derecha).
   Fuente DejaVu Sans Bold (tiene ñ y acentos). Emojis se sanitizan (tofu).
4. `apply_chat_messages()`: burbujas WhatsApp (bot=verde derecha,
   cliente=blanca izquierda, máx 12). Auto-ajuste: achica letra hasta 16px
   y conserva los últimos mensajes sin pisar el contacto.
5. Archivo final en `model-router-output/{job_id}.png`, visible en
   Descargas del front con ⬇️.

## Prompts que funcionan

- **Fondo:** inglés, corto, comida + ambiente + "vertical composition,
  clean dark background". Sin pedir texto jamás.
- **Textos:** español directo del cliente, entrecomillados en el formulario
  (Título / Subtítulo / Contacto / Marca). Cortos: lo largo se achica solo.
- **Chat:** formato `bot: ...` / `cliente: ...`, estilo real del bot
  (voseo, pedido N°, transferencia, "te avisamos"). Ver
  `EcEnlacesChacoPython/ecchatbot/shared/agents/definitions.py` (ORDER_READY_AGENT)
  y `workers/message_processor.py` (carrito).

## Límites conocidos

- Qwen-Image-Edit (difusión editando imagen existente): DESCARTADO para
  texto exacto — deforma y cambia dígitos ajenos (tests 52 y 15 precios,
  ver fixes-2026-10-switch-jobs.md). Pesos borrados.
- Emojis en overlays: no salen (DejaVu sin glifos) — se filtran.
- 1024px rinde mejor texto que 768; cuesta ~13 min vs ~7 min.
- Un modelo a la vez (switch exclusivo): `qwen-image` tumba coder y viceversa.

## Archivos

- Pesos: `~/ComfyUI/models/{diffusion_models,text_encoders,vae}/`
- Worker: `src/jobs/worker.py` (`_qwen_image_workflow`, `_run_comfyui_t2i`,
  `apply_text_overlays`, `apply_chat_messages`)
- API: `src/jobs/router.py` (`ImageJobRequest.overlays/messages`)
- Front: `web/templates/index.html` (overlayWrap, ovChat, gating, loadNeededBtn)
- Tests: `tests/test_qwen_image_job.py` (workflow, descarga, timeout,
  overlays, chat, autofit, slots inválidos)

## Foto base + textos (2026-10-07)

- `image` acepta `photo` (upload o ruta): se usa la foto TAL CUAL de fondo +
  overlays/chat encima. Instantáneo, sin GPU, sin voltear el modelo activo.
- **Límite honesto:** es ADITIVO — no borra ni reemplaza texto existente de la
  foto. El "sacale X y poné Y" real (edición por difusión) se probó dos veces
  y falla (cambia dígitos ajenos, desobedece alcance). No se ofrece.

## Restyle (foto similar nueva) — 2026-10-07

- `image` + foto + `restyle=true`: img2img denoise 0.65 con Qwen-Image base
  (requiere GPU/switch) + overlays encima. Test `9d979bc4` con NUEVOMENU.png:
  completed, mantiene 1024×1536, marca verificada.
- Front: checkbox "Rediseñar la foto". 201 tests.

## Menú rediseñado completo (2026-10-07)

- `scripts/render_menu.py`: fondo restyleado oscurecido + lista 1–52 en 2
  columnas (nombre…precio) + botón contacto. Salida `menu-rediseno.png`.
- OCR verifica números, nombres y precios exactos (trunca nombres larguísimos).
