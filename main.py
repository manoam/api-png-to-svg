import io
import os
import base64
import subprocess
import tempfile
from typing import Optional
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from PIL import Image
import numpy as np
import vtracer

app = FastAPI(title="PNG to SVG API", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def analyze_complexity(img: Image.Image) -> dict:
    """Analyse la complexité d'une image pour estimer la fiabilité de conversion."""
    width, height = img.size
    total_pixels = width * height

    rgba = img.convert("RGBA")
    data = np.array(rgba)

    if total_pixels > 50000:
        step = int(np.sqrt(total_pixels / 50000))
        sampled = data[::step, ::step]
    else:
        sampled = data

    opaque_mask = sampled[:, :, 3] >= 10
    opaque_pixels = sampled[opaque_mask]

    if len(opaque_pixels) == 0:
        return {
            "width": width, "height": height, "hasAlpha": True,
            "uniqueColors": 0, "gradientRatio": 0.0, "transparentRatio": 1.0,
            "score": 0, "quality": "non_convertissable", "convertible": False,
        }

    quantized = opaque_pixels[:, :3] // 16
    unique_colors = len(set(map(tuple, quantized.tolist())))

    flat = data[:, :, 0].astype(np.int16)
    if flat.shape[0] > 1 and flat.shape[1] > 1:
        diff_h = np.abs(np.diff(flat, axis=1))
        diff_v = np.abs(np.diff(flat, axis=0))
        gradient_mask = ((diff_h > 0) & (diff_h < 16))
        gradient_mask_v = ((diff_v > 0) & (diff_v < 16))
        total_diffs = diff_h.size + diff_v.size
        gradient_count = np.sum(gradient_mask) + np.sum(gradient_mask_v)
        gradient_ratio = round(float(gradient_count / total_diffs), 2) if total_diffs > 0 else 0.0
    else:
        gradient_ratio = 0.0

    has_alpha = img.mode == "RGBA" or "transparency" in img.info
    transparent_ratio = round(float(1 - np.sum(opaque_mask) / sampled[:, :, 3].size), 2)

    score = 100
    if unique_colors > 500:
        score -= 40
    elif unique_colors > 200:
        score -= 25
    elif unique_colors > 50:
        score -= 10
    elif unique_colors > 10:
        score -= 5

    if gradient_ratio > 0.5:
        score -= 30
    elif gradient_ratio > 0.3:
        score -= 20
    elif gradient_ratio > 0.15:
        score -= 10

    if transparent_ratio > 0.3:
        score += 5

    if total_pixels > 4000000:
        score -= 10
    elif total_pixels > 2000000:
        score -= 5

    score = max(0, min(100, score))

    convertible = score >= 30
    if score >= 85:
        quality = "excellent"
    elif score >= 65:
        quality = "bon"
    elif score >= 45:
        quality = "moyen"
    elif score >= 30:
        quality = "faible"
    else:
        quality = "non_convertissable"

    return {
        "width": width, "height": height, "hasAlpha": has_alpha,
        "uniqueColors": unique_colors, "gradientRatio": gradient_ratio,
        "transparentRatio": transparent_ratio, "score": score,
        "quality": quality, "convertible": convertible,
    }


# --- Moteur 1 : vtracer (Rust) ---
def convert_vtracer(img: Image.Image, color_precision: int = 8,
                    filter_speckle: int = 4, mode: str = "color") -> str:
    processed = img.convert("L").convert("RGBA") if mode == "bw" else img.convert("RGBA")

    max_width = 2000 if mode == "bw" else 1200
    if processed.width > max_width:
        ratio = max_width / processed.width
        processed = processed.resize((max_width, int(processed.height * ratio)), Image.LANCZOS)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_in:
        processed.save(tmp_in, format="PNG")
        tmp_in_path = tmp_in.name

    tmp_out_path = tmp_in_path.replace(".png", ".svg")
    colormode = "binary" if mode == "bw" else "color"

    try:
        vtracer.convert_image_to_svg_py(
            tmp_in_path, tmp_out_path,
            colormode=colormode, hierarchical="stacked",
            filter_speckle=filter_speckle, color_precision=color_precision,
            layer_difference=16, corner_threshold=60,
            length_threshold=4.0, max_iterations=10,
            splice_threshold=45, path_precision=3,
        )
        with open(tmp_out_path, "r", encoding="utf-8") as f:
            return f.read()
    finally:
        for p in [tmp_in_path, tmp_out_path]:
            if os.path.exists(p):
                os.unlink(p)


# --- Moteur 2 : AutoTrace (C) ---
def convert_autotrace(img: Image.Image, color_count: int = 16,
                      mode: str = "color") -> str:
    processed = img.convert("L").convert("RGBA") if mode == "bw" else img.convert("RGBA")

    max_width = 1500
    if processed.width > max_width:
        ratio = max_width / processed.width
        processed = processed.resize((max_width, int(processed.height * ratio)), Image.LANCZOS)

    # AutoTrace ne gère pas bien la transparence, on remplace par du blanc
    bg = Image.new("RGBA", processed.size, (255, 255, 255, 255))
    bg.paste(processed, mask=processed.split()[3])
    rgb = bg.convert("RGB")

    with tempfile.NamedTemporaryFile(suffix=".ppm", delete=False) as tmp_in:
        rgb.save(tmp_in, format="PPM")
        tmp_in_path = tmp_in.name

    tmp_out_path = tmp_in_path.replace(".ppm", ".svg")

    try:
        cmd = [
            "autotrace",
            "-output-format", "svg",
            "-output-file", tmp_out_path,
            "-color-count", str(color_count),
        ]

        if mode == "bw":
            cmd.extend(["-color-count", "2"])

        cmd.append(tmp_in_path)

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
        )

        if result.returncode != 0:
            raise RuntimeError(f"AutoTrace error: {result.stderr}")

        with open(tmp_out_path, "r", encoding="utf-8") as f:
            svg = f.read()

        # Supprimer les rectangles blancs de fond
        import re
        svg = re.sub(
            r'<rect[^>]*fill\s*=\s*["\'](?:#fff(?:fff)?|white|rgb\(255,\s*255,\s*255\))["\'][^>]*/?>',
            '', svg, flags=re.IGNORECASE
        )

        return svg
    finally:
        for p in [tmp_in_path, tmp_out_path]:
            if os.path.exists(p):
                os.unlink(p)


