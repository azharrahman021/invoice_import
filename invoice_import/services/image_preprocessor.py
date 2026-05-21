from __future__ import annotations

import tempfile
from pathlib import Path


def preprocess_image(input_path: str) -> str:
    """Return an enhanced temporary image path for OCR."""
    try:
        import cv2
        import numpy as np
        from PIL import Image, ImageOps
        try:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        except Exception:
            pass
    except Exception:
        return input_path

    with Image.open(input_path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        source = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)

    gray = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
    denoised = cv2.fastNlMeansDenoising(gray, h=12)
    enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(denoised)
    deskewed = _deskew(enhanced)
    thresholded = cv2.adaptiveThreshold(
        deskewed, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11
    )

    output_path = Path(tempfile.gettempdir()) / f"invoice_import_{Path(input_path).stem}.png"
    cv2.imwrite(str(output_path), thresholded)
    return str(output_path)


def _deskew(image):
    try:
        import cv2
        import numpy as np
    except Exception:
        return image

    coords = np.column_stack(np.where(image < 255))
    if coords.size == 0:
        return image
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
    if abs(angle) < 0.5 or abs(angle) > 20:
        return image
    h, w = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
    return cv2.warpAffine(image, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
