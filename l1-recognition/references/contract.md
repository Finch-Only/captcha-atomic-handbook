# 调用契约

JSON 文件里的相对图片路径以 JSON 所在目录为准。通过标准输入传 JSON 时，相对路径以当前工作目录为准。原子工具只读本地文件，一次处理一道题，输出一行 JSON。图片建议 PNG 或 JPEG，每张不超过 8 MiB 和 400 万像素。

```sh
printf '%s\n' '{"type":"digits","image":"/absolute/path/code.png","length":4}' |
  ~/.codex/skills/captcha-recognition/run -
```

| 配置 | 题型 | 所需输入 | 候选答案 |
|---|---|---|---|
| `generic`（默认） | `digits` / `letters` / `alphanumeric` / `chinese` | `image`，可选 `length` | `text`，保留大小写、前导零 |
| `generic` | `arithmetic` | `image`，可选 `division_format` 为 `integer`（默认）或 `fraction` | `text` 和 `expression`。支持两个整数的加减乘除，除法结果按指定格式输出 |
| `generic` | `ordered_fill` | `image`、`boxes`、`order`。顺序由调用者提供 | `text` |
| `generic` | `ordered_click` | `image`、`boxes`、`cue`。提示例 `"山水木"` | `points`，图片像素 |
| `generic` | `slider` | `image`（有缺口）、`reference`（同尺寸完整底图） | `gap_center`，图片像素 |
| `generic` | `rotate` | `image` 和同尺寸方形正向 `reference`，或 `ring:[cx,cy,radius]` | `clockwise_degrees` |
| `tac` | `slider` / `rotate` / `ordered_click` | 原始 `image` 与 `template`，保持模板原有透明通道 | `drag_px` 或 `points`，站点A组件坐标 |
| `tac` | `concat` | 原始 `image`；如已取得站点给出的 `randomY`，传入 `row_hint` | `drag_px`，站点A组件坐标 |

`boxes` 为原图像素 `[[x1,y1,x2,y2],...]`，右下边界不包含。 `order` 为全部字格的从零开始的下标排列。通用顺序题由调用者提供字格与顺序，通用旋转需要可对齐的参考图或圆环几何。无参考的任意旋转、自由布局点字、语序推断和未知组件需单独适配。站点A坐标基于组件 300×180 显示区域、242 像素轨道，调用者负责用当前控件几何映射到页面。

示例请求：

```json
{"profile":"tac","type":"ordered_click","image":"/tmp/background.png","template":"/tmp/prompt.png"}
```

示例输出：

```json
{"schema_version":1,"type":"digits","profile":"generic","status":"candidate","site_verified":false,"answer":{"text":"0148"},"model":"ddddocr","score":null,"reason":null}
```

- `candidate`（退出码 0）为待核实候选，`needs_human`（退出码 0）为无法给出合适候选，`error`（退出码 2）为输入或运行环境异常。任务必须解析 `status`。
- 工具的 `site_verified` 固定为 `false`。定时任务应对同一挑战记录服务端回执，结果页是否成功另行核验。调度器建议设 30 秒子进程超时，并为新站点先用带答案的样本测整题正确率与错误放行率。
- 引擎使用 [ddddocr](https://github.com/sml2h3/ddddocr) 与 [RapidOCR](https://github.com/RapidAI/RapidOCR)。站点A求解内核取自 100 页现网冻结版，原验收全轮 100/103、首百题 97/100，四类分别为旋转 24/24、滑块 28/29、拼接 22/22、点字 26/28。这个成绩只适用于该轮站点A配置。通用场景只有离线样本验证。

## 跨机器使用

解压源码包后，在技能目录用 Python 3.12 建立环境。当前机器已配置。

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

依赖版本沿用当前 macOS 实测环境。先在目标运行器上试一次，确认模型可读、依赖可用、标准输出只有一行 JSON。站点A点字还需将已验证的 `ch_v4_rec_server.onnx`、`ppocr_keys_v1.txt` 放到技能目录的 `assets/`。源码包不附模型权重或真实验证码图片。两个文件的 SHA-256 分别是 `6a2676219be9907c7fc9cf61ebaa843bf2898777def567925b78886fcd90c07a` 和 `a1c84d9bdb9ab29043c58896224d32941783eb821629618416dcb08f12886492`。

当前机器的技能目录已保存站点A同底图差分所需的参考图副本。若运行机器使用其他位置，通过 `REFERENCE_DIR` 指向本地参考图根目录，目录包含 `paging_test/` 和 `live_test/` 下的 `bg_*.png`。没有这批参考图时，部分难图缺少差分候选，须重新做站点验收。定时任务用技能的绝对路径调用 CLI，任务目录可独立于技能。