# --- Moteur 3 : embed exact (base64) ---
def embed_png_as_svg(img: Image.Image) -> str:
    rgba = img.convert("RGBA")
    width, height = rgba.size

    buf = io.BytesIO()
    rgba.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n'
        f'  <image width="{width}" height="{height}" '
        f'href="data:image/png;base64,{b64}" />\n'
        f'</svg>'
    )


async def read_image(file: UploadFile) -> Image.Image:
    contents = await file.read()
    return Image.open(io.BytesIO(contents))


# --- Vérifier les moteurs disponibles ---
def get_available_engines():
    engines = ["vtracer", "exact"]
    try:
        result = subprocess.run(["autotrace", "--version"], capture_output=True, timeout=5)
        engines.append("autotrace")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return engines


@app.get("/")
def info():
    return {
        "name": "PNG to SVG API",
        "version": "4.0.0",
        "engines": get_available_engines(),
        "endpoints": {
            "POST /convert": {
                "description": "Convertit une image en SVG",
                "body": {
                    "type": "multipart/form-data",
                    "fields": {
                        "image": "(fichier) Image à convertir — requis",
                        "engine": "'vtracer' (défaut), 'autotrace', ou 'exact' (PNG base64 dans SVG)",
                        "mode": "'color' (défaut) ou 'bw' pour noir et blanc",
                        "format": "'json' (défaut) ou 'svg' pour le SVG brut",
                        "color_precision": "vtracer: 1-12 (défaut: 8) | autotrace: nombre de couleurs (défaut: 16)",
                        "filter_speckle": "vtracer: supprime artefacts (défaut: 4)",
                        "force": "'true' pour forcer si image trop complexe",
                        "download": "'true' pour télécharger le fichier",
                    },
                },
            },
            "POST /analyze": {
                "description": "Analyse la complexité d'une image",
                "body": "multipart/form-data avec champ 'image'",
            },
            "POST /batch": {
                "description": "Convertit plusieurs images (max 50)",
                "body": "multipart/form-data avec champ 'images'",
            },
        },
    }


