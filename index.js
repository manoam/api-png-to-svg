const express = require("express");
const multer = require("multer");
const sharp = require("sharp");
const potrace = require("potrace");
const ImageTracer = require("imagetracerjs");
const cors = require("cors");
const path = require("path");

const app = express();
const PORT = process.env.PORT || 3000;

app.use(cors());
app.use(express.json());

const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: 10 * 1024 * 1024 },
  fileFilter: (req, file, cb) => {
    const allowed = ["image/png", "image/jpeg", "image/webp", "image/bmp", "image/tiff"];
    if (allowed.includes(file.mimetype)) {
      cb(null, true);
    } else {
      cb(new Error("Format non supporté. Formats acceptés : PNG, JPEG, WebP, BMP, TIFF"));
    }
  },
});

const uploadBatch = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: 10 * 1024 * 1024, files: 50 },
  fileFilter: (req, file, cb) => {
    const allowed = ["image/png", "image/jpeg", "image/webp", "image/bmp", "image/tiff"];
    if (allowed.includes(file.mimetype)) {
      cb(null, true);
    } else {
      cb(null, false);
    }
  },
});

// --- Redimensionner si trop grand (pour la performance) ---
async function prepareForAnalysis(buffer) {
  const metadata = await sharp(buffer).metadata();
  const MAX_PIXELS = 500000; // 500k pixels max pour l'analyse
  const totalPixels = metadata.width * metadata.height;

  if (totalPixels > MAX_PIXELS) {
    const scale = Math.sqrt(MAX_PIXELS / totalPixels);
    const newWidth = Math.round(metadata.width * scale);
    return sharp(buffer).resize(newWidth).png().toBuffer();
  }
  return buffer;
}

// --- Analyse de complexité d'une image ---
async function analyzeComplexity(buffer) {
  const originalMeta = await sharp(buffer).metadata();
  const analysisBuffer = await prepareForAnalysis(buffer);
  const image = sharp(analysisBuffer);
  const metadata = await image.metadata();
  const { width, height, channels, hasAlpha } = metadata;
  const totalPixels = width * height;

  // Extraire les pixels bruts
  const raw = await image.raw().toBuffer();

  // Compter les couleurs uniques (échantillonnage si image trop grande)
  const step = totalPixels > 50000 ? Math.floor(totalPixels / 50000) : 1;
  const colorSet = new Set();
  const pixelSize = channels;

  for (let i = 0; i < raw.length; i += pixelSize * step) {
    // Ignorer les pixels transparents
    if (hasAlpha && raw[i + channels - 1] < 10) continue;

    // Quantifier les couleurs (grouper par blocs de 16 pour éviter les micro-variations)
    const r = Math.floor(raw[i] / 16);
    const g = channels >= 3 ? Math.floor(raw[i + 1] / 16) : r;
    const b = channels >= 3 ? Math.floor(raw[i + 2] / 16) : r;
    colorSet.add(`${r},${g},${b}`);
  }

  const uniqueColors = colorSet.size;

  // Détecter les dégradés (variation progressive entre pixels voisins)
  let gradientScore = 0;
  let gradientSamples = 0;
  const rowBytes = width * pixelSize;
  const sampleStep = Math.max(1, Math.floor(totalPixels / 20000));

  for (let y = 0; y < height - 1; y += sampleStep) {
    for (let x = 0; x < width - 1; x += sampleStep) {
      const idx = (y * width + x) * pixelSize;
      const idxRight = idx + pixelSize;
      const idxDown = idx + rowBytes;

      if (idxRight + pixelSize <= raw.length && idxDown + pixelSize <= raw.length) {
        // Différence avec le pixel à droite
        const diffH = Math.abs(raw[idx] - raw[idxRight]);
        // Différence avec le pixel en dessous
        const diffV = Math.abs(raw[idx] - raw[idxDown]);

        // Un dégradé = petite différence progressive (entre 1 et 15)
        if (diffH > 0 && diffH < 16) gradientScore++;
        if (diffV > 0 && diffV < 16) gradientScore++;
        gradientSamples += 2;
      }
    }
  }

  const gradientRatio = gradientSamples > 0 ? gradientScore / gradientSamples : 0;

  // Calculer le ratio de pixels transparents
  let transparentPixels = 0;
  if (hasAlpha) {
    for (let i = channels - 1; i < raw.length; i += pixelSize * step) {
      if (raw[i] < 10) transparentPixels++;
    }
  }
  const transparentRatio = hasAlpha ? transparentPixels / Math.ceil(totalPixels / step) : 0;

  // Score de fiabilité (0-100)
  let score = 100;

  // Pénalité couleurs (plus y'a de couleurs, moins le SVG sera fidèle)
  if (uniqueColors > 500) score -= 40;
  else if (uniqueColors > 200) score -= 25;
  else if (uniqueColors > 50) score -= 10;
  else if (uniqueColors > 10) score -= 5;

  // Pénalité dégradés
  if (gradientRatio > 0.5) score -= 30;
  else if (gradientRatio > 0.3) score -= 20;
  else if (gradientRatio > 0.15) score -= 10;

  // Bonus transparence (PNG avec fond transparent = souvent des icônes = bon pour SVG)
  if (transparentRatio > 0.3) score += 5;

  // Pénalité résolution très haute (trop de détails)
  if (totalPixels > 4000000) score -= 10;
  else if (totalPixels > 2000000) score -= 5;

  score = Math.max(0, Math.min(100, score));

  const convertible = score >= 30;
  let quality;
  if (score >= 85) quality = "excellent";
  else if (score >= 65) quality = "bon";
  else if (score >= 45) quality = "moyen";
  else if (score >= 30) quality = "faible";
  else quality = "non_convertissable";

  return {
    width: originalMeta.width,
    height: originalMeta.height,
    hasAlpha: !!hasAlpha,
    uniqueColors,
    gradientRatio: Math.round(gradientRatio * 100) / 100,
    transparentRatio: Math.round(transparentRatio * 100) / 100,
    score,
    quality,
    convertible,
  };
}

