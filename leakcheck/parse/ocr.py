"""OCR：RapidOCR（ONNX 后端）包装。

用 onnxruntime 跑 PP-OCR 的检测 + 识别模型，不依赖 paddle，体积小、可打包。

对外三个函数：

    available()          → 当前环境能不能 OCR
    ocr_blocks(source)   → 图片（路径/字节/数组）→ Block 列表
    ocr_text(source)     → 只要纯文本

OCR 出来的块 ``source="ocr"``、带 ``confidence``，检测端会按"降级"处理。
"""

from __future__ import annotations

from pathlib import Path

from .blocks import Block

#: 低于这个置信度的识别结果直接丢掉（噪点太多）
MIN_CONF = 0.40

#: 太小的图不 OCR（图标、装饰线）
MIN_PIXELS = 40

_engine = None
_engine_error: str | None = None


def available() -> bool:
    """能否使用 OCR。"""
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:                              # noqa: BLE001
        return False


def _get_engine():
    global _engine, _engine_error
    if _engine is not None:
        return _engine
    if _engine_error is not None:
        return None
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception as exc:                       # noqa: BLE001
        _engine_error = f"没有 OCR 引擎：{exc}"
        return None
    try:
        _engine = RapidOCR()
    except Exception as exc:                       # noqa: BLE001
        _engine_error = f"OCR 引擎初始化失败：{exc}"
        return None
    return _engine


def engine_error() -> str | None:
    return _engine_error


#: 长边小于这个值就放大（小图里的字识别率很低）
MIN_LONG_SIDE = 720


def _upscale_if_small(source):
    """数组输入且偏小时放大到可识别尺寸。"""
    try:
        import numpy as np
        from PIL import Image
    except Exception:                              # noqa: BLE001
        return source
    if not isinstance(source, np.ndarray):
        return source
    height, width = source.shape[:2]
    long_side = max(height, width)
    if long_side >= MIN_LONG_SIDE:
        return source
    scale = min(3.0, MIN_LONG_SIDE / max(long_side, 1))
    new_size = (int(width * scale), int(height * scale))
    image = Image.fromarray(source).resize(new_size, Image.LANCZOS)
    return np.array(image)


def _as_input(source):
    """RapidOCR 接受 路径 / bytes / ndarray；这里统一成它认识的形式。"""
    if isinstance(source, (str, Path)):
        return str(source)
    return _upscale_if_small(source)


def _run(source) -> list[tuple[str, float, tuple[float, float, float, float]]]:
    engine = _get_engine()
    if engine is None:
        return []
    try:
        result, _ = engine(_as_input(source))
    except Exception:                              # noqa: BLE001
        return []
    if not result:
        return []

    out = []
    for item in result:
        try:
            box, text, score = item[0], item[1], float(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        text = (text or "").strip()
        if not text or score < MIN_CONF:
            continue
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        out.append((text, score, (min(xs), min(ys), max(xs), max(ys))))

    # 按阅读顺序（先 y 后 x）排
    out.sort(key=lambda t: (round(t[2][1] / 8), t[2][0]))
    return out


def ocr_blocks(source, page: int = 1, vdoc_base: int = 0,
               kind: str = "image_text") -> list[Block]:
    """图片 → Block 列表。识别不出东西时返回空列表。"""
    rows = _run(source)
    blocks: list[Block] = []
    for text, score, bbox in rows:
        blocks.append(Block(
            text=text,
            kind=kind,
            page=page,
            bbox=bbox,
            vdoc=vdoc_base + len(blocks),
            source="ocr",
            confidence=round(score, 3),
        ))
    return blocks


def ocr_text(source) -> str:
    return "\n".join(text for text, _score, _box in _run(source))
