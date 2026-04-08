import io
import os
from typing import Optional
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from PIL import Image
import numpy as np
import vtracer

app = FastAPI(title="PNG to SVG API", version="3.0.0")

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

    # Convertir en RGBA
    rgba = img.convert("RGBA")
    data = np.array(rgba)

    # Échantillonner si trop grand
    if total_pixels > 50000:
        step = int(np.sqrt(total_pixels / 50000))
        sampled = data[::step, ::step]
    else:
        sampled = data

    # Pixels non transparents
    opaque_mask = sampled[:, :, 3] >= 10
    opaque_pixels = sampled[opaque_mask]

    if len(opaque_pixels) == 0:
        return {
            "width": width, "height": height, "hasAlpha": True,
            "uniqueColors": 0, "gradientRatio": 0.0, "transparentRatio": 1.0,
            "score": 0, "quality": "non_convertissable", "convertible": False,
        }

    # Compter les couleurs uniques (quantifiées par blocs de 16)
    quantized = opaque_pixels[:, :3] // 16
    unique_colors = len(set(map(tuple, quantized.tolist())))

    # Détecter les dégradés
    flat = data[:, :, 0].astype(np.int16)  # canal rouge pour simplifier
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

    # Ratio de transparence
    has_alpha = img.mode == "RGBA" or "transparency" in img.info
    transparent_ratio = round(float(1 - np.sum(opaque_mask) / sampled[:, :, 3].size), 2)

    # Score de fiabilité
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
        "width": width,
        "height": height,
        "hasAlpha": has_alpha,
        "uniqueColors": unique_colors,
        "gradientRatio": gradient_ratio,
        "transparentRatio": transparent_ratio,
        "score": score,
        "quality": quality,
        "convertible": convertible,
    }


def convert_with_vtracer(
    img: Image.Image,
    mode: str = "color",
    color_precision: int = 8,
    filter_speckle: int = 4,
    corner_threshold: int = 60,
    path_precision: int = 3,
) -> str:
    """Convertit une image en SVG avec vtracer."""

    # Préparer l'image selon le mode
    if mode == "bw":
        processed = img.convert("L").convert("RGBA")
    else:
        processed = img.convert("RGBA")

    # Redimensionner si trop grand
    max_width = 2000 if mode == "bw" else 1200
    if processed.width > max_width:
        ratio = max_width / processed.width
        new_height = int(processed.height * ratio)
        processed = processed.resize((max_width, new_height), Image.LANCZOS)

    # Convertir en bytes RGBA bruts
    raw_bytes = processed.tobytes()
    width, height = processed.size
    colormode = "binary" if mode == "bw" else "color"

    svg = vtracer.convert_raw_image_to_svg(
        raw_bytes,
        img_width=width,
        img_height=height,
        colormode=colormode,
        hierarchical="stacked",
        filter_speckle=filter_speckle,
        color_precision=color_precision,
        layer_difference=16,
        corner_threshold=corner_threshold,
        length_threshold=4.0,
        max_iterations=10,
        splice_threshold=45,
        path_precision=path_precision,
    )

    return svg


async def read_image(file: UploadFile) -> Image.Image:
    """Lit un fichier uploadé et retourne une image PIL."""
    contents = await file.read()
    return Image.open(io.BytesIO(contents))


@app.get("/")
def info():
    return {
        "name": "PNG to SVG API",
        "version": "3.0.0",
        "engine": "vtracer (Rust)",
        "endpoints": {
            "POST /convert": {
                "description": "Convertit une image en SVG avec analyse de fiabilité",
                "body": {
                    "type": "multipart/form-data",
                    "fields": {
                        "image": "(fichier) Image à convertir — requis",
                        "mode": "'color' (défaut) ou 'bw' pour noir et blanc",
                        "format": "'json' (défaut) pour SVG + analyse, ou 'svg' pour le SVG brut",
                        "color_precision": "Nombre de couleurs 1-12 (défaut: 8, plus = plus fidèle)",
                        "filter_speckle": "Supprimer les petits artefacts (défaut: 4)",
                        "force": "'true' pour forcer la conversion même si image trop complexe",
                        "download": "'true' pour télécharger le fichier SVG",
                    },
                },
            },
            "POST /analyze": {
                "description": "Analyse la complexité d'une image sans la convertir",
                "body": "multipart/form-data avec champ 'image' (fichier)",
            },
            "POST /batch": {
                "description": "Convertit plusieurs images (max 50)",
                "body": "multipart/form-data avec champ 'images' (fichiers multiples)",
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
    mode: Optional[str] = Form("color"),
    format: Optional[str] = Form("json"),
    color_precision: Optional[int] = Form(8),
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

    try:
        svg = convert_with_vtracer(
            img,
            mode=mode or "color",
            color_precision=color_precision or 8,
            filter_speckle=filter_speckle or 4,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur conversion: {str(e)}")

    if format == "json":
        return {
            "filename": image.filename,
            "analysis": analysis,
            "svg": svg,
            "svgSize": len(svg.encode("utf-8")),
        }

    headers = {
        "X-Conversion-Score": str(analysis["score"]),
        "X-Conversion-Quality": analysis["quality"],
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

            svg = convert_with_vtracer(img)

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
