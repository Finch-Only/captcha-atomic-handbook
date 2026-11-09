#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
captcha_solver_v2.py —— 验证码求解原子能力 V2.0 (API 级)

与 V1 的区别
------------
V1 只拿一张组件截图瞎猜；V2 直接吃 tianai genCaptcha 返回的原图数据:
  backgroundImage  400x240 原始背景(含黑圆洞/深色缺口/错位条)
  templateImage    拼图块(带alpha) / 旋转圆内容 / 提示字条
再结合从前端 tac.min.js 逆向出的精确协议:
  - 拖动上限 end = 242px (SLIDER/ROTATE/CONCAT 硬编码)
  - 旋转角度 = 拖动距离 / 242 * 360 度 (顺时针, CSS rotate 正方向)
  - SLIDER: 拼图块初始 left=0, 拖动 t 块平移 t px
  - CONCAT: 整条横带 background-position-x = t (整条随拖动平移)
  - WORD_IMAGE_CLICK: 依次点击, 校验点击点与词位置的容差

支持四类
--------
  ROTATE            圆内内容转正 → 计算纠正角 → 距离 = 角度*242/360
  SLIDER            拼图块归位 → 带掩码模板匹配缺口 → 距离 = 缺口x - 块起始x
  CONCAT            横带对齐 → 模板匹配水平偏移 → 距离 = 偏移量
  WORD_IMAGE_CLICK  认字+定位 → 依次点击坐标序列

识别引擎: PP-OCRv4 server 识别模型 (90MB, 本地 ONNX 推理) + ddddocr 检测/分类
全部本地推理, 不联网。

用法
----
  python3 captcha_solver_v2.py --dir <api_types目录>     # 解已抓取的 JSON+图片
  python3 captcha_solver_v2.py --bg <bg.png> --tpl <tpl.png> --type ROTATE
  python3 captcha_solver_v2.py --selftest
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime
from functools import lru_cache
from pathlib import Path

VERSION = "2.0"
END = 242                      # tac.min.js 硬编码拖动上限
BG_W, BG_H = 400, 240          # 服务器返回的背景图尺寸(常见值, 实际以图片为准)
DISP_W, DISP_H = 300, 180      # 组件图片显示尺寸 (CHECK payload 实测 bgImageWidth=300)
VENDOR = os.path.expanduser("~/.workbuddy/captcha_solver_vendor")
OCR_VENDOR = os.path.expanduser("~/.workbuddy/captcha_ocr_vendor")
V4_MODEL = "/tmp/ch_v4_rec_server.onnx"
V4_KEYS = "/tmp/ppocr_keys_v1.txt"

import cv2
import numpy as np


def disp_map(bg):
    """原始背景图 -> 组件显示区(300x180). 前端 <img> 为 object-fit:fill ——
    各向异性拉伸 (2026-11-05 DOM 实测): 814x419 大图 x/y 比例不同 (0.369/0.430),
    标准图 400x240 两比例恰好同为 0.75 所以此前未暴露.
    返回 (sx, sy): disp_x = raw_x * sx, disp_y = raw_y * sy"""
    H, W = bg.shape[:2]
    return DISP_W / float(W), DISP_H / float(H)


# ----------------------------------------------------------------------
# 引擎加载
# ----------------------------------------------------------------------

_ENG = {"rec": None, "cls": None, "det": None}


def load_engines():
    if _ENG["det"] is not None:
        return _ENG
    import ddddocr
    _ENG["det"] = ddddocr.DdddOcr(det=True, show_ad=False)
    _ENG["cls"] = ddddocr.DdddOcr(show_ad=False)
    try:
        from rapidocr_onnxruntime.ch_ppocr_v3_rec.text_recognize import TextRecognizer
        cfg = {"model_path": V4_MODEL, "keys_path": V4_KEYS,
               "rec_img_shape": [3, 48, 320], "rec_batch_num": 6, "use_cuda": False}
        _ENG["rec"] = TextRecognizer(cfg)
    except Exception:
        _ENG["rec"] = None
    return _ENG


# ----------------------------------------------------------------------
# 结果结构
# ----------------------------------------------------------------------

def _jsonable(o):
    """递归把 numpy 标量/数组转成原生类型, 保证 JSON 可序列化"""
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return _jsonable(o.tolist())
    return o


def result(kind, action, confidence, need_human, reason, **extra):
    d = {
        "能力版本": VERSION,
        "时间": datetime.now().isoformat(timespec="seconds"),
        "验证类型": kind,
        "动作": action,
        "置信度": round(float(confidence), 3),
        "需人工处理": bool(need_human),
        "判定理由": reason,
    }
    d.update(_jsonable(extra))
    if action is not None:
        d["动作"] = _jsonable(action)
    return d


# ----------------------------------------------------------------------
# ROTATE: 圆内内容转正
# ----------------------------------------------------------------------

def find_black_disk(bg):
    """背景上被挖黑的圆洞: 多阈值 + 圆形度约束的最大连通域.
    夜景/暗色背景下, 宽阈值会把黑盘与黑暗天空粘连 → 依次用严阈值分离正圆."""
    g = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
    best, bi = None, -1
    for th in (5, 8, 12, 18):
        m = (g < th).astype(np.uint8) * 255
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        n, lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
        for i in range(1, n):
            x, y, w, h, a = stats[i]
            if a < 2000:
                continue
            circ = a / (np.pi * (w / 2.0) * (h / 2.0) + 1e-6)   # 圆形度: 接近1=正圆
            ar = w / float(h)
            if circ < 0.80 or not (0.85 < ar < 1.18):
                continue
            if best is None or a > best[4]:
                best, bi = stats[i], i
        if best is not None:
            break   # 严阈值已找到合格正圆, 不再放宽
    if best is None:
        return None
    x, y, w, h, a = best
    return {"cx": x + w / 2, "cy": y + h / 2, "r": (w + h) / 4, "bbox": (x, y, w, h)}


def tpl_circle(tpl):
    """模板图的圆形内容范围(alpha)"""
    if tpl.shape[2] == 4:
        a = tpl[:, :, 3]
        ys, xs = np.where(a > 10)
        if len(ys) == 0:
            return None
        x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
        return {"cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2, "r": (x1 - x0 + y1 - y0) / 4,
                "bbox": (x0, y0, x1 - x0, y1 - y0)}
    return None