// --- Trace simple (N&B) ---
function traceBW(buffer, options = {}) {
  const TIMEOUT = 60000;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Conversion timeout (60s)")), TIMEOUT);
    potrace.trace(buffer, {
      turdSize: options.turdSize ?? 2,
      threshold: options.threshold ?? potrace.Potrace.THRESHOLD_AUTO,
      color: options.color || "black",
    }, (err, svg) => {
      clearTimeout(timer);
      if (err) return reject(err);
      resolve(svg);
    });
  });
}

// --- Conversion couleur : quantifier les couleurs dominantes puis tracer chacune ---
async function traceColor(buffer, options = {}) {
  const maxColors = options.steps || 4;
  const turdSize = options.turdSize ?? 2;

  // Lire les pixels bruts en RGBA
  const raw = await sharp(buffer).ensureAlpha().raw().toBuffer();
  const meta = await sharp(buffer).metadata();
  const width = meta.width;
  const height = meta.height;
  const channels = 4;

  // Quantifier : regrouper par blocs de 32, mais garder la vraie couleur moyenne
  const quantStep = 32;
  const colorMap = new Map();
  for (let i = 0; i < raw.length; i += channels) {
    if (raw[i + 3] < 128) continue;
    const qr = Math.round(raw[i] / quantStep) * quantStep;
    const qg = Math.round(raw[i + 1] / quantStep) * quantStep;
    const qb = Math.round(raw[i + 2] / quantStep) * quantStep;
    const key = `${qr},${qg},${qb}`;
    if (!colorMap.has(key)) colorMap.set(key, { qr, qg, qb, sumR: 0, sumG: 0, sumB: 0, count: 0 });
    const entry = colorMap.get(key);
    entry.sumR += raw[i];
    entry.sumG += raw[i + 1];
    entry.sumB += raw[i + 2];
    entry.count++;
  }

  // Calculer la couleur moyenne réelle de chaque groupe
  const groups = [...colorMap.values()]
    .sort((a, b) => b.count - a.count)
    .slice(0, Math.max(maxColors, 8));

  const colors = groups.map(g => ({
    qr: g.qr, qg: g.qg, qb: g.qb,
    r: Math.round(g.sumR / g.count),
    g: Math.round(g.sumG / g.count),
    b: Math.round(g.sumB / g.count),
    count: g.count,
  }));

  // Index rapide pour retrouver les couleurs par clé quantifiée
  const colorIndex = new Map();
  colors.forEach((c, i) => colorIndex.set(`${c.qr},${c.qg},${c.qb}`, i));

  // Assigner chaque pixel à sa couleur quantifiée
  const assigned = new Uint8Array(width * height);
  for (let i = 0; i < raw.length; i += channels) {
    const px = i / channels;
    if (raw[i + 3] < 128) { assigned[px] = 255; continue; }
    const qr = Math.round(raw[i] / quantStep) * quantStep;
    const qg = Math.round(raw[i + 1] / quantStep) * quantStep;
    const qb = Math.round(raw[i + 2] / quantStep) * quantStep;
    const idx = colorIndex.get(`${qr},${qg},${qb}`);
    assigned[px] = idx !== undefined ? idx : 255;
  }

  // Pour chaque couleur, créer un masque et tracer avec la vraie couleur moyenne
  const svgPaths = [];
  for (let ci = 0; ci < colors.length; ci++) {
    const color = colors[ci];
    const mask = Buffer.alloc(width * height);
    for (let p = 0; p < assigned.length; p++) {
      mask[p] = assigned[p] === ci ? 0 : 255;
    }

    const maskPng = await sharp(mask, { raw: { width, height, channels: 1 } }).png().toBuffer();
    const hex = `#${color.r.toString(16).padStart(2, "0")}${color.g.toString(16).padStart(2, "0")}${color.b.toString(16).padStart(2, "0")}`;

    const pathSvg = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("Trace timeout")), 30000);
      potrace.trace(maskPng, { turdSize, color: hex }, (err, svg) => {
        clearTimeout(timer);
        if (err) return reject(err);
        resolve(svg);
      });
    });

    const pathMatch = pathSvg.match(/<path[^>]*\/>/g);
    if (pathMatch) svgPaths.push(...pathMatch);
  }

  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" version="1.1">\n${svgPaths.map(p => `\t${p}`).join("\n")}\n</svg>`;
  return svg;
}

