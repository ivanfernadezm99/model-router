# Feature: qwen-image-edit

## Objective

Dejar operativo un modelo local open-weights que edite texto exacto dentro de imágenes (caso: 52 precios de `NUEVOMENU.png`), con calidad comparable a GPT-image.

## Problem

Los modelos de difusión actuales (SDXL) redibujan la imagen y deforman texto. ComfyUI está roto (faltan `alembic`, `comfy_aimdo`). Qwen-Image-Edit 20B Q4 es el candidato (mejor open preservando texto); fallback FLUX.1-Kontext.

## Why

Pedido explícito del usuario 2026-10-06: "quiero un modelo que me sirva para esto" (editar precios del menú). Investigación previa verificó disco (119GB/375GB libres) y VRAM (24GB, Q4 entra).

## Scope

- Incluye: reparar env ComfyUI, descargar pesos Qwen-Image-Edit Q4, workflow de edición, prueba con NUEVOMENU.png, validación de fidelidad de texto español.
- Excluye: integración al gateway/model-router (se decide después de validar calidad), reentrenamiento, SDXL.

## Constraints

- No romper servicios activos (llama :8082/:8083, wan :8189, gateway :8000, web :5000).
- Descargas ~25GB al disco grande si es posible.
- VRAM: convivir con el switch exclusivo (el servicio nuevo debe usar su propio puerto y pararse cuando no se usa).

## Authorized scope

Autorizado por el usuario: reparar ComfyUI, descargar pesos, crear workflow y servicio nuevo. Commits en rama feature del repo model-router si se tocan archivos del repo.

## Acceptance criteria

- [ ] ComfyUI levanta sin crash-loop.
- [ ] Job de edición sobre NUEVOMENU.png completa sin OOM.
- [ ] Evaluación honesta: ¿cuántos de los precios salen exactos? (criterio: ≥90% para darlo por bueno, si no → probar Kontext).

## Tasks

- [x] 1. Reparar entorno ComfyUI (alembic, comfy_aimdo) y verificar que sdxl levanta — DONE 2026-10-06: causa raíz era ExecStart con python del sistema en vez del venv; fix en ~/.config/systemd/user/sdxl.service + pip install -r requirements en venv. Verificado: active (running), :8188/system_stats responde (ComfyUI 0.34.0). (route: delegated, trigger: preparation/env)
- [x] 2. Descargar pesos Qwen-Image-Edit Q4 + nodos/workflow ComfyUI — DONE. Todo junto en ~/ComfyUI/models/{diffusion_models,text_encoders,vae}/ (disco rápido, sin duplicados, extra_model_paths.yaml eliminado). sdxl reiniciado y ve los 3 archivos.
- [x] 3. Probar edición con NUEVOMENU.png y evaluar fidelidad — HECHO 2026-10-07: workflow `~/ComfyUI/user/default/workflows/qwen-image-edit-menu-test.json` (UnetLoaderGGUF Q4 + CLIPLoader qwen_image + VAE + TextEncodeQwenImageEdit + KSampler euler/simple 40 steps cfg 4.0 denoise 1.0 + ModelSamplingFlux 1024×1536). Salida `menu-edit-test_00001_.png`. VEREDICTO HONESTO: parcial — layout y secciones preservados, varios precios bien ($27.000, $23.000, $20.000, $17.000) PERO texto deformado en varios lados ("Papas Oheddar y Pamela"), precio mal (ítem 51 $16.000 vs $12.000) y numeración corrida. No llega al ≥90%. NO integrar al combo todavía.
- [ ] 4. Decidir: reintentar por partes (1 sección por pasada) o fallback a Kontext

## Resultado final 2026-10-07
- Qwen-Image-Edit DESCARTADO para precios exactos (test 52 y test 15 fallaron, ver docs/fixes-2026-10-switch-jobs.md).
- Pesos edit borrados. Quedan encoder VL fp8 + VAE para futuro text-to-image.
- Tarea 4 redirigida: modelo de generación para publicidad (Qwen-Image base) en vez de Kontext.

## image cableado 2026-10-07
- [x] Registry qwen-image + worker _run_comfyui_t2i + front NUEVO descripto + 192 tests + validación real job 098ef89a completed.