def solve_rotate(bg, tpl):
    """
    边界连续性法 (物理精确): 圆内内容 = 黑盘区域原内容旋转 G° (CCW) 的版本,
    源区被黑盘遮挡 → 无法用背景模板匹配。但内容转回 G° (CW) 后必然与盘周边
    照片无缝衔接 ("整个图片规整")。
      score(θ) = 圆内容逆时针转 θ 后, 盘内侧环与盘外侧环沿径向的颜色连续性
      峰值 θ* = (360 - G) mod 360 → 拖动 A = G = (360 - θ*) mod 360
    """
    if tpl.shape[2] != 4:
        return result("ROTATE", None, 0, True, "模板无 alpha, 转人工")
    disk = find_black_disk(bg)
    circ = tpl_circle(tpl)
    if disk is None or circ is None:
        return result("ROTATE", None, 0, True, "未找到黑盘/圆模板, 转人工")
    dcy, dcx, Rd = disk["cy"], disk["cx"], disk["r"]
    tcy, tcx = circ["cy"], circ["cx"]
    Rt = min(circ["bbox"][2], circ["bbox"][3]) / 2.0
    if Rt < 20 or Rd < 20:
        return result("ROTATE", None, 0, True, "盘/圆尺寸异常, 转人工")
    s = Rd / Rt
    H, W = bg.shape[:2]
    yy, xx = np.mgrid[0:H, 0:W]
    rr = np.sqrt((yy - dcy) ** 2 + (xx - dcx) ** 2)
    ang = np.arctan2(yy - dcy, xx - dcx)
    tpl_alpha = (tpl[:, :, 3] > 128).astype(np.uint8) * 255
    tpl_bgr = tpl[:, :, :3]

    def score_at(th):
        """θ: 圆内容逆时针旋转度数(回贴到盘位), 返回径向连续性(负色差, 越大越好)"""
        M = cv2.getRotationMatrix2D((tcx, tcy), float(th), s)
        M[0, 2] += dcx - tcx
        M[1, 2] += dcy - tcy
        warped = cv2.warpAffine(tpl_bgr, M, (W, H))
        wmask = cv2.warpAffine(tpl_alpha, M, (W, H)) > 127
        inner = (rr >= Rd - 10) & (rr <= Rd - 3) & wmask
        if inner.sum() < 100:
            return -999.0
        px = np.round(dcx + (Rd + 6.5) * np.cos(ang[inner])).astype(int)
        py = np.round(dcy + (Rd + 6.5) * np.sin(ang[inner])).astype(int)
        ok = (px >= 0) & (px < W) & (py >= 0) & (py < H)
        pi_, pj_ = np.where(inner)
        a1 = warped[pi_[ok], pj_[ok]].astype(np.float32)
        a2 = bg[py[ok], px[ok]].astype(np.float32)
        return -float(np.abs(a1 - a2).mean())

    scores = np.array([score_at(th) for th in range(360)])
    valid = scores > -900
    if valid.sum() < 180:
        return result("ROTATE", None, 0, True, "盘区有效角度不足, 转人工")
    th_coarse = int(np.argmax(scores))
    # ±3° 0.25° 细化
    best = (scores[th_coarse], float(th_coarse))
    for th in np.arange(th_coarse - 3.0, th_coarse + 3.01, 0.25):
        v = score_at(float(th % 360.0))
        if v > best[0]:
            best = (v, float(th % 360.0))
    v_peak, th_star = best
    gap = v_peak - float(np.median(scores[valid]))
    drag = (360.0 - th_star) % 360.0
    dist = drag / 360.0 * END
    conf = max(0.0, min(1.0, (gap - 8.0) / 22.0))
    act = {"类型": "拖动", "拖动距离": round(dist, 1), "纠正角度": round(drag, 1),
           "轨道上限": END}
    return result("ROTATE", act, conf, gap < 12.0,
                  "边界连续性: 圆内容逆时针%.2f°与盘周照片衔接 (峰-中位差%.1f) → 拖动CW %.1f°"
                  % (th_star, gap, drag),
                  对齐角=round(th_star, 2), 峰中位差=round(gap, 1))


# ----------------------------------------------------------------------
# SLIDER: 拼图块归位
# ----------------------------------------------------------------------

def _zmap(a):
    med = float(np.median(a))
    std = float(np.std(a)) + 1e-6
    return (a - med) / std


