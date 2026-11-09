# L1 · 识别原子层（换站点零改动）

纯图像/几何求解器：输入验证码图片（stdin JSON），输出坐标/距离/文本（stdout JSON）。**不知道网页是什么、不碰浏览器** —— 换任何用同类题型的站点，这一层零改动。

支持 **10 种题型、2 个 profile**：`generic`（各行业普遍存在的通用题型）与 `tac`（天爱 tianai-captcha 组件特调，源自 100 页现网冻结版）。

## 文件

| 文件 | 职责 |
|---|---|
| `run` | CLI 入口：`echo '<JSON>' \| ./run -`，内部走 `.venv` 调 `scripts/recognize.py` |
| `scripts/recognize.py` | 协议层：解析请求 → 按 profile/题型分发 → 输出一行 JSON |
| `scripts/tac_engine.py` | `tac` 求解内核（四题型），含从前端 JS 逆向出的精确几何协议（242px 轨道等） |
| `references/contract.md` | 调用契约：请求/响应格式、题型覆盖边界、跨机器安装、验收成绩 |
| `requirements.txt` | 依赖（ddddocr + RapidOCR/ONNX 本地推理 + OpenCV） |
| `tuning.json` | 现网调优参数快照 |

## 快速上手

```sh
cd l1-recognition
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # 依赖装进 .venv（run 只认它）
echo '{"profile":"tac","type":"slider","image":"/abs/bg.png","template":"/abs/piece.png"}' | ./run -
# → {"schema_version":1,"status":"candidate","answer":{"drag_px":123},...}
```

## 通用题型（profile=`generic`，各行业普遍存在）

| type | 题型 | 请求字段 | 输出 | 判定方式 |
|---|---|---|---|---|
| `digits` / `letters` / `alphanumeric` / `chinese` | 文本类（数字/字母/数字字母混合/汉字） | `image`，可选 `length`（长度校验） | `{"text": "..."}` | OCR（ddddocr），结果必须整体匹配对应正则 |
| `arithmetic` | 算术题（如 `3×5=?`） | `image`，可选 `division_format`: `integer`(默认)/`fraction` | `{"text": "15", "expression": "3*5"}` | OCR 提式 + 符号归一（×x÷−）+ 精确计算；除零/非整数/提式失败 → 转人工 |
| `ordered_fill` | 语序填空（把字按指定顺序读出） | `image` + `boxes`（1–24 个 `[x1,y1,x2,y2]` 字格） + `order`（零基排列） | `{"text": "..."}` | 逐格 OCR，任一格无法唯一识别 → 转人工 |
| `ordered_click` | 点选（按提示依次点击字） | `image` + `boxes` + `cue`（点选提示词，字须唯一） | `{"points":[[x,y],...]}`，坐标为原图像素 | 逐格 OCR + 按提示排序；目标缺失/重复 → 转人工 |
| `slider` | 滑块（有干净底图对比） | `image`（带缺口图） + `reference`（同尺寸干净底图） | `{"gap_center":[x,y]}` | 图像差分 + 连通域，缺口必须唯一且明显（次大成分 ≥65% 即转人工） |
| `rotate` | 旋转（有参考或圆环几何） | `image` + `reference`（同尺寸正方形参考图）**或** `ring:[cx,cy,radius]` | `{"clockwise_degrees": 123}` | 参考图 3° 步进对齐 / 圆环内外环 1° 步进；多解相近 → 转人工 |

**示例：**

```sh
# 算术题
echo '{"profile":"generic","type":"arithmetic","image":"/abs/code.png"}' | ./run -
# → {"status":"candidate","answer":{"text":"15","expression":"3*5"},...}

# 点选题（调用者先用检测模型给出字格 boxes）
echo '{"profile":"generic","type":"ordered_click","image":"/abs/bg.png","boxes":[[10,20,58,68],[70,22,118,70]],"cue":"请依次点击：人民"}' | ./run -

# 通用旋转（无参考图，给圆环几何）
echo '{"profile":"generic","type":"rotate","image":"/abs/ring.png","ring":[120,120,86]}' | ./run -
```

## 天爱组件题型（profile=`tac`，特调版）

| type | 题型 | 输入 | 输出 |
|---|---|---|---|
| `slider` | 滑块拼图 | 背景图 + 带透明通道的拼图块 | `drag_px` |
| `rotate` | 旋转转正 | 背景图（圆洞）+ 圆内容 | `drag_px`（角度×242/360） |
| `concat` | 横带对齐 | 背景图 + 模板（可传 `row_hint`） | `drag_px` |
| `ordered_click` | 点字 | 底图 + 字条图 | `points`（300×180 组件坐标系） |

`tac` 输出的坐标为**组件坐标系**（300×180 显示区、242px 轨道），调用者（L2）负责映射到页面坐标；`generic` 输出为**原图像素坐标**。

## 输出状态

- `candidate` —— 有答案，附置信度/判定引擎；`needs_human` —— 识别不确定，**转人工是正常分支，不是故障**；`error` —— 环境或输入问题（缺 `.venv`、缺模型、参数非法）。
- 输入图片默认**不留存**；需要评估时由调用者自行按题型分组保存正确/拒绝/转人工记录。
- `site_verified` 恒为 `false`：判定「通过」要靠调用方记录同一挑战的服务端回执，别只信模型分数。

## ⚠️ 能力边界：这些明确不在范围内

本能力是**图片验证码识别**能力。以下类型**不要尝试接入**，正确姿势是 `needs_human` 转人工或放弃该站点：

| 类型 | 典型例子 | 原因 |
|---|---|---|
| 行为验证码 | 极验（12306 点选）、腾讯 TCaptcha（微信/QQ 登录滑块）、reCAPTCHA | 考的是鼠标轨迹/加速度/浏览器指纹，`isTrusted` 事件标记可直接暴露脚本；缺口算对也可能判失败 |
| 无预设字格的点选 | 自由布局"点击图中所有红绿灯" | 需要先跑目标检测框出目标，本引擎的 `ordered_click` 只接受调用者传入的 `boxes` |
| 短信/邮箱验证码 | 手机验证码登录 | 走接口不走图片，与本能力无关 |
| 云端动态难度 | Google reCAPTCHA v3 无感模式 | 对整段浏览行为打分，连"题"都没有 |

## ⚠️ 不随仓库附带的东西（按 contract.md 自取）

1. **模型权重**：`ch_v4_rec_server.onnx` + `ppocr_keys_v1.txt`（点字题需要），放到本目录 `assets/`；两个文件的 SHA-256 校验值写在 `references/contract.md`（先校验再使用）。
2. **站点参考图**（`tac` 差分候选用）：放到 `assets/tac-reference/`（`REFERENCE_DIR` 环境变量可指向任意本地目录）。没有参考图时部分难图缺少差分候选，需对新站点重做验收。`generic` 的 slider/rotate 不需要它（参考图由调用者随请求传入）。

## 验收基准（离线可复测）

`tac` 四题型共 103 题：旋转 24/24、滑块 28/29、拼接 22/22、点字 26/28（详见 contract.md）。`generic` 各题型接入新站点时，先拿带答案样本测整题正确率与**错误放行率**，再上现网。