// --- Conversion exacte (vraies couleurs) avec imagetracerjs ---
async function traceExact(buffer, options = {}) {
  const numberOfColors = options.steps || 16;

  const raw = await sharp(buffer).ensureAlpha().raw().toBuffer();
  const meta = await sharp(buffer).metadata();

  const imgd = {
    width: meta.width,
    height: meta.height,
    data: new Uint8ClampedArray(raw),
  };

  const svg = ImageTracer.imagedataToSVG(imgd, {
    numberofcolors: numberOfColors,
    colorsampling: 2,
    strokewidth: 0,
    pathomit: 4,
    roundcoords: 2,
    ltres: 0.5,
    qtres: 0.5,
    scale: 1,
  });

  return svg;
}

// POST /analyze — analyse la complexité sans convertir
app.post("/analyze", upload.single("image"), async (req, res) => {
  try {
    if (!req.file) {
      return res.status(400).json({ error: "Aucune image fournie" });
    }

    const analysis = await analyzeComplexity(req.file.buffer);
    res.json({
      filename: req.file.originalname,
      ...analysis,
    });
  } catch (err) {
    res.status(500).json({ error: "Erreur analyse", details: err.message });
  }
});

// POST /convert — convertit une image en SVG avec analyse
app.post("/convert", upload.single("image"), async (req, res) => {
  try {
    if (!req.file) {
      return res.status(400).json({ error: "Aucune image fournie" });
    }

    // Analyse de complexité
    const analysis = await analyzeComplexity(req.file.buffer);

    // Lire les paramètres depuis le body (form-data) ou query string (fallback)
    const mode = req.body.mode || req.query.mode || "color";
    const format = req.body.format || req.query.format || "json";
    const force = (req.body.force || req.query.force) === "true";
    const turdSize = Number(req.body.turdSize || req.query.turdSize) || 2;
    const steps = Number(req.body.steps || req.query.steps) || Math.min(Math.max(2, Math.ceil(analysis.uniqueColors / 8)), 4);
    const threshold = Number(req.body.threshold || req.query.threshold) || undefined;
    const download = (req.body.download || req.query.download) === "true";
    // Si non convertissable et pas de forçage
    if (!analysis.convertible && !force) {
      return res.status(422).json({
        error: "Image trop complexe pour une conversion SVG fidèle",
        analysis,
        hint: "Ajoutez force=true dans le body pour forcer la conversion",
      });
    }

    // Préparer l'image (redimensionner pour la perf)
    const meta = await sharp(req.file.buffer).metadata();
    let sharpPipeline = sharp(req.file.buffer);
    const MAX_WIDTH = mode === "bw" ? 2000 : mode === "exact" ? 1000 : 800;
    if (meta.width > MAX_WIDTH) {
      sharpPipeline = sharpPipeline.resize(MAX_WIDTH);
    }
    const processedBuffer = await sharpPipeline.png().toBuffer();

    let svg;
    if (mode === "bw") {
      svg = await traceBW(processedBuffer, { turdSize, threshold });
    } else if (mode === "exact") {
      svg = await traceExact(processedBuffer, { steps: Number(req.body.steps || req.query.steps) || 16 });
    } else {
      svg = await traceColor(processedBuffer, { steps, turdSize });
    }
    // Retourner JSON avec SVG + analyse, ou juste le SVG
    if (format === "json") {
      return res.json({
        filename: req.file.originalname,
        analysis,
        svg,
        svgSize: Buffer.byteLength(svg, "utf8"),
      });
    }

    if (download) {
      const filename = path.parse(req.file.originalname).name + ".svg";
      res.setHeader("Content-Disposition", `attachment; filename="${filename}"`);
    }

    res.setHeader("X-Conversion-Score", analysis.score);
    res.setHeader("X-Conversion-Quality", analysis.quality);
    res.setHeader("X-Unique-Colors", analysis.uniqueColors);
    res.setHeader("Content-Type", "image/svg+xml");
    res.send(svg);
  } catch (err) {
    res.status(500).json({ error: "Erreur conversion", details: err.message });
  }
});

