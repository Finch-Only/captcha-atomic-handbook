# L3 · 业务调用协议层（参数、节奏与接入方式）

这一层是业务采集器与验证码能力之间的「交通规则」：什么时候解、解不过怎么办、页面之间隔多久、被限流了怎么让长跑跑得完。**全部是参数级复用**，换业务不改代码。

## 接入方式（采集器侧伪代码）

```js
import { attachCaptchaWatcher, tryAutoSolve } from "./captcha-auto.mjs";   // L2

// 每页流程（persistent-collector.mjs 的真实结构）：
await gotoPage(page, targetPage);
attachCaptchaWatcher(page, captchaLog);          // 挂网络回执监听
if (弹窗出现) {
  const ok = await resolveCaptchaOrSkip(page, targetPage, captchaLog);
  if (!ok) { skippedPages.push(targetPage); continue; }   // 记缺口，不中断
}
await waitForPayload();                          // 通过后等业务载荷（站点会补发被拦查询）
```

## 节奏参数（环境变量，均为实测调优值）

| 环境变量 | 默认值 | 含义 | 何时动 |
|---|---|---|---|
| `CAPTCHA_AUTO_ROUNDS` | 4 | 单页最多换题轮数，耗尽即断点跳页 | 基本不动 |
| `CAPTCHA_ROUND_GAP_MS` | 60000 | 换题轮间隔（下一轮才允许点刷新） | 出现限流时加大 |
| `CAPTCHA_ROUND_JITTER_MS` | 15000 | 轮间隔随机抖动 | — |
| `PAGE_GAP_MS` | 5000 | 页间隔基准（对齐人工节奏） | 限流回退时升到 8000 |
| `PAGE_GAP_JITTER_MS` | 4000 | 页间隔抖动 → 实际 5~9s | — |
| `BURST_PAGES` | 10 | 每 N 页进入一次批冷却 | 限流严重时降回 8 |
| `BURST_COOLDOWN_MS` | 25000 | 批冷却时长 | 回退顺序：先回 60s，再回页间隔 8s |
| `BURST_COOLDOWN_JITTER_MS` | 15000 | 冷却抖动 → 实际 25~40s | — |
| `CAPTCHA_HUMAN_MODE` | 1 | 1=模拟人工模式（默认）；0=诊断模式 | 排障时临时用 0 |

**调优口径**（实测得出，别拍脑袋改）：风控判定的是「短时间窗内连续申请/提交验证码」的爆破特征，而不是绝对速度。把高频动作离散化（分钟级换题、页间随机间隔、分批冷却）比整体放慢更有效。

## 题型映射（L2 把组件题翻译成 L1 的 type）

**天爱组件**（`profile: "tac"`）：`SLIDER→slider`、`ROTATE→rotate`、`CONCAT→concat`、`WORD_IMAGE_CLICK→ordered_click`。

**通用题型**（`profile: "generic"`，多数行业站点直接用）：
`文本类→digits/letters/alphanumeric/chinese`、`算术→arithmetic`、`语序填空→ordered_fill`、`点选→ordered_click`、`差分滑块→slider`、`通用旋转→rotate`。完整请求字段见 `../l1-recognition/README.md`。

## 失败语义（上层必须处理）

| 情况 | 表现 | 上层动作 |
|---|---|---|
| 单页 N 轮耗尽 | `captcha_auto_exhausted` | 记断点 + `skippedPages`，跳页继续 |
| 会话重载失败 | `session_reset_failed` / 达到上限 | 停止重试，如实上报 |
| 站点限流 | `site_throttle` | 停止，冷却后再跑，不硬刚 |
| 有缺口 | checkpoint `partial=true` + `skippedPages[]` | **必须检查**，用 `START_PAGE=N` 定点补采；不检查就会漏报数据缺口 |

## 合规边界

- 仅用于公开页面的公开数据采集，不绕过访问控制；
- `needs_human`（转人工）是能力的正式组成部分：识别不确定时必须交人工，不得强行突破；
- 节奏参数的目的是「模拟真人合理使用」，不是规避风控的攻击手段。
