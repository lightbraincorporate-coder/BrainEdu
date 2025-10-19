from __future__ import annotations

import io
from typing import Optional

import numpy as np
import pytesseract
from PIL import Image


def ocr_image_bytes(image_bytes: bytes, lang: str = "eng+fra") -> str:
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    # Pré-traitement simple: conversion en niveaux de gris, seuil adaptatif via numpy/Pillow
    gray = image.convert("L")
    np_img = np.array(gray)
    # Normalisation simple
    np_img = (np_img - np_img.min()) / max(1, (np_img.max() - np_img.min())) * 255
    np_img = np_img.astype(np.uint8)
    processed = Image.fromarray(np_img)
    text: str = pytesseract.image_to_string(processed, lang=lang)
    return text.strip()