def solve_slider(bg, tpl):
    """拼图块归位: 全程在显示坐标系 (300x180, object-fit:fill 拉伸) 匹配.
    依据: 服务器校验的是前端显示行为, 显示系即 ground truth;
    814x419 大图在原始系匹配会被岩石/海浪纹理干扰 (cc~0.17), 缩到显示系后
    细节干扰被抹掉, 边界环特征突出 (cc~0.38). 标准图 400x240 两坐标系结果一致.
    主信号 = 掩膜边界环 × 背景 Canny 边缘 (TM_CCOEFF_NORMED),
    辅助 = |暗度对比| 校验洞区确实偏离周边亮度 (洞可能被压暗或提亮)."""
    if tpl.shape[2] != 4:
        return result("SLIDER", None, 0, True, "拼图块模板无 alpha, 转人工")
    mask = (tpl[:, :, 3] > 128).astype(np.uint8)
    ys, xs = np.where(mask > 0)
    x0, y0, x1, y1 = xs.min(), ys.min(), xs.max(), ys.max()
    mcrop = mask[y0:y1 + 1, x0:x1 + 1]
    if mcrop.shape[0] > bg.shape[0] or mcrop.shape[1] > bg.shape[1]:
        return result("SLIDER", None, 0, True, "拼图块大于背景, 尺寸异常")

    sx, sy = disp_map(bg)          # fill 各向异性拉伸 (bg → 300x180)
    # 2x 显示系匹配: 精度翻倍 (~0.5px)
    S2 = 2
    bgd = cv2.resize(bg, (DISP_W * S2, DISP_H * S2), interpolation=cv2.INTER_AREA)
    # 定位用: 洞在 bg fill 拉伸后的形状 → mask 按 sx 缩放 (与洞边界环同形)
    mw = max(2, int(round(mcrop.shape[1] * sx * S2)))
    mh = max(2, int(round(mcrop.shape[0] * sy * S2)))
    md = cv2.resize(mcrop, (mw, mh), interpolation=cv2.INTER_AREA)

    # 主: 边界环模板 vs 2x 显示系 Canny
    er = cv2.erode(md, np.ones((5, 5), np.uint8))
    ring = np.clip(md.astype(int) - er.astype(int), 0, 3).astype(np.uint8) * 255
    pe = cv2.GaussianBlur(ring.astype(np.float32), (5, 5), 0)
    g = cv2.cvtColor(bgd, cv2.COLOR_BGR2GRAY)
    ge = cv2.Canny(g, 40, 140).astype(np.float32)
    if pe.shape[0] > ge.shape[0] or pe.shape[1] > ge.shape[1]:
        return result("SLIDER", None, 0, True, "拼图块大于显示区, 尺寸异常")
    res = cv2.matchTemplate(ge, pe, cv2.TM_CCOEFF_NORMED)
    _, cc, _, loc = cv2.minMaxLoc(res)
    hx, hy = loc
    # 抛物线亚像素插值 (x 方向)
    sub = 0.0
    if 1 <= hx < res.shape[1] - 1:
        denom = res[hy, hx - 1] - 2 * res[hy, hx] + res[hy, hx + 1]
        if abs(denom) > 1e-9:
            sub = float(0.5 * (res[hy, hx - 1] - res[hy, hx + 1]) / denom)
            sub = max(-1.0, min(1.0, sub))
    hxd = (hx + sub) / float(S2)   # 显示系洞左缘 x
    hyd = hy / float(S2)
    r2 = res.copy()
    cv2.rectangle(r2, (max(0, hx - 12 * S2), max(0, hy - 12 * S2)),
                  (min(r2.shape[1] - 1, hx + 12 * S2), min(r2.shape[0] - 1, hy + 12 * S2)), -1)
    _, cc2, _, _ = cv2.minMaxLoc(r2)

    # 辅: |暗度对比| 在峰处应偏高 (显示系)
    g1 = cv2.cvtColor(cv2.resize(bg, (DISP_W, DISP_H), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
    g32 = g1.astype(np.float32)
    md1 = cv2.resize(mcrop, (max(2, int(round(mcrop.shape[1] * sy))), max(2, int(round(mcrop.shape[0] * sy)))),
                     interpolation=cv2.INTER_AREA).astype(np.float32)
    inner = cv2.matchTemplate(g32, md1, cv2.TM_CCORR)
    ones = np.ones_like(md1)
    out_mean = (cv2.matchTemplate(g32, ones, cv2.TM_CCORR) - inner) / max(1e-6, float(ones.sum() - md1.sum()))
    in_mean = inner / max(1e-6, float(md1.sum()))
    dev = _zmap(np.abs(out_mean - in_mean))[int(round(hyd)), int(round(hxd))]

    # Full-height pieces preserve the raw vertical position. Restrict the
    # fallback to that row so unrelated high-contrast scenery cannot win.
    row_mismatch = tpl.shape[0] == bg.shape[0] and abs(hyd - y0 * sy) > 5
    if tpl.shape[0] == bg.shape[0] and (cc < 0.15 or dev < 0.3 or row_mismatch):
        raw = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
        kernel = np.ones((5, 5), np.uint8)
        boundary = cv2.dilate(mcrop, kernel) - cv2.erode(mcrop, kernel)
        raw_edges = cv2.Canny(raw, 18, 72)
        scores = cv2.matchTemplate(raw_edges, boundary, cv2.TM_CCORR_NORMED)
        start = max(0, y0 - 4)
        band = scores[start:y0 + 5]
        if band.size:
            peak_y, peak_x = np.unravel_index(band.argmax(), band.shape)
            value = float(band[peak_y, peak_x])
            rivals = band[:, np.abs(np.arange(band.shape[1]) - peak_x) > 12]
            runner_up = float(rivals.max()) if rivals.size else 0.0
            dist = peak_x * sx - x0 * sy
            if value >= 0.25 and value - runner_up >= 0.04 and 0 <= dist <= END:
                act = {"类型": "拖动", "拖动距离": round(dist, 1), "轨道上限": END}
                return result("SLIDER", act, 0.78, False,
                              "固定高度边界匹配: 原图洞位(%d,%d)，主峰 %.3f，次峰 %.3f" %
                              (peak_x, start + peak_y, value, runner_up))

    if tpl.shape[0] == bg.shape[0] and (cc < 0.15 or dev < 0.3 or row_mismatch):
        height, width = bg.shape[:2]
        difference = background_difference(bg, [(0, y0, width, y1 + 1)])
        if difference is not None:
            row = max(0, y0 - 5)
            band = difference[row:min(height, y1 + 7)].astype(np.float32)
            foreground = np.clip((band - 18) / 32, 0, 1)
            scores = cv2.matchTemplate(foreground, mcrop.astype(np.float32), cv2.TM_CCOEFF_NORMED)
            peak_y, peak_x = np.unravel_index(scores.argmax(), scores.shape)
            value = float(scores[peak_y, peak_x])
            rivals = scores[:, np.abs(np.arange(scores.shape[1]) - peak_x) > 12]
            runner_up = float(rivals.max()) if rivals.size else 0.0
            dist = peak_x * sx - x0 * sy
            if value >= 0.52 and value - runner_up >= 0.16 and 0 <= dist <= END:
                act = {"类型": "拖动", "拖动距离": round(dist, 1), "轨道上限": END}
                return result("SLIDER", act, 0.76, False,
                              "固定高度同底图差分: 洞位(%d,%d)，主峰 %.3f，次峰 %.3f" %
                              (peak_x, row + peak_y, value, runner_up))

    conf = max(0.0, min(1.0, (cc - 0.10) / 0.35)) * max(0.4, min(1.0, dev / 2.0))
    # dist = 洞显示x - 块可见部初始显示x (块图层按 sy 缩放: x0*sy)
    dist = hxd - x0 * sy
    act = {"类型": "拖动", "拖动距离": round(max(0.0, dist), 1), "轨道上限": END}
    return result("SLIDER", act, conf, cc < 0.15 or dev < 0.3 or row_mismatch,
                  "2x显示系边界环匹配: 洞显示位(%.1f,%.1f) cc=%.3f 次峰=%.3f 对比z=%.2f"
                  % (hxd, hyd, cc, cc2, dev),
                  洞显示=[round(hxd, 1), round(hyd, 1)], 边缘分=round(cc, 3), 次峰=round(cc2, 3), 对比z=round(float(dev), 2),
                  块内容框=[int(x0), int(y0), int(x1 - x0 + 1), int(y1 - y0 + 1)])


# ----------------------------------------------------------------------
# WORD_IMAGE_CLICK: 依次点字
# ----------------------------------------------------------------------

def _tip_words(tpl):
    """从提示条 alpha 读出目标字序列 (顺序=点击顺序)"""
    eng = load_engines()
    if tpl.shape[2] != 4:
        return [], "提示条无 alpha"
    alpha = tpl[:, :, 3]
    line = cv2.morphologyEx(alpha, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (90, 1)))
    m2 = np.clip(alpha.astype(int) - (line > 0) * 255, 0, 255).astype(np.uint8)
    W = m2.shape[1]
    n = max(2, round(W / 71))
    words = []
    for k in range(n):
        sub = m2[:, int(k * W / n):int((k + 1) * W / n)]
        ys, xs = np.where(sub > 0)
        if len(ys) == 0:
            continue
        sub = sub[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        img = cv2.resize(255 - sub, (sub.shape[1] * 3, sub.shape[0] * 3),
                         interpolation=cv2.INTER_LANCZOS4)
        pad = cv2.copyMakeBorder(img, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=255)
        ch = ""
        if eng["rec"] is not None:
            res, _ = eng["rec"]([cv2.cvtColor(pad, cv2.COLOR_GRAY2BGR)])
            if res:
                ch = res[0][0]
        if not ch:
            ok, enc = cv2.imencode(".png", pad)
            ch = eng["cls"].classification(enc.tobytes())
        words.append(ch)
    return words, None


def _tip_words2(tpl):
    """提示条读字 + 返回每个字的二值形状 (供形状匹配), (words, shapes, err)"""
    eng = load_engines()
    if tpl.ndim != 3 or tpl.shape[2] != 4:
        return [], [], "提示条无 alpha"
    alpha = tpl[:, :, 3]
    line = cv2.morphologyEx(alpha, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (90, 1)))
    m2 = np.clip(alpha.astype(int) - (line > 0) * 255, 0, 255).astype(np.uint8)
    W = m2.shape[1]
    n = max(2, round(W / 71))
    words, shapes = [], []
    for k in range(n):
        sub = m2[:, int(k * W / n):int((k + 1) * W / n)]
        ys, xs = np.where(sub > 0)
        if len(ys) == 0:
            continue
        sub = sub[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        shapes.append(sub.copy())
        img = cv2.resize(255 - sub, (sub.shape[1] * 3, sub.shape[0] * 3),
                         interpolation=cv2.INTER_LANCZOS4)
        pad = cv2.copyMakeBorder(img, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=255)
        ch = ""
        if eng["rec"] is not None:
            res, _ = eng["rec"]([cv2.cvtColor(pad, cv2.COLOR_GRAY2BGR)])
            if res:
                ch = res[0][0]
        if not ch:
            ok, enc = cv2.imencode(".png", pad)
            ch = eng["cls"].classification(enc.tobytes())
        words.append(ch)
    return words, shapes, None


def _bg_char_mask(bg, box):
    """背景框字形的二值 mask: HSV 主色相分割"""
    x1, y1, x2, y2 = box
    crop = bg[y1:y2, x1:x2]
    big = cv2.resize(crop, (crop.shape[1] * 4, crop.shape[0] * 4),
                     interpolation=cv2.INTER_LANCZOS4)
    hsv = cv2.cvtColor(big, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[:, :, 0].astype(int), hsv[:, :, 1].astype(int), hsv[:, :, 2].astype(int)
    strong = (S > 40) & (V > 45)
    if strong.sum() < 150:
        return None
    hist = np.bincount(H[strong], minlength=180).astype(float)
    k = np.array([1, 2, 3, 2, 1], float); k /= k.sum()
    hs = np.convolve(np.concatenate([hist[-2:], hist, hist[:2]]), k, "same")[2:-2]
    hue = int(np.argmax(hs))
    m = ((np.abs(H - hue) < 14) & (S > 30)).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    ys, xs = np.where(m > 0)
    if len(ys) < 100:
        return None
    return m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def _hu_shape_dist(tsn, bg_mask):
    """Hu 矩 (log 变换) 距离, 旋转/缩放不变. tsn: 64x64 提示条形状, bg_mask: 背景字形 mask"""
    bmn = cv2.resize(bg_mask, (64, 64), interpolation=cv2.INTER_AREA)
    ha = cv2.HuMoments(cv2.moments(tsn, True)).flatten()
    hb = cv2.HuMoments(cv2.moments(bmn, True)).flatten()
    la = -np.sign(ha) * np.log10(np.abs(ha) + 1e-20)
    lb = -np.sign(hb) * np.log10(np.abs(hb) + 1e-20)
    return float(np.linalg.norm(la - lb))


def _bg_char_labels(bg, box):
    """背景字多预处理 × 多模型候选标签"""
    eng = load_engines()
    x1, y1, x2, y2 = box
    crop = bg[y1:y2, x1:x2]
    labels = set()
    big = cv2.resize(crop, (crop.shape[1] * 4, crop.shape[0] * 4),
                     interpolation=cv2.INTER_LANCZOS4)
    if eng["rec"] is not None:
        res, _ = eng["rec"]([big])
        if res and res[0][0]:
            labels.add(res[0][0])
    hsv = cv2.cvtColor(big, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[:, :, 0].astype(int), hsv[:, :, 1].astype(int), hsv[:, :, 2].astype(int)
    strong = (S > 40) & (V > 45)
    if strong.sum() > 150:
        hist = np.bincount(H[strong], minlength=180).astype(float)
        k = np.array([1, 2, 3, 2, 1], float); k /= k.sum()
        hs = np.convolve(np.concatenate([hist[-2:], hist, hist[:2]]), k, "same")[2:-2]
        hue = int(np.argmax(hs))
        m = ((np.abs(H - hue) < 13) & (S > 30)).astype(np.uint8) * 255
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        if eng["rec"] is not None:
            mb = cv2.resize(255 - m, (200, 200))
            pad = cv2.copyMakeBorder(mb, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
            res, _ = eng["rec"]([cv2.cvtColor(pad, cv2.COLOR_GRAY2BGR)])
            if res and res[0][0]:
                labels.add(res[0][0])
        ok, enc = cv2.imencode(".png", big)
        lb = eng["cls"].classification(enc.tobytes())
        if lb:
            labels.add(lb)
    return labels


_ROTATION_OCR = None
_SIMPLIFY = None


@lru_cache(maxsize=2)
def reference_backgrounds(shape):
    root = Path(os.environ.get("REFERENCE_DIR",
                               str(Path.home() / ".workbuddy/captcha_samples")))
    images = []
    for directory in ("paging_test", "live_test"):
        for path in sorted((root / directory).glob("bg_*.png")):
            image = cv2.imread(str(path))
            if image is not None and image.shape == shape:
                images.append(image)
    return images


def background_difference(bg, boxes):
    references = reference_backgrounds(bg.shape)
    known = np.ones(bg.shape[:2], bool)
    for x, y, right, bottom in boxes:
        known[max(0, y - 3):bottom + 3, max(0, x - 3):right + 3] = False
    known = known[::4, ::4]
    if known.mean() < 0.3:
        return None
    pixels = bg[::4, ::4][known].astype(np.int16)
    matches = []
    for index, image in enumerate(references):
        distance = float(np.abs(pixels - image[::4, ::4][known]).mean())
        if distance < 7 and not np.array_equal(image, bg):
            matches.append((distance, index))
    if len(matches) < 3:
        return None
    matches.sort()
    template = np.median([references[index] for _, index in matches[:11]], axis=0)
    return np.max(np.abs(bg.astype(np.int16) - template), axis=2)


def recover_undetected_boxes(bg, boxes):
    """Propose cells only when several detector boxes confirm the visible grid."""
    height, width = bg.shape[:2]
    scale = width / 400
    centers = [(round(x * scale), round(y * scale))
               for y in (64, 164) for x in (66, 166, 266, 366)]
    radius = round(30 * scale)
    occupied = set()
    for left, top, right, bottom in boxes:
        x, y = (left + right) / 2, (top + bottom) / 2
        for cell in centers:
            if abs(x - cell[0]) <= 12 * scale and abs(y - cell[1]) <= 14 * scale:
                occupied.add(cell)
    if not 4 <= len(occupied) < 6:
        return boxes, None
    foreground = background_difference(bg, boxes)
    if foreground is None:
        return boxes, None
    proposals = []
    for x, y in centers:
        if (x, y) in occupied:
            continue
        left, top = max(0, x - radius), max(0, y - radius)
        right, bottom = min(width, x + radius), min(height, y + radius)
        if bottom <= top or right <= left:
            continue
        density = float((foreground[top:bottom, left:right] > 18).mean())
        if 0.075 <= density <= 0.45:
            proposals.append((density, [left, top, right, bottom]))
    proposals.sort(key=lambda item: item[0], reverse=True)
    return boxes + [box for _, box in proposals[:6 - len(occupied)]], foreground


def word_engine():
    """Load the shared offline recognition model once per process."""
    global _ROTATION_OCR, _SIMPLIFY
    if _ROTATION_OCR is None:
        from rapidocr import RapidOCR
        from opencc import OpenCC
        _SIMPLIFY = OpenCC("t2s").convert
        _ROTATION_OCR = RapidOCR(params={
            "Global.use_det": False, "Global.use_cls": False,
            "EngineConfig.onnxruntime.intra_op_num_threads": 2,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        })
    return _ROTATION_OCR, _SIMPLIFY


def recover_prompt(tpl):
    if tpl.ndim != 3 or tpl.shape[2] != 4:
        return []
    model, simplify = word_engine()
    alpha = tpl[:, :, 3]
    chroma = np.ptp(tpl[:, :, :3], axis=2)
    reads = []
    for mask in ((chroma < 13) & (alpha > 65), (chroma < 22) & (alpha > 35), alpha > 110):
        gray = 255 - mask.astype(np.uint8) * 255
        padded = cv2.copyMakeBorder(gray, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
        out = model(padded, use_det=False, use_cls=False)
        if out.txts and out.scores[0] >= 0.90:
            label = simplify(out.txts[0])
            if len(label) == max(2, round(tpl.shape[1] / 71)) and all("\u3400" <= c <= "\u9fff" for c in label):
                reads.append(label)
    return list(reads[0]) if len(reads) >= 2 and len(set(reads)) == 1 else []


def rotated_word_votes(bg, box, targets, foreground=None):
    model, simplify = word_engine()
    x1, y1, x2, y2 = box
    h, saturation, value = cv2.split(cv2.cvtColor(bg[y1:y2, x1:x2], cv2.COLOR_BGR2HSV))
    hist = np.bincount(h[saturation > 100], minlength=180)
    peak = int(hist.argmax())
    hue_gap = np.abs(h.astype(int) - peak)
    hue_gap = np.minimum(hue_gap, 180 - hue_gap)
    masks = [(hue_gap < 9) & (saturation > t) for t in (65, 100, 140, 180)]
    masks += [(hue_gap < 12) & (saturation > 35) & (value > cutoff)
              for cutoff in (145, 175, 205)]
    original = _bg_char_mask(bg, box)
    if original is not None:
        masks.append(original > 0)
    if foreground is not None:
        masks = [foreground[y1:y2, x1:x2] > 25]
    supported = {}
    for mask in masks:
        if mask.sum() < 30 or mask.mean() > 0.65:
            continue
        glyph = np.full((110, 110), 255, np.uint8)
        glyph[25:85, 25:85] = 255 - cv2.resize(mask.astype(np.uint8) * 255, (60, 60),
                                               interpolation=cv2.INTER_NEAREST)
        last = None
        for angle in range(-100, 101, 5):
            mat = cv2.getRotationMatrix2D((55, 55), angle, 1)
            view = cv2.warpAffine(glyph, mat, (110, 110), borderValue=255)
            out = model(view, use_det=False, use_cls=False)
            if not out.txts:
                last = None
                continue
            char, score = simplify(out.txts[0]), out.scores[0]
            if char in targets and last is not None and last[0] == char:
                repeat = min(score, last[1])
                if repeat >= 0.88:
                    supported[char] = max(repeat, supported.get(char, 0))
            last = (char, score)
    return supported


def normalize_glyph(mask):
    y,x=np.where(mask)
    if len(x)<25: return None
    r=mask[y.min():y.max()+1,x.min():x.max()+1].astype(np.uint8)
    if min(r.shape)<4: return None
    # Retain character proportions when aligning the two cropped glyphs.
    sh = np.maximum(1,np.rint(np.array(r.shape)*42/max(r.shape))).astype(int)
    r=cv2.resize(r,(sh[1],sh[0]),interpolation=cv2.INTER_NEAREST)
    g=np.zeros((48,48),np.uint8)
    y,x=(48-sh)//2
    g[y:y+sh[0],x:x+sh[1]]=r
    return g

def glyph_distances(tpl, bg, boxes, word_count, delta, low_contrast=False):
    alpha=tpl[:,:,3]
    neutral=np.ptp(tpl[:,:,:3],axis=2)<22
    tips=[]
    boundaries = np.linspace(0, tpl.shape[1], word_count + 1, dtype=int)
    for a, b in zip(boundaries[:-1], boundaries[1:]):
        g=normalize_glyph(((alpha>55)&neutral)[:,a:b])
        tips.append(g)
    results=np.full((len(tips),len(boxes)),999.)
    tip_distances = [cv2.distanceTransform(1-t,cv2.DIST_L2,3) if t is not None else None for t in tips]
    for bi,(x,y,X,Y) in enumerate(boxes):
        h,s,v=cv2.split(cv2.cvtColor(bg[y:Y,x:X],cv2.COLOR_BGR2HSV))
        freq=np.bincount(h[s>100],minlength=180)
        hd=np.abs(h.astype(int)-freq.argmax()); hd=np.minimum(hd,180-hd)
        masks=[(hd<10)&(s>t) for t in (50,100,145,180)]
        masks.extend((hd<13)&(s>35)&(v>t) for t in (145,175,205))
        if low_contrast:
            masks = [(s < 45) & (v > t) for t in (165, 185, 205, 225)]
            masks.extend((hd < 13) & (v < t) & (s > 28) for t in (45, 65, 90, 120))
            if delta is not None:
                masks.extend(delta[y:Y, x:X] > t for t in (10, 15))
        elif delta is not None:
            masks.extend(delta[y:Y, x:X] > t for t in (22, 35, 55))
        for ma in masks:
            norm=normalize_glyph(ma)
            if norm is None or ma.mean()>.65:continue
            g=cv2.copyMakeBorder(norm,12,12,12,12,cv2.BORDER_CONSTANT,value=0)
            for a in range(0,91,4):
                r=cv2.warpAffine(g,cv2.getRotationMatrix2D((36,36),a,1),(72,72),flags=cv2.INTER_NEAREST)
                r=normalize_glyph(r)
                if r is None: continue
                dr=cv2.distanceTransform(1-r,cv2.DIST_L2,3)
                for ti,t in enumerate(tips):
                    if t is None:continue
                    dt=tip_distances[ti]
                    distance=(dr[t>0].mean()+dt[r>0].mean())/2
                    results[ti,bi]=min(distance,results[ti,bi])
    return results


def solve_word_click(bg, tpl):
    words, shapes, err = _tip_words2(tpl)
    recovered = recover_prompt(tpl)
    if recovered and len(recovered) == len(shapes):
        words = recovered
    if err or not words or any(len(word) != 1 for word in words):
        return result("WORD_IMAGE_CLICK", None, 0, True,
                      "提示条读字失败: %s, 转人工" % (err or "空"))
    ok, enc = cv2.imencode(".png", bg)
    boxes = load_engines()["det"].detection(enc.tobytes()) or []
    boxes, proposed_foreground = recover_undetected_boxes(bg, boxes)
    if len(boxes) < len(words):
        return result("WORD_IMAGE_CLICK", None, 0, True,
                      "背景仅检出 %d 个字框 < 目标 %d 字, 转人工" % (len(boxes), len(words)))
    boxes = sorted(boxes, key=lambda b: (b[1], b[0]))
    lab_sets = [_bg_char_labels(bg, b) for b in boxes]
    bg_masks = [_bg_char_mask(bg, b) for b in boxes]

    # 依序一对一分配 (贪心: 每个目标字取命中框, 已用不重复)
    seq, used, missing_slots = [], set(), []
    s, oy = disp_map(bg)
    targets = set(words)
    for slot, w in enumerate(words):
        labeled = [i for i, labels in enumerate(lab_sets) if w in labels]
        hit = labeled[0] if (len(labeled) == 1 and labeled[0] not in used and
                             len(lab_sets[labeled[0]] & targets) == 1 and words.count(w) == 1) else None
        if hit is None:
            missing_slots.append(slot)
            seq.append(None)
        else:
            used.add(hit)
            b = boxes[hit]
            seq.append([round((b[0] + b[2]) / 2 * s, 1), round((b[1] + b[3]) / 2 * oy, 1)])
    ocr_hits = len(words) - len(missing_slots)
    foreground = proposed_foreground
    if missing_slots and foreground is None:
        foreground = background_difference(bg, boxes)
    costs = glyph_distances(tpl, bg, boxes, len(words), foreground) if missing_slots else None

    def assign_glyphs(slots, max_distance, min_margin):
        unresolved_slots = []
        for slot in slots:
            ranked = np.argsort(costs[slot])
            best, runner = int(ranked[0]), int(ranked[1])
            value = float(costs[slot, best])
            margin = float(costs[slot, runner]) - value
            mutual_best = int(costs[:, best].argmin()) == slot
            if best in used or value > max_distance or margin < min_margin or not mutual_best:
                unresolved_slots.append(slot)
                continue
            used.add(best)
            b = boxes[best]
            seq[slot] = [round((b[0] + b[2]) / 2 * s, 1),
                         round((b[1] + b[3]) / 2 * oy, 1)]
        return unresolved_slots

    before_glyphs = len(missing_slots)
    if missing_slots:
        missing_slots = assign_glyphs(missing_slots, 0.36, 0.14)
    glyph_hits = before_glyphs - len(missing_slots)
    rotation_hits = 0
    if missing_slots:
        targets = {words[i] for i in missing_slots}
        votes = {i: rotated_word_votes(bg, box, targets)
                 for i, box in enumerate(boxes) if i not in used}
        lacking = targets - {word for vote in votes.values() for word in vote}
        if lacking:
            if foreground is not None:
                for i, box in enumerate(boxes):
                    if i not in used and not votes[i]:
                        votes[i] = rotated_word_votes(bg, box, lacking, foreground)
        remaining = []
        for slot in missing_slots:
            word = words[slot]
            choices = [i for i, vote in votes.items() if word in vote and len(vote) == 1]
            if len(choices) != 1 or choices[0] in used or words.count(word) != 1:
                remaining.append(slot)
                continue
            i = choices[0]
            used.add(i)
            b = boxes[i]
            seq[slot] = [round((b[0] + b[2]) / 2 * s, 1), round((b[1] + b[3]) / 2 * oy, 1)]
            rotation_hits += 1
        missing_slots = remaining

    before_glyphs = len(missing_slots)
    if missing_slots:
        missing_slots = assign_glyphs(missing_slots, 0.43, 0.12)
    glyph_hits += before_glyphs - len(missing_slots)
    if missing_slots and len(set(words)) == len(words):
        costs = np.minimum(costs, glyph_distances(tpl, bg, boxes, len(words), foreground, low_contrast=True))
        before_glyphs = len(missing_slots)
        missing_slots = assign_glyphs(missing_slots, 0.40, 0.115)
        glyph_hits += before_glyphs - len(missing_slots)

    shape_hits = 0
    if missing_slots:
        cand = [i for i in range(len(boxes)) if i not in used and bg_masks[i] is not None]
        pairs = []
        for slot in missing_slots:
            if shapes[slot] is None:
                continue
            tsn = cv2.resize(shapes[slot], (64, 64), interpolation=cv2.INTER_AREA)
            for bi in cand:
                d = _hu_shape_dist(tsn, bg_masks[bi])
                if d < 7.0:
                    pairs.append((d, slot, bi))
        pairs.sort()
        for _, slot, box_index in pairs:
            if seq[slot] is not None or box_index in used:
                continue
            used.add(box_index)
            b = boxes[box_index]
            seq[slot] = [round((b[0] + b[2]) / 2 * s, 1),
                         round((b[1] + b[3]) / 2 * oy, 1)]
            shape_hits += 1

    unresolved = [word for word, point in zip(words, seq) if point is None]
    found = len(words) - len(unresolved)
    conf = (0.9 * ocr_hits + 0.85 * rotation_hits + 0.82 * glyph_hits + 0.65 * shape_hits) / len(words)
    act = {"类型": "依次点击", "点序": seq, "目标字": words}
    return result("WORD_IMAGE_CLICK", act, conf, bool(unresolved),
                  "目标 %s, 候选分配 %d/%d (OCR %d, 旋转OCR %d, 字形 %d, Hu矩 %d)%s" %
                  ("".join(words), found, len(words), ocr_hits, rotation_hits, glyph_hits, shape_hits,
                   ", 未匹配:" + "".join(unresolved) if unresolved else ""),
                  候选框=[[int(v) for v in b] for b in boxes],
                  候选标签=[sorted(ls) for ls in lab_sets])


# ----------------------------------------------------------------------
# CONCAT: 横带对齐
# ----------------------------------------------------------------------

def solve_concat(bg, tpl=None, random_y=None):
    """
    CONCAT: 上下两层同一张图, 顶带内容被预位移(含wrap)。
    难点: 带边界 y 未知 (自然岩边会骗过单候选检测), 且天空区平滑导致相关平顶。
    解法: 扫描多个候选缝 y, 对每个缝计算位移能量曲线,
    真缝特征 = energy(位移0) 远大于 min energy (位移对比度)。
    拖动 = 最优位移 × 显示缩放 (带内容左移 s0 → 右移 s0 对齐)。
    """
    # A wrapped upper strip has a sustained vertical seam at its wrap point.
    # Restore it by rolling right by the remaining width.
    h, w = bg.shape[:2]
    sx, _ = disp_map(bg)
    colors = bg.astype(np.float32)
    known_y = h - int(random_y) if random_y is not None and 12 <= int(random_y) < h - 12 else None
    top = colors[3:max(6, min(h // 4, 60))]
    edges = np.abs(np.diff(top, axis=1)).mean(axis=2)
    vertical = np.median(edges, axis=0)
    min_x = max(1, int(w - END / sx))
    candidates = vertical[min_x - 1:w - 12]
    if len(candidates):
        seam = min_x + int(candidates.argmax())
        if known_y:
            # A building edge can be stronger than the actual wrap. Compare
            # similarly strong edges by how well their rows meet at the seam.
            nearby = np.argsort(candidates)[-12:] + min_x
            nearby = [int(x) for x in nearby if vertical[x - 1] >= 0.55 * vertical[seam - 1]]
            upper = colors[known_y - 1]
            lower = colors[known_y]
            residual = {x: float(np.abs(np.roll(upper, w - x, axis=0) - lower).mean()) for x in nearby}
            best_seam = min(residual, key=residual.get)
            if residual[seam] - residual[best_seam] >= 12 and residual[best_seam] <= 0.5 * residual[seam]:
                seam = best_seam
        strength = float(vertical[seam - 1])
        background_level = float(np.percentile(vertical, 95))
        shift = w - seam
        if strength >= 12 and strength >= 3 * max(1, background_level):
            # Confirm a horizontal boundary whose rows fit better after unwrapping.
            raw = np.abs(colors[1:] - colors[:-1]).mean(axis=(1, 2))
            aligned = np.abs(np.roll(colors[:-1], shift, axis=1) - colors[1:]).mean(axis=(1, 2))
            gain = raw - aligned
            start, end = max(12, h // 9), h - max(12, h // 10)
            y = start + int(gain[start:end].argmax())
            if known_y and abs(y + 1 - known_y) <= 4:
                near = known_y - 3
                y = near + int(gain[near:known_y + 2].argmax())
            matches_hint = known_y is not None and abs(y + 1 - known_y) <= 4
            validated_by_boundary = gain[y] >= 6 and aligned[y] <= 0.72 * raw[y]
            below_seam = np.abs(colors[known_y:, seam] - colors[known_y:, seam - 1]).mean(axis=1) if known_y else []
            validated_by_hint = (matches_hint and strength >= 20 and
                                 strength >= 4 * max(1, float(np.median(below_seam))) and
                                 aligned[y] <= raw[y])
            if validated_by_boundary or validated_by_hint:
                dist = shift * sx
                act = {"类型": "拖动", "拖动距离": round(dist, 1), "轨道上限": END,
                       "带缝y": y + 1, "位移raw": shift}
                return result("CONCAT", act, 0.88, False,
                              "竖直断缝 x=%d，上下边界 y=%d，复原后色差下降 %.1f" %
                              (seam, y + 1, gain[y]))
    g = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY).astype(np.float32)
    H, W = g.shape
    dy = np.abs(np.diff(g, axis=0)).mean(axis=1)
    idx = np.argsort(dy[20:min(150, H // 2)])[::-1][:40] + 20
    cands = []
    for y in idx:
        if all(abs(int(y) - c) >= 12 for c in cands):
            cands.append(int(y))
        if len(cands) >= 6:
            break
    best = None
    for ry in cands:
        band = g[max(0, ry - 12):ry - 1]
        below = g[ry + 1:ry + 12]
        if band.shape[0] < 4 or below.shape[0] < 4 or band.shape[1] < W:
            continue
        es = np.empty(W)
        for s in range(W):
            rolled = np.roll(band, s, axis=1)
            es[s] = (np.abs(below[0] - rolled[-1]).mean()
                     + 0.5 * np.abs(below[1] - rolled[-2]).mean())
        s0 = int(np.argmin(es))
        contrast = float(es[0] - es[s0])
        if best is None or contrast > best[0]:
            best = (contrast, ry, s0, float(es[0]), float(es[s0]))
    if best is None:
        return result("CONCAT", None, 0, True, "未找到候选缝, 转人工")
    contrast, ry, s0, e0, emin = best
    conf = max(0.0, min(1.0, (contrast - 5.0) / 60.0))
    s, _sy = disp_map(bg)
    dist = s0 * s
    act = {"类型": "拖动", "拖动距离": round(dist, 1), "轨道上限": END,
           "带缝y": ry, "位移raw": s0}
    return result("CONCAT", act, conf, contrast < 15.0,
                  "顶带位移 %dpx (缝y=%d, 位移对比度%.1f)" % (s0, ry, contrast),
                  位移=s0, 缝y=ry, 对比度=round(contrast, 1))


# ----------------------------------------------------------------------
# 统一入口
# ----------------------------------------------------------------------

def solve(bg_bytes, tpl_bytes, type_hint=None, extra=None):
    import cv2 as _cv
    bg = _cv.imdecode(np.frombuffer(bg_bytes, np.uint8), _cv.IMREAD_COLOR)
    tpl = None
    if tpl_bytes:
        tpl = _cv.imdecode(np.frombuffer(tpl_bytes, np.uint8), _cv.IMREAD_UNCHANGED)
    kind = type_hint
    if bg is None or (tpl is None and kind != "CONCAT"):
        return result(kind or "unknown", None, 0, True, "图片解码失败")
    if not kind:
        # 无提示时按模板几何判: 圆形alpha→ROTATE, 高<100的宽条→WORD, 全宽条→CONCAT, 否则SLIDER
        if tpl.shape[2] == 4:
            ys, xs = np.where(tpl[:, :, 3] > 10)
            if len(ys):
                w, h = xs.max() - xs.min(), ys.max() - ys.min()
                fill = (tpl[:, :, 3] > 10).sum() / max(1, w * h)
                if w > BG_W * 0.8:
                    kind = "CONCAT"
                elif h < 100 and w > 150:
                    kind = "WORD_IMAGE_CLICK"
                elif fill < 0.55 and abs(w - h) < max(w, h) * 0.4:
                    kind = "ROTATE"
                else:
                    kind = "SLIDER"
        if not kind:
            kind = "SLIDER"
    if kind == "CONCAT":
        return solve_concat(bg, tpl, (extra or {}).get("randomY"))
    fn = {"ROTATE": solve_rotate, "SLIDER": solve_slider,
          "WORD_IMAGE_CLICK": solve_word_click}.get(kind)
    if fn is None:
        return result(kind, None, 0, True, "该类型求解器未实现: %s" % kind)
    return fn(bg, tpl)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def run_dir(d):
    out = []
    for f in sorted(os.listdir(d)):
        if not f.endswith(".json"):
            continue
        p = os.path.join(d, f)
        j = json.load(open(p))
        if not isinstance(j, dict) or not j.get("data") or "id" not in j["data"]:
            continue
        data = j["data"]
        bg_b = base64.b64decode(data["backgroundImage"].split(",")[1])
        tpl_b = base64.b64decode(data["templateImage"].split(",")[1]) if data.get("templateImage") else b""
        r = solve(bg_b, tpl_b, data.get("type"), data.get("data") if isinstance(data.get("data"), dict) else None)
        r["题目ID"] = data["id"]
        r["图片"] = f
        out.append(r)
        act = r["动作"] or {}
        print("%-40s %-18s conf=%.3f %s" % (
            f[:40], r["验证类型"], r["置信度"],
            json.dumps(act, ensure_ascii=False)[:80]))
    json.dump(out, open(os.path.join(d, "solver_v2_results.json"), "w"),
              ensure_ascii=False, indent=1)
    print("\n结果已写入:", os.path.join(d, "solver_v2_results.json"))
    return out


def selftest():
    import base64
    d = os.path.expanduser("~/.workbuddy/captcha_samples/api_types")
    ok = True
    for f in sorted(os.listdir(d)):
        if not f.endswith(".json"):
            continue
        j = json.load(open(os.path.join(d, f)))
        if not isinstance(j, dict) or not j.get("data"):
            continue
        data = j["data"]
        tpl_b = base64.b64decode(data["templateImage"].split(",")[1]) if data.get("templateImage") else b""
        r = solve(base64.b64decode(data["backgroundImage"].split(",")[1]),
                  tpl_b, data.get("type"),
                  data.get("data") if isinstance(data.get("data"), dict) else None)
        print("%-30s %-18s conf=%.3f need_human=%s" % (
            f[:30], r["验证类型"], r["置信度"], r["需人工处理"]))
        if r["需人工处理"]:
            ok = False
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="验证码求解原子能力 V%s" % VERSION)
    ap.add_argument("--dir", help="批量解目录内 genCaptcha JSON")
    ap.add_argument("--bg", help="背景图 PNG")
    ap.add_argument("--tpl", help="模板图 PNG")
    ap.add_argument("--type", default=None, help="类型: ROTATE/SLIDER/CONCAT/WORD_IMAGE_CLICK")
    ap.add_argument("--random-y", default=None, help="CONCAT 带高 randomY")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.dir:
        run_dir(a.dir)
        return 0
    if a.bg:
        extra = {"randomY": int(a.random_y)} if a.random_y else None
        tpl_b = open(a.tpl, "rb").read() if a.tpl else b""
        r = solve(open(a.bg, "rb").read(), tpl_b, a.type, extra)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
