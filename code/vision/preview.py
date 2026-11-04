"""调参预览窗口（只在本机显示，不保存任何画面）。

画面上叠加：骨架、床区、躯干角度 / 髋部下降 / 规则状态。按 q 或 Esc 退出。
没有画面的输入（骨架回放、合成场景）画在黑底上。OpenCV 的 putText 不支持中文，所以窗口里用英文。
"""
import cv2
import numpy as np

from vision.skeleton import BONES
COLORS = {"WATCH": (113, 204, 46), "CANDIDATE": (36, 191, 251), "FALLEN": (113, 113, 248)}  # BGR
WINDOW = "CareMate vision preview (q to quit)"


class Preview:
    def __init__(self, rules, size=(640, 480)):
        self.rules = rules
        self.size = size

    def show(self, frame, kp, detector):
        img = self.render(frame, kp, detector)
        cv2.imshow(WINDOW, img)
        return (cv2.waitKey(1) & 0xFF) not in (ord("q"), 27)

    def render(self, frame, kp, detector, *, diagnostics=True):
        """Render in memory; the headless web monitor never calls imshow/waitKey."""
        img = frame.copy() if frame is not None else np.zeros((self.size[1], self.size[0], 3), np.uint8)
        h, w = img.shape[:2]
        color = COLORS.get(detector.state, (200, 200, 200))
        for z in detector.rules["bed_zones"]:
            cv2.rectangle(img, (int(z[0] * w), int(z[1] * h)), (int(z[2] * w), int(z[3] * h)), (200, 160, 60), 2)
            if diagnostics:
                cv2.putText(img, "bed", (int(z[0] * w) + 4, int(z[1] * h) + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 160, 60), 1)
        if kp:
            pts = {k: (int(p[0] * w), int(p[1] * h)) for k, p in kp.items()
                   if p is not None and (len(p) < 3 or p[2] >= detector.rules["min_visibility"])}
            if "neck" not in pts and "l_shoulder" in pts and "r_shoulder" in pts:
                a, b = pts["l_shoulder"], pts["r_shoulder"]
                pts["neck"] = ((a[0] + b[0]) // 2, (a[1] + b[1]) // 2)
            for a, b in BONES:
                if a in pts and b in pts:
                    cv2.line(img, pts[a], pts[b], color, 4, cv2.LINE_AA)
            for p in pts.values():
                cv2.circle(img, p, 4, (255, 255, 255), -1, cv2.LINE_AA)

        if not diagnostics:
            return img
        last = detector.last or {}
        f = last.get("f")
        lines = ["state: %s  input: %s" % (detector.state, getattr(detector, "input_status", "observing"))]
        if f:
            drop = ""
            if detector.standing_hip is not None and detector.standing_len:
                drop = "  hip drop: %.2f L" % ((f["hip_y"] - detector.standing_hip) / detector.standing_len)
            lines.append("torso: %4.0f deg (upright<=%g lying>=%g)%s" % (
                f["angle"], self.rules.get("upright_angle", 35), self.rules.get("lying_angle", 55), drop))
            flags = [n for n in ("upright", "lying", "low", "in_bed") if last.get(n)]
            lines.append("flags: " + (" ".join(flags) or "-"))
        else:
            lines.append("pose unavailable (check shoulders/hips)" if kp else "no person")
        cv2.rectangle(img, (0, 0), (w, 22 * len(lines) + 10), (0, 0, 0), -1)
        for i, text in enumerate(lines):
            cv2.putText(img, text, (10, 24 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color if i == 0 else (230, 230, 230), 1, cv2.LINE_AA)
        return img

    def close(self):
        cv2.destroyAllWindows()
