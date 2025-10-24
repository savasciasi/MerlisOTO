"""Template matching helpers for PM detection."""
from __future__ import annotations

import cv2
import numpy as np
from typing import List, Tuple

Box = Tuple[int, int, int, int, float]


def match_template(gray_img: np.ndarray, gray_tmpl: np.ndarray, thr: float = 0.8) -> List[Box]:
    """Return NMS filtered template matches."""
    if gray_img is None or gray_tmpl is None:
        return []
    res = cv2.matchTemplate(gray_img, gray_tmpl, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(res >= thr)
    h, w = gray_tmpl.shape[:2]
    boxes: List[Box] = [(int(x), int(y), int(x + w), int(y + h), float(res[y, x])) for x, y in zip(xs, ys)]
    return nms(boxes, iou_thr=0.3)


def nms(boxes: List[Box], iou_thr: float = 0.3) -> List[Box]:
    """Basic non-maximum suppression for template boxes."""
    if not boxes:
        return []
    boxes = sorted(boxes, key=lambda b: b[4], reverse=True)
    keep: List[Box] = []

    def iou(a: Box, b: Box) -> float:
        ax1, ay1, ax2, ay2, _ = a
        bx1, by1, bx2, by2, _ = b
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
        inter = iw * ih
        if inter <= 0:
            return 0.0
        area_a = (ax2 - ax1) * (ay2 - ay1)
        area_b = (bx2 - bx1) * (by2 - by1)
        union = area_a + area_b - inter + 1e-6
        return inter / union

    while boxes:
        current = boxes.pop(0)
        keep.append(current)
        boxes = [b for b in boxes if iou(current, b) < iou_thr]
    return keep
