# L2 · 页面交互编排层（captcha-auto.mjs）

这一层负责「在真实网页里把验证码解掉」：等弹窗 → 存图 → 调 L1 识别 CLI → 把答案翻译成鼠标操作 → 核对回执。**它只关心组件怎么操作，不关心业务页面是什么** —— 换一个用同类组件的站点，这一层可以原样搬走。

## 文件

- `captcha-auto.mjs` —— 站点A（天爱 tianai-captcha 开源组件）实测版桥接，约 700 行。其他组件（如极验、腾讯 TCaptcha）按同样的协议另写一份同构文件即可。

## 它与上下两层的接口

```
L3 业务采集器（persistent-collector.mjs）
    │  import { attachCaptchaWatcher, tryAutoSolve } from "./captcha-auto.mjs"
    │  每次翻页后调用 resolveCaptchaOrSkip(page, targetPage, log)
    ▼
L2 captcha-auto.mjs（本层）
    │  stdin JSON → spawn L1 CLI（子进程，30s 超时）
    │  stdout JSON ← 解析 status=candidate / needs_human / error
    ▼
L1 识别原子（../l1-recognition/）
```

## 内部关键机制（改代码前必读）

1. **回执台账 `state.checks`**：判定「通过」的唯一依据是网络层 `captcha/check` 响应里的真实 verdict。verdict 未到时补等 1.5s、按挑战 id 补捞迟到回执（事件 `captcha_auto_late_receipt`）。**没有真实回执绝不宣称通过** —— 「拖完了」≠「提交了」≠「通过了」。
2. **`widgetScale()`**：一次性 `page.evaluate` 同步读 DOM 取组件几何。绝不能用会等待的 `locator.boundingBox()` —— 一个 30s 的静默等待会把 12s 的 check 响应窗口整个吃掉（这是 v1 最贵的坑，详见手册踩坑复盘）。
3. **每页一道题、一次提交**：不刷新、不换题、不自适应重提。未通过时按 `CAPTCHA_ROUND_GAP_MS`（默认 60s + 随机 15s）隔开再开新一轮，新一轮才允许点一次刷新换题；连续 `CAPTCHA_AUTO_ROUNDS`（默认 4）轮不过 → 抛 `captcha_auto_exhausted`，上层记断点跳页。
4. **连续 2 轮失败自动重载会话**跳过该页继续翻页，页面缺口由上层记录 `skippedPages` 并可定点补采（`START_PAGE=N` 续跑）。
5. **人化节奏**：操作带随机停顿与拖动轨迹；`CAPTCHA_HUMAN_MODE=0` 可回退到诊断模式。

## 事件（发到 captcha-events.jsonl）

`captcha_auto_solved` / `captcha_auto_round` / `captcha_auto_refresh` / `captcha_auto_late_receipt` / `captcha_auto_exhausted` / `captcha_auto_error` 等，完整字典见 `../index.html` 附录或 `../l3-protocol/README.md`。

## 适配新组件时改什么

| 改动点 | 位置 |
|---|---|
| 弹窗容器 / 底图 / 模板的 DOM 选择器 | 各 `resolve*` 函数顶部 |
| 组件几何（显示区尺寸、拖动轨道长度） | `widgetScale()` |
| 校验回执的网络响应特征 | watcher 的响应拦截 |
| 鼠标操作原语（拖动/点击序列） | 各题型执行器 |
| 传给 L1 的 JSON（题型映射、字段名） | `tryAutoSolve` 的请求构造 |