@app.post("/analyze")
async def analyze(image: UploadFile = File(...)):
    img = await read_image(image)
    analysis = analyze_complexity(img)
    return {"filename": image.filename, **analysis}


@app.post("/convert")
async def convert(
    image: UploadFile = File(...),
    engine: Optional[str] = Form("vtracer"),
    mode: Optional[str] = Form("color"),
    format: Optional[str] = Form("json"),
    color_precision: Optional[int] = Form(None),
    filter_speckle: Optional[int] = Form(4),
    force: Optional[str] = Form("false"),
    download: Optional[str] = Form("false"),
):
    try:
        img = await read_image(image)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Impossible de lire l'image: {str(e)}")

    analysis = analyze_complexity(img)

    if not analysis["convertible"] and force != "true":
        return JSONResponse(
            status_code=422,
            content={
                "error": "Image trop complexe pour une conversion SVG fidèle",
                "analysis": analysis,
                "hint": "Ajoutez force=true pour forcer la conversion",
            },
        )

    actual_engine = (engine or "vtracer").strip().lower()
    actual_mode = (mode or "color").strip().lower()
    print(f"[convert] engine param raw={engine!r} actual={actual_engine!r} mode={actual_mode!r}")

    try:
        if actual_engine == "exact":
            svg = embed_png_as_svg(img)
        elif actual_engine == "autotrace":
            svg = convert_autotrace(
                img,
                color_count=color_precision or 16,
                mode=actual_mode,
            )
        else:  # vtracer (défaut)
            svg = convert_vtracer(
                img,
                color_precision=color_precision or 8,
                filter_speckle=filter_speckle or 4,
                mode=actual_mode,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur conversion ({actual_engine}): {str(e)}")

    if format == "json":
        return {
            "filename": image.filename,
            "engine_received": engine,
            "engine_used": actual_engine,
            "analysis": analysis,
            "svg": svg,
            "svgSize": len(svg.encode("utf-8")),
        }

    headers = {
        "X-Conversion-Score": str(analysis["score"]),
        "X-Conversion-Quality": analysis["quality"],
        "X-Engine": actual_engine,
    }

    if download == "true":
        name = os.path.splitext(image.filename or "image")[0]
        headers["Content-Disposition"] = f'attachment; filename="{name}.svg"'

    return Response(content=svg, media_type="image/svg+xml", headers=headers)


@app.post("/batch")
async def batch(images: list[UploadFile] = File(...)):
    if len(images) > 50:
        raise HTTPException(status_code=400, detail="Maximum 50 images")

    results = []
    for file in images:
        try:
            img = await read_image(file)
            analysis = analyze_complexity(img)

            if not analysis["convertible"]:
                results.append({
                    "filename": file.filename,
                    "status": "skipped",
                    "reason": "Image trop complexe",
                    "analysis": analysis,
                })
                continue

            svg = convert_vtracer(img)

            results.append({
                "filename": file.filename,
                "status": "converted",
                "analysis": analysis,
                "svg": svg,
                "svgSize": len(svg.encode("utf-8")),
            })
        except Exception as e:
            results.append({
                "filename": file.filename,
                "status": "error",
                "error": str(e),
            })

    summary = {
        "total": len(results),
        "converted": sum(1 for r in results if r["status"] == "converted"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "errors": sum(1 for r in results if r["status"] == "error"),
    }

    return {"summary": summary, "results": results}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3000)
