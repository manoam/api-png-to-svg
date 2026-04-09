"""
Vectorizer intelligent — convertit des images complexes en SVG avec :
- Détection de régions (segmentation)
- Détection de dégradés → <linearGradient> / <radialGradient> SVG
- K-means clustering pour regrouper les couleurs
- Traçage des contours avec approximation Bézier
"""

import cv2
import numpy as np
import svgwrite
from PIL import Image
from sklearn.cluster import MiniBatchKMeans
from io import BytesIO


def pil_to_cv2(img: Image.Image) -> np.ndarray:
    """Convertit PIL Image en numpy array BGR pour OpenCV."""
    rgba = img.convert("RGBA")
    arr = np.array(rgba)
    # RGBA → BGRA
    bgra = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGRA)
    return bgra


def detect_gradient(region_pixels: np.ndarray, region_mask: np.ndarray,
                    bbox: tuple) -> dict | None:
    """
    Détecte si une région contient un dégradé linéaire.
    Retourne les infos du gradient ou None si couleur unie.
    """
    x, y, w, h = bbox
    if w < 4 or h < 4:
        return None

    # Extraire les pixels de la région
    ys, xs = np.where(region_mask[y:y+h, x:x+w] > 0)
    if len(ys) < 10:
        return None

    # Couleurs des pixels de la région (RGB)
    colors = region_pixels[y:y+h, x:x+w][region_mask[y:y+h, x:x+w] > 0]
    if len(colors) < 10:
        return None

    # Vérifier la variance des couleurs
    color_std = np.std(colors, axis=0)
    mean_std = np.mean(color_std)

    # Si très peu de variance → couleur unie, pas un dégradé
    if mean_std < 8:
        return None

    # Si trop de variance → trop complexe pour un simple gradient
    if mean_std > 80:
        return None

    # Détecter la direction du dégradé (horizontal vs vertical)
    # Calculer la variance moyenne des couleurs le long de chaque axe
    h_variance = 0
    v_variance = 0

    crop = region_pixels[y:y+h, x:x+w].astype(np.float32)
    mask_crop = region_mask[y:y+h, x:x+w]

    # Variance horizontale (colonne par colonne)
    for col in range(0, w, max(1, w // 10)):
        col_mask = mask_crop[:, col] > 0
        if np.sum(col_mask) > 2:
            col_colors = crop[:, col][col_mask]
            h_variance += np.mean(np.std(col_colors, axis=0))

    # Variance verticale (ligne par ligne)
    for row in range(0, h, max(1, h // 10)):
        row_mask = mask_crop[row, :] > 0
        if np.sum(row_mask) > 2:
            row_colors = crop[row, :][row_mask]
            v_variance += np.mean(np.std(row_colors, axis=0))

    # Déterminer la direction
    is_horizontal = h_variance < v_variance

    # Calculer les couleurs de début et fin du gradient
    if is_horizontal:
        # Gradient de gauche à droite
        left_mask = mask_crop[:, :max(1, w//4)] > 0
        right_mask = mask_crop[:, max(1, 3*w//4):] > 0
        left_colors = crop[:, :max(1, w//4)][left_mask]
        right_colors = crop[:, max(1, 3*w//4):][right_mask]
    else:
        # Gradient de haut en bas
        top_mask = mask_crop[:max(1, h//4), :] > 0
        bottom_mask = mask_crop[max(1, 3*h//4):, :] > 0
        left_colors = crop[:max(1, h//4), :][top_mask]
        right_colors = crop[max(1, 3*h//4):, :][bottom_mask]

    if len(left_colors) == 0 or len(right_colors) == 0:
        return None

    start_color = np.mean(left_colors, axis=0).astype(int)
    end_color = np.mean(right_colors, axis=0).astype(int)

    # Vérifier que les couleurs de début et fin sont différentes
    color_diff = np.linalg.norm(start_color - end_color)
    if color_diff < 15:
        return None

    return {
        "type": "linear",
        "horizontal": is_horizontal,
        "start_color": tuple(start_color.tolist()),
        "end_color": tuple(end_color.tolist()),
        "bbox": bbox,
    }


def detect_radial_gradient(region_pixels: np.ndarray, region_mask: np.ndarray,
                           bbox: tuple) -> dict | None:
    """Détecte un dégradé radial (du centre vers l'extérieur)."""
    x, y, w, h = bbox
    if w < 10 or h < 10:
        return None

    crop = region_pixels[y:y+h, x:x+w].astype(np.float32)
    mask_crop = region_mask[y:y+h, x:x+w]

    cx, cy = w // 2, h // 2
    max_r = min(w, h) // 2

    if max_r < 5:
        return None

    # Échantillonner les couleurs par distance au centre
    inner_colors = []
    outer_colors = []

    for dy in range(-max_r, max_r, max(1, max_r // 5)):
        for dx in range(-max_r, max_r, max(1, max_r // 5)):
            py, px = cy + dy, cx + dx
            if 0 <= py < h and 0 <= px < w and mask_crop[py, px] > 0:
                dist = np.sqrt(dx*dx + dy*dy)
                if dist < max_r * 0.3:
                    inner_colors.append(crop[py, px])
                elif dist > max_r * 0.7:
                    outer_colors.append(crop[py, px])

    if len(inner_colors) < 3 or len(outer_colors) < 3:
        return None

    center_color = np.mean(inner_colors, axis=0).astype(int)
    edge_color = np.mean(outer_colors, axis=0).astype(int)

    color_diff = np.linalg.norm(center_color - edge_color)
    if color_diff < 15:
        return None

    return {
        "type": "radial",
        "center_color": tuple(center_color.tolist()),
        "edge_color": tuple(edge_color.tolist()),
        "center": (x + cx, y + cy),
        "radius": max_r,
        "bbox": bbox,
    }


def rgb_to_hex(r, g, b) -> str:
    """Convertit RGB en hex."""
    return f"#{max(0,min(255,int(r))):02x}{max(0,min(255,int(g))):02x}{max(0,min(255,int(b))):02x}"


def contour_to_svg_path(contour: np.ndarray, epsilon_factor: float = 0.002) -> str:
    """Convertit un contour OpenCV en path SVG avec approximation."""
    epsilon = epsilon_factor * cv2.arcLength(contour, True)
    approx = cv2.approxPolyDP(contour, epsilon, True)

    if len(approx) < 3:
        return ""

    points = approx.reshape(-1, 2)
    path = f"M {points[0][0]},{points[0][1]}"
    for p in points[1:]:
        path += f" L {p[0]},{p[1]}"
    path += " Z"
    return path


def vectorize(img: Image.Image, num_colors: int = 16,
              min_area: int = 50, detail: float = 0.002) -> str:
    """
    Vectorise une image en SVG avec détection de dégradés.

    Args:
        img: Image PIL en entrée
        num_colors: Nombre de couleurs pour le clustering (plus = plus fidèle)
        min_area: Surface minimum d'une région (en pixels)
        detail: Niveau de détail des courbes (plus petit = plus détaillé)

    Returns:
        SVG string
    """
    # Préparer l'image
    rgba = img.convert("RGBA")
    width, height = rgba.size

    # Redimensionner si trop grand
    max_dim = 1200
    if max(width, height) > max_dim:
        scale = max_dim / max(width, height)
        width = int(width * scale)
        height = int(height * scale)
        rgba = rgba.resize((width, height), Image.LANCZOS)

    arr = np.array(rgba)
    rgb = arr[:, :, :3]
    alpha = arr[:, :, 3]

    # Masque des pixels opaques
    opaque_mask = (alpha > 128).astype(np.uint8)

    # K-means clustering pour réduire les couleurs
    pixels = rgb[opaque_mask > 0].reshape(-1, 3).astype(np.float32)

    if len(pixels) == 0:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"></svg>'

    n_clusters = min(num_colors, len(pixels))
    kmeans = MiniBatchKMeans(n_clusters=n_clusters, batch_size=1000,
                             n_init=3, random_state=42)
    kmeans.fit(pixels)

    # Créer l'image quantifiée
    labels_full = np.zeros((height, width), dtype=np.int32) - 1
    labels_full[opaque_mask > 0] = kmeans.labels_
    centers = kmeans.cluster_centers_.astype(int)

    # Créer le SVG
    dwg = svgwrite.Drawing(size=(width, height))
    dwg.viewbox(0, 0, width, height)
    defs = dwg.defs
    gradient_id = 0

    # Pour chaque cluster de couleur, créer les régions
    for cluster_idx in range(n_clusters):
        # Masque de cette couleur
        cluster_mask = ((labels_full == cluster_idx) & (opaque_mask > 0)).astype(np.uint8) * 255

        # Nettoyer le masque (morphologie)
        kernel = np.ones((3, 3), np.uint8)
        cluster_mask = cv2.morphologyEx(cluster_mask, cv2.MORPH_CLOSE, kernel)
        cluster_mask = cv2.morphologyEx(cluster_mask, cv2.MORPH_OPEN, kernel)

        # Trouver les contours
        contours, hierarchy = cv2.findContours(
            cluster_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        for contour in contours:
            area = cv2.contourArea(contour)
            if area < min_area:
                continue

            # Bounding box
            bx, by, bw, bh = cv2.boundingRect(contour)

            # Créer un masque pour cette région spécifique
            region_mask = np.zeros((height, width), dtype=np.uint8)
            cv2.drawContours(region_mask, [contour], -1, 255, -1)

            # Détecter si c'est un dégradé
            gradient_info = detect_gradient(rgb, region_mask, (bx, by, bw, bh))
            if gradient_info is None:
                gradient_info = detect_radial_gradient(rgb, region_mask, (bx, by, bw, bh))

            # Convertir le contour en path SVG
            path_d = contour_to_svg_path(contour, detail)
            if not path_d:
                continue

            if gradient_info and gradient_info["type"] == "linear":
                # Créer un gradient linéaire SVG
                grad_id = f"grad_{gradient_id}"
                gradient_id += 1

                sc = gradient_info["start_color"]
                ec = gradient_info["end_color"]

                if gradient_info["horizontal"]:
                    lg = svgwrite.gradients.LinearGradient(
                        id=grad_id, x1="0%", y1="0%", x2="100%", y2="0%"
                    )
                else:
                    lg = svgwrite.gradients.LinearGradient(
                        id=grad_id, x1="0%", y1="0%", x2="0%", y2="100%"
                    )

                lg.add_stop_color(0, rgb_to_hex(*sc))
                lg.add_stop_color(1, rgb_to_hex(*ec))
                defs.add(lg)

                dwg.add(dwg.path(d=path_d, fill=f"url(#{grad_id})",
                                 stroke="none"))

            elif gradient_info and gradient_info["type"] == "radial":
                # Créer un gradient radial SVG
                grad_id = f"grad_{gradient_id}"
                gradient_id += 1

                cc = gradient_info["center_color"]
                ec = gradient_info["edge_color"]
                cx_pct = (gradient_info["center"][0] - bx) / max(1, bw) * 100
                cy_pct = (gradient_info["center"][1] - by) / max(1, bh) * 100

                rg = svgwrite.gradients.RadialGradient(
                    id=grad_id,
                    cx=f"{cx_pct}%", cy=f"{cy_pct}%", r="50%"
                )
                rg.add_stop_color(0, rgb_to_hex(*cc))
                rg.add_stop_color(1, rgb_to_hex(*ec))
                defs.add(rg)

                dwg.add(dwg.path(d=path_d, fill=f"url(#{grad_id})",
                                 stroke="none"))
            else:
                # Couleur unie
                color = centers[cluster_idx]
                hex_color = rgb_to_hex(*color)
                dwg.add(dwg.path(d=path_d, fill=hex_color, stroke="none"))

    return dwg.tostring()


def vectorize_bw(img: Image.Image, detail: float = 0.002,
                 min_area: int = 50) -> str:
    """Vectorise en noir et blanc."""
    gray = img.convert("L")
    width, height = gray.size

    max_dim = 2000
    if max(width, height) > max_dim:
        scale = max_dim / max(width, height)
        width = int(width * scale)
        height = int(height * scale)
        gray = gray.resize((width, height), Image.LANCZOS)

    arr = np.array(gray)

    # Binarisation adaptative
    binary = cv2.adaptiveThreshold(
        arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 11, 2
    )

    contours, _ = cv2.findContours(
        binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    dwg = svgwrite.Drawing(size=(width, height))
    dwg.viewbox(0, 0, width, height)

    for contour in contours:
        if cv2.contourArea(contour) < min_area:
            continue
        path_d = contour_to_svg_path(contour, detail)
        if path_d:
            dwg.add(dwg.path(d=path_d, fill="black", stroke="none"))

    return dwg.tostring()
