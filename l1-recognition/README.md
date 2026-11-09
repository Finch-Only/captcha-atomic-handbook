# L1 · 识别原子层（换站点零改动）

纯图像/几何求解器：输入验证码图片（stdin JSON），输出坐标/距离（stdout JSON）。**不知道网页是什么、不碰浏览器** —— 换任何用同类题型的站点，这一层零改动。

## 文件

| 文件 | 职责 |
|---|---|
| `run` | CLI 入口：`echo '<JSON>' | ./run -`，内部走 `.venv` 调 `scripts/recognize.py` |
| `scripts/recognize.py` | 协议层：解析请求 → 按 profile 分发 → 输出一行 JSON |
| `scripts/tac_engine.py` | 求解内核（天爱四题型）：模板匹配 + 几何协议 + OCR 差分，源自 100 页现网冻结版 |
| `references/contract.md` | 调用契约：请求/响应格式、题型覆盖边界、跨机器安装、验收成绩 |
| `requirements.txt` | 依赖（ddddocr + RapidOCR/ONNX 本地推理） |
| `tuning.json` | 现网调优参数快照 |

## 快速上手

```sh
cd l1-recognition
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # 依赖装进 .venv（run 只认它）
echo '{"profile":"tac","type":"slider","image":"/abs/bg.png","template":"/abs/piece.png"}' | ./run -
# → {"schema_version":1,"status":"candidate","answer":{"drag_px":123},...}
```

## 支持的题型

| type | 题型 | 输入 | 输出 |
|---|---|---|---|
| `slider` | 滑块拼图 | 背景图 + 带透明通道的拼图块 | `drag_px` |
| `rotate` | 旋转转正 | 背景图（圆洞）+ 圆内容 | `drag_px`（角度×242/360） |
| `concat` | 横带对齐 | 背景图 + 模板（可传 `row_hint`） | `drag_px` |
| `ordered_click` | 点字 | 底图 + 字条图 | `points`（坐标序列，300×180 坐标系） |

## 输出状态

- `candidate` —— 有答案，置信度已给出；
- `needs_human` —— 识别不确定，**转人工是正常分支，不是故障**；
- `error` —— 环境或输入问题（如缺 `.venv`、缺模型）。

## ⚠️ 不随仓库附带的东西（按 contract.md 自取）

1. **模型权重**：`ch_v4_rec_server.onnx` + `ppocr_keys_v1.txt`，放到本目录 `assets/`；两个文件的 SHA-256 校验值写在 `references/contract.md`（先校验再使用）。
2. **站点参考图**（差分候选用）：放到 `assets/tac-reference/`（原环境 `REFERENCE_DIR` 环境变量可指向任意本地目录）。没有参考图时部分难图缺少差分候选，需对新站点重做验收。

## 验收基准（离线可复测）

四题型共 103 题：旋转 24/24、滑块 28/29、拼接 22/22、点字 26/28（详见 contract.md）。接入新站点时，先拿带答案样本测整题正确率与**错误放行率**，再上现网。