// POST /batch — convertir plusieurs images d'un coup
app.post("/batch", uploadBatch.array("images", 50), async (req, res) => {
  try {
    if (!req.files || req.files.length === 0) {
      return res.status(400).json({ error: "Aucune image fournie" });
    }

    const results = await Promise.all(
      req.files.map(async (file) => {
        try {
          const analysis = await analyzeComplexity(file.buffer);

          if (!analysis.convertible) {
            return {
              filename: file.originalname,
              status: "skipped",
              reason: "Image trop complexe",
              analysis,
            };
          }

          const fileMeta = await sharp(file.buffer).metadata();
          let pipeline = sharp(file.buffer);
          if (fileMeta.width > 800) pipeline = pipeline.resize(800);
          const processedBuffer = await pipeline.png().toBuffer();

          const steps = Math.min(Math.max(2, Math.ceil(analysis.uniqueColors / 8)), 4);
          const svg = await traceColor(processedBuffer, { steps, turdSize: 2 });

          return {
            filename: file.originalname,
            status: "converted",
            analysis,
            svg,
            svgSize: Buffer.byteLength(svg, "utf8"),
          };
        } catch (err) {
          return {
            filename: file.originalname,
            status: "error",
            error: err.message,
          };
        }
      })
    );

    const summary = {
      total: results.length,
      converted: results.filter((r) => r.status === "converted").length,
      skipped: results.filter((r) => r.status === "skipped").length,
      errors: results.filter((r) => r.status === "error").length,
    };

    res.json({ summary, results });
  } catch (err) {
    res.status(500).json({ error: "Erreur batch", details: err.message });
  }
});

// GET / — documentation
app.get("/", (req, res) => {
  res.json({
    name: "PNG to SVG API",
    version: "2.0.0",
    endpoints: {
      "POST /convert": {
        description: "Convertit une image en SVG avec analyse de fiabilité",
        body: {
          type: "multipart/form-data",
          fields: {
            image: "(fichier) Image à convertir — requis",
            mode: "'color' (défaut), 'exact' (vraies couleurs), ou 'bw' (noir et blanc)",
            format: "'json' (défaut) pour SVG + analyse, ou 'svg' pour le SVG brut",
            threshold: "Seuil de détection 0-255 (défaut: auto, uniquement en mode bw)",
            turdSize: "Suppression des petits artefacts (défaut: 2, modes color/bw)",
            steps: "Nombre de couleurs: 2-8 (color), 2-64 (exact, défaut: 16)",
            force: "'true' pour forcer la conversion même si image trop complexe",
            download: "'true' pour télécharger le fichier SVG",
          },
        },
      },
      "POST /analyze": {
        description: "Analyse la complexité d'une image sans la convertir",
        body: "multipart/form-data avec champ 'image' (fichier)",
        response: {
          score: "Score de fiabilité (0-100)",
          quality: "excellent | bon | moyen | faible | non_convertissable",
          convertible: "true/false",
          uniqueColors: "Nombre de couleurs uniques détectées",
          gradientRatio: "Ratio de dégradés (0-1)",
          transparentRatio: "Ratio de transparence (0-1)",
        },
      },
      "POST /batch": {
        description: "Convertit plusieurs images (max 50) avec analyse automatique",
        body: "multipart/form-data avec champ 'images' (fichiers multiples)",
        response: "JSON avec summary + résultats par image (couleur par défaut)",
      },
    },
  });
});

app.listen(PORT, () => {
  console.log(`PNG to SVG API v2 démarrée sur http://localhost:${PORT}`);
});
