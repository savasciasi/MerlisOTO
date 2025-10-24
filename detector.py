from typing import Dict, Tuple, Iterable
import cv2

# pm sınıf adlarını esnek tanı
VALID_PM_NAMES = {"pm-box", "pm_box"}

class YoloDetector:
    """
    Roboflow Hosted Inference wrapper.
    predict_scaled: görüntüyü küçült, tahmin yap, bbox'ları geri ölçekle.
    """
    def __init__(self, api_key: str, workspace: str, project: str, version: int):
        from roboflow import Roboflow
        rf = Roboflow(api_key=api_key)
        self.model = rf.workspace(workspace).project(project).version(version).model

    def predict_scaled(self, bgr_image, confidence: int, overlap: int, max_w: int = 1024):
        h, w = bgr_image.shape[:2]
        scale = 1.0
        if w > max_w:
            scale = max_w / float(w)
            new_w = max_w
            new_h = int(h * scale)
            small = cv2.resize(bgr_image, (new_w, new_h), interpolation=cv2.INTER_AREA)
        else:
            small = bgr_image.copy()

        preds = self.model.predict(small, confidence=confidence, overlap=overlap).json()
        if scale != 1.0:
            for p in preds.get("predictions", []):
                p["x"]      = p["x"]      / scale
                p["y"]      = p["y"]      / scale
                p["width"]  = p["width"]  / scale
                p["height"] = p["height"] / scale
        return preds

    @staticmethod
    def draw_predictions(bgr, preds: Dict, classes: Iterable[str] = ("player", "pm-box", "pm_box")):
        for p in preds.get("predictions", []):
            cls = p.get("class")
            if cls not in classes:
                continue
            x, y, w, h = p["x"], p["y"], p["width"], p["height"]
            x1, y1 = int(x - w/2), int(y - h/2)
            x2, y2 = int(x + w/2), int(y + h/2)
            color = (0, 255, 0) if cls == "player" else (60, 220, 255)
            conf = p.get("confidence", 0.0)
            cv2.rectangle(bgr, (x1, y1), (x2, y2), color, 2)
            cv2.putText(bgr, f"{cls} {conf:.2f}", (x1, max(0, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)
        return bgr

    @staticmethod
    def bbox_xyxy_from_pred(p) -> Tuple[int, int, int, int]:
        x, y, w, h = p["x"], p["y"], p["width"], p["height"]
        x1, y1 = int(x - w/2), int(y - h/2)
        x2, y2 = int(x + w/2), int(y + h/2)
        return x1, y1, x2, y2

    @staticmethod
    def is_pm(cls_name: str) -> bool:
        return cls_name in VALID_PM_NAMES
