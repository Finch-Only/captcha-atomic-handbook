/**
 * captcha-auto.mjs —— 业务采集器的验证码自动识别桥接（站点A 天爱组件实测版）
 *
 * 【2026-11-06 重构：模拟人工模式（默认）】
 * 实测对照：人工填码跑 332 页零风控；自动识别跑 3 页就触发「访问过于频繁」。
 * 差异不在识别准确率，而在调用方式 —— 人工是「一页只申请 1 道题、只提交 1 次、
 * 答完就等站点自己完成查询」，而旧版自动识别会在一页内换题、多策略重解、同题重提，
 * 相当于连续向 genCaptcha / captcha/check 打十几次，被判为爆破。
 *
 * 因此默认 HUMAN_MODE：只解当前这道题、只提交一次、不刷新不换题、不做自适应重解；
 * 解不出或未通过就直接交人工接管（人工在同一窗口接手，站点侧看到的仍是「一人一次」）。
 * 操作节奏也按人还原（看题思考、点选间隔、拖动轨迹带抖动与回弹）。
 * CAPTCHA_HUMAN_MODE=0 可退回旧的多轮重试模式（仅用于离线诊断，不建议跑现网）。
 *
 * 判定一律以 /captcha/check 的服务端回执为准（同一挑战 id + code 200 + success）。
 */
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";

const execFileAsync = promisify(execFile);

export const SKILL_DIR =
  process.env.CAPTCHA_SKILL_DIR || path.join(os.homedir(), ".workbuddy/skills/captcha-recognition");
const SKILL_RUN = path.join(SKILL_DIR, "run");
const TUNING_PATH = path.join(SKILL_DIR, "tuning.json");
// 技能资产目录（可能是软链）：参考底图必须落在这里才会被冻结引擎读到
const ASSET_DIR = path.join(SKILL_DIR, "assets", "tac-reference");
const LEARNED_DIR = path.join(ASSET_DIR, "live_test");
const LEARNED_INDEX = path.join(ASSET_DIR, "learned-index.jsonl");
const LEARNED_MAX = 40; // 参考库新增样本上限，超出按最旧淘汰
const DRAG_LIMIT = 242; // tac.min.js 硬编码轨道上限（组件坐标）

const TYPE_MAP = {
  SLIDER: "slider",
  ROTATE: "rotate",
  CONCAT: "concat",
  WORD_IMAGE_CLICK: "ordered_click",
};

const delay = (ms) => new Promise((r) => setTimeout(r, ms));
// 模拟人工模式：一页 = 一道题 = 一次提交。绝不主动刷新/换题（那等于连续申请新挑战）。
const HUMAN_MODE = process.env.CAPTCHA_HUMAN_MODE !== "0";
const humanPause = (min, max) => delay(Math.round(min + Math.random() * Math.max(0, max - min)));

// 站点限流识别：沿用 2026-11-05 现网验收里验证有效的策略 —— 一旦出现限流信号就停止重试，
// 不再继续提交（继续打只会加重风控）。429 与中文提示都要认。
const THROTTLE_RE = /频繁|稍后再试|限流|操作过快|请求过多|请稍候/i;
const isThrottle = (status, message) => status === 429 || THROTTLE_RE.test(String(message ?? ""));

/** 挂上 genCaptcha 监听，返回一个可读的共享状态对象。 */
export function attachCaptchaWatcher(page, { baseDir }) {
  const state = {
    latest: null,
    generation: null,
    attempted: new Set(),
    events: [],
    throttle: null,
    // 服务端校验回执台账。waitForResponse 有超时窗口，若某一步意外耗时过长，
    // 回执可能在窗口外才回来；这里留一份最近记录供 no_verdict 时补捞，避免误判失败。
    checks: [],
    baseDir,
  };
  const watchThrottle = (url, response, message) => {
    if (!isThrottle(response.status(), message)) return;
    if (!state.throttle) {
      state.throttle = {
        at: new Date().toISOString(),
        status: response.status(),
        message: String(message ?? "").slice(0, 160),
        endpoint: url.replace(/^https?:\/\/[^/]+/, "").slice(0, 120),
      };
    }
  };
  page.on("response", async (response) => {
    const url = response.url();
    if (/\/captcha\/check/.test(url)) {
      try {
        const body = await response.json();
        state.checks.push({
          status: response.status(),
          code: body?.code,
          success: body?.success,
          id: body?.data?.id,
          at: Date.now(),
        });
        if (state.checks.length > 20) state.checks.shift();
        watchThrottle(url, response, body?.msg ?? body?.message);
      } catch {
        watchThrottle(url, response, "");
      }
      return;
    }
    if (!/genCaptcha/i.test(response.url())) return;
    try {
      const body = await response.json();
      const data = body?.data;
      const message = String(body?.msg ?? body?.message ?? "").slice(0, 120);
      state.generation = {
        status: response.status(),
        code: body?.code,
        message,
        type: data?.type,
        hasId: Boolean(data?.id),
        hasImage: Boolean(data?.backgroundImage),
        at: new Date().toISOString(),
      };
      watchThrottle(url, response, message);
      if (data?.id && data.backgroundImage) state.latest = data;
    } catch {
      state.generation = { status: response.status(), error: "NON_JSON_RESPONSE", at: new Date().toISOString() };
      watchThrottle(url, response, "");
    }
  });
  return state;
}

async function logEvent(state, record) {
  const entry = { ...record, timestamp: new Date().toISOString() };
  state.events.push(entry);
  try {
    await fs.appendFile(path.join(state.baseDir, "captcha-events.jsonl"), JSON.stringify(entry) + "\n");
  } catch {
    /* 日志失败不影响主流程 */
  }
}

// ── 技能调用 ────────────────────────────────────────────────────────────
async function solveWithSkill(request, requestDir) {
  const file = path.join(requestDir, `request-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.json`);
  await fs.writeFile(file, JSON.stringify(request), "utf8");
  const started = Date.now();
  try {
    const { stdout } = await execFileAsync(SKILL_RUN, [file], { timeout: 30_000, maxBuffer: 4 * 1024 * 1024 });
    return { answer: parseLastJson(stdout), ms: Date.now() - started };
  } catch (error) {
    // status=error 时技能以退出码 2 结束，stdout 里仍有结构化结果
    const parsed = parseLastJson(error?.stdout);
    if (parsed) return { answer: parsed, ms: Date.now() - started };
    return { answer: { status: "error", reason: `skill_call_failed: ${error?.message ?? error}` }, ms: Date.now() - started };
  } finally {
    await fs.unlink(file).catch(() => {});
  }
}

function parseLastJson(stdout) {
  const last = String(stdout ?? "").trim().split("\n").filter(Boolean).at(-1);
  if (!last) return null;
  try {
    return JSON.parse(last);
  } catch {
    return null;
  }
}

// ── 页面动作 ────────────────────────────────────────────────────────────
/** 技能返回的是规范化字段：拖动类 drag_px，点字类 points；坐标空间 300×180。 */
async function applyAnswer(page, answer, scaleHint) {
  const picture = page.locator("#tianai-captcha-slider-bg-img").filter({ visible: true }).first();
  // 人看到题会先辨认一下再动手，不是题一出现就瞬间完成（旧版 450ms 拖完是明显的机器特征）。
  if (HUMAN_MODE) await humanPause(1200, 2800);

  if (Array.isArray(answer.points) && answer.points.length) {
    const rect = await picture.boundingBox();
    if (!rect) throw new Error("点字图片不可见");
    for (const [x, y] of answer.points) {
      if (!(x >= 0 && x <= 300 && y >= 0 && y <= 180)) throw new Error(`点选越界 ${x},${y}`);
      const px = rect.x + (x * rect.width) / 300;
      const py = rect.y + (y * rect.height) / 180;
      if (HUMAN_MODE) {
        // 鼠标先移到目标附近、再挪过去点，模拟人眼的定位过程
        await page.mouse.move(px - 8 - Math.random() * 12, py + 6 + Math.random() * 10);
        await humanPause(120, 300);
      }
      await page.mouse.click(px, py);
      await delay(HUMAN_MODE ? 350 + Math.floor(Math.random() * 350) : 180);
    }
    if (HUMAN_MODE) await humanPause(400, 900); // 点完看一眼再按确认
    await page
      .locator("#tianai-captcha .click-confirm-btn, [class*='click-confirm']")
      .filter({ visible: true })
      .first()
      .click({ timeout: 2000 });
    return { kind: "click", points: answer.points, scale: Number((rect.width / 300).toFixed(3)) };
  }

  const distance = Number(answer.drag_px);
  if (!Number.isFinite(distance)) throw new Error("技能未给出可执行的拖动距离");
  if (distance < 0 || distance > DRAG_LIMIT) throw new Error(`拖动距离越界 ${distance}`);

  const handle = page.locator("#tianai-captcha-slider-move-btn").filter({ visible: true }).first();
  await handle.click({ trial: true, timeout: 4000 });
  const rect = await handle.boundingBox();
  if (!rect) throw new Error("拖动控件不可见");
  // 组件按 300 宽设计；渲染宽度不同则按比例换算（实测 300 时与原样传像素等价）
  const scale = scaleHint && Math.abs(scaleHint - 1) > 0.05 ? scaleHint : 1;
  const target = distance * scale;
  const x = rect.x + rect.width / 2;
  const y = rect.y + rect.height / 2;
  await page.mouse.move(x, y);
  if (HUMAN_MODE) await humanPause(150, 400); // 按下前的停顿
  await page.mouse.down();
  try {
    // 人的拖动：先快后慢、步长不等、带轻微垂直抖动，末端有一次微调回弹。
    const steps = HUMAN_MODE ? 24 + Math.floor(Math.random() * 10) : 18;
    for (let step = 1; step <= steps; step += 1) {
      const ratio = step / steps;
      const eased = HUMAN_MODE ? 1 - Math.pow(1 - ratio, 1.6) : ratio;
      const jitterY = HUMAN_MODE ? (Math.random() - 0.5) * 1.6 : 0;
      await page.mouse.move(x + target * eased, y + jitterY);
      await delay(HUMAN_MODE ? 25 + Math.floor(Math.random() * 50) : 25);
    }
    if (HUMAN_MODE) {
      await page.mouse.move(x + target - 3, y);
      await humanPause(90, 170);
      await page.mouse.move(x + target, y); // 松手前必须精确落回目标距离
      await humanPause(80, 180);
    }
    const moved = await handle.boundingBox();
    return {
      kind: "drag",
      expected: Math.round(distance * 10) / 10,
      applied: Math.round(target * 10) / 10,
      observed: moved ? Math.round((moved.x - rect.x) * 10) / 10 : null,
    };
  } finally {
    await page.mouse.up();
  }
}

/**
 * 控件渲染宽度 / 300 的换算系数。
 *
 * ⚠️ 2026-11-06 根因修复（CONCAT 一直 no_verdict 的真凶）：
 *   CONCAT 大卡片题**没有** IMG#tianai-captcha-slider-bg-img —— 它的底图是
 *   DIV.tianai-captcha-slider-concat-bg-img 的 CSS background-image。
 *   旧实现用 locator.boundingBox() 等这个不存在的元素，会静默卡满 30 秒超时才 catch 返回 1，
 *   而 /captcha/check 的 waitForResponse 只有 12 秒：等回执的窗口在拖动开始前就过期了。
 *   于是拖动其实成功、站点也返回了校验回执，代码却记成 no_verdict，再白等 90 秒换题重来。
 *   表现很像「大卡片卡住 / 控件没渲染」，实际与识别、渲染都无关。
 * 现在改为一次性同步读 DOM，绝不使用任何会等待的 locator。
 */
async function widgetScale(page) {
  return page
    .evaluate(() => {
      const el =
        document.getElementById("tianai-captcha-slider-bg-img") ||
        document.querySelector("#tianai-captcha .tianai-captcha-slider-concat-bg-img") ||
        document.getElementById("tianai-captcha");
      const w = el ? el.getBoundingClientRect().width : 0;
      return w > 0 ? w / 300 : 1;
    })
    .catch(() => 1);
}

/**
 * 等控件可操作即动手。
 * ⚠️ 2026-11-06 更正：CONCAT 的大卡片高度由 loadCaptchaForData **同步**按 randomY 算出，
 *   不存在「挂载慢」。早期加的 12 秒等待 + h>=80 硬门槛是错误假设，会误杀 randomY 偏大的题，
 *   已移除；这里只打一条诊断信息，不作为阻塞条件。真正的病根是 widgetScale()，见下方。
 */
/** 等不到控件时把真实 DOM 打出来，便于定位大卡片用的是哪套元素。 */
async function widgetDiagnostics(page) {
  return page.evaluate(() => {
    const root = document.querySelector("#tianai-captcha") || document.querySelector('[id*="tianai-captcha"]');
    const pick = (e) => {
      const r = e.getBoundingClientRect();
      const st = getComputedStyle(e);
      return { tag: e.tagName, id: e.id, cls: String(e.className || "").slice(0, 60), w: Math.round(r.width), h: Math.round(r.height), disp: st.display, vis: st.visibility };
    };
    return {
      rootFound: !!root,
      root: root ? pick(root) : null,
      nodes: root ? Array.from(root.querySelectorAll("*")).slice(0, 45).map(pick) : [],
      candidates: Array.from(document.querySelectorAll('[id*="bg-img"], [class*="bg-img"], [id*="move-btn"], [class*="move-btn"], canvas, [id*="concat"]')).slice(0, 25).map(pick),
    };
  }).catch((e) => ({ error: String(e) }));
}

/**
 * 读取 CONCAT 大卡片的实时尺寸（仅用于诊断日志）。
 * 真实大卡片是 DIV#tianai-captcha-slider-concat-img-div，高度由 JS 同步设置；
 * IMG#...-concat-slider-img 是 src 恒空的占位元素，不要等它、也不要据此判断就绪。
 */
async function concatCardReady(page) {
  return page.evaluate(() => {
    const box = document.getElementById("tianai-captcha-slider-concat-img-div");
    const im = document.getElementById("tianai-captcha-slider-concat-slider-img");
    if (!box) return { ready: false, why: "no-concat-div" };
    const h = box.getBoundingClientRect().height;
    return {
      ready: h >= 80 || (im && (im.naturalWidth > 0 || (im.getAttribute("src") || "").length > 50)),
      h: Math.round(h),
      imgNatural: im ? im.naturalWidth : -1,
    };
  }).catch((e) => ({ ready: false, why: String(e) }));
}

async function waitWidgetForChallenge(page, challenge, timeout = 4000) {
  const isConcat = challenge?.type === "CONCAT";
  const until = Date.now() + timeout;
  const bg = page.locator("#tianai-captcha-slider-bg-img").filter({ visible: true }).first();
  // 大卡片题底图是 CSS 背景，没有 IMG#...-bg-img；只要拖动柄可见即可动手。
  const handle = page
    .locator("#tianai-captcha-slider-move-btn, [id*='tianai-captcha'][id*='move-btn'], [class*='slider-move-btn']")
    .filter({ visible: true })
    .first();
  while (Date.now() < until) {
    const frameVisible = (await bg.isVisible().catch(() => false)) || (await handle.isVisible().catch(() => false));
    if (frameVisible) {
      if (isConcat) {
        const card = await concatCardReady(page);
        if (!card.ready) console.log(`[CONCAT 卡片诊断] ${JSON.stringify(card)}（不作为阻塞条件）`);
      }
      await delay(350);
      return true;
    }
    await delay(120);
  }
  if (isConcat) {
    const dom = await widgetDiagnostics(page);
    console.log(`[CONCAT 控件诊断] ${JSON.stringify(dom).slice(0, 3500)}`);
  }
  return false;
}

async function refreshWidget(page) {
  const refresh = page
    .locator("#tianai-captcha-slider-refresh-btn, [id*='tianai-captcha'][id*='refresh']")
    .filter({ visible: true })
    .first();
  try {
    if (!(await refresh.isVisible())) return false;
    await refresh.click({ timeout: 2500 });
    return true;
  } catch {
    return false;
  }
}

async function nextChallenge(state, page, allowRefresh = false) {
  const fresh = () => (state.latest && !state.attempted.has(state.latest.id) ? state.latest : null);
  let challenge = fresh();
  if (challenge) return challenge;
  // ⚠️ 模拟人工：人解不出会隔一会儿换一道题，但绝不会 3 秒内连点 5 次刷新。
  // 这里先被动等站点把新题推过来（只读 watcher 状态，不发任何请求，最长 10 秒）；
  // 只有外层明确允许（每轮最多一次、轮间已隔 90 秒以上）才点一次刷新申请新挑战。
  if (HUMAN_MODE) {
    const waitForFresh = async (ms) => {
      const until = Date.now() + ms;
      while (Date.now() < until) {
        await delay(200);
        const c = fresh();
        if (c) return c;
      }
      return null;
    };
    challenge = await waitForFresh(10_000);
    if (challenge) return challenge;
    if (allowRefresh && (await refreshWidget(page))) {
      await logEvent(state, { event: "captcha_manual_like_refresh", note: "轮间已冷却，仅点一次刷新换题" });
      challenge = await waitForFresh(10_000);
    }
    return challenge;
  }
  if (!(await refreshWidget(page))) return null;
  const until = Date.now() + 9000;
  while (Date.now() < until) {
    await delay(200);
    challenge = fresh();
    if (challenge) return challenge;
  }
  return null;
}

// ── 自适应：样本沉淀 ─────────────────────────────────────────────────────
/**
 * 把题目底图归档进技能的参考底图库，供后续同底图差分使用。
 * 引擎只在「与当前图不完全相同且相似度够高」时采纳，且需 ≥3 张相似图才生效，
 * 因此新增样本不会污染当前这张图的候选，只会改善后续同类场景。
 */
async function learnSample(state, challenge, bgPath, { verified }) {
  const stem = String(challenge.id).replace(/[^A-Za-z0-9]/g, "").slice(0, 20) || `t${Date.now()}`;
  const dest = path.join(LEARNED_DIR, `bg_LEARNED_${stem}.png`);
  try {
    await fs.mkdir(LEARNED_DIR, { recursive: true });
    const stat = await fs.stat(dest).catch(() => null);
    if (!stat) {
      await fs.copyFile(bgPath, dest);
      const entry = {
        file: path.basename(dest),
        type: challenge.type,
        challenge: String(challenge.id),
        source: verified ? "server_pass" : "attempt",
        addedAt: new Date().toISOString(),
      };
      await fs.appendFile(LEARNED_INDEX, JSON.stringify(entry) + "\n");
      await pruneLearned();
    }
    return { added: !stat, dest };
  } catch (error) {
    return { added: false, error: String(error?.message ?? error) };
  }
}

async function pruneLearned() {
  const files = (await fs.readdir(LEARNED_DIR).catch(() => []))
    .filter((f) => f.startsWith("bg_LEARNED_"))
    .map(async (f) => ({ f, t: (await fs.stat(path.join(LEARNED_DIR, f))).mtimeMs }))
    .map((p) => p.catch(() => null));
  const list = (await Promise.all([...files].map((p) => p))).filter(Boolean).sort((a, b) => a.t - b.t);
  for (const item of list.slice(0, Math.max(0, list.length - LEARNED_MAX))) {
    await fs.unlink(path.join(LEARNED_DIR, item.f)).catch(() => {});
  }
}

// ── 自适应：策略统计（下次优先用通过率高的策略） ────────────────────────
async function readTuning() {
  try {
    return JSON.parse(await fs.readFile(TUNING_PATH, "utf8"));
  } catch {
    return { updatedAt: null, profiles: {} };
  }
}

async function recordStrategy(type, strategy, passed) {
  try {
    const tuning = await readTuning();
    tuning.profiles = tuning.profiles || {};
    const key = `${type}`;
    tuning.profiles[key] = tuning.profiles[key] || {};
    const cell = (tuning.profiles[key][strategy] = tuning.profiles[key][strategy] || { attempts: 0, pass: 0 });
    cell.attempts += 1;
    if (passed) cell.pass += 1;
    tuning.updatedAt = new Date().toISOString();
    await fs.writeFile(TUNING_PATH, JSON.stringify(tuning, null, 2), "utf8");
  } catch {
    /* 统计失败不影响主流程 */
  }
}

/** 按历史通过率排策略顺序；样本不足时保持默认顺序。 */
function orderStrategies(type, tuning, base) {
  const stats = tuning?.profiles?.[type] || {};
  const score = (s) => {
    const cell = stats[s];
    if (!cell || cell.attempts < 3) return -1;
    return cell.pass / cell.attempts;
  };
  return [...base].sort((a, b) => score(b) - score(a));
}

/**
 * 尝试自动过一道验证码。阶梯：直接解 → 换题重试 → 自适应（样本入库/备选策略）→ 再重试。
 * 返回 { ok:true, ... } 或 { ok:false, reason, attempts }
 */
export async function tryAutoSolve(page, state, { targetPage = 0, maxAttempts = 6, refreshOnce = false, log = console.log } = {}) {
  if (!(await exists(SKILL_RUN))) {
    await logEvent(state, { event: "captcha_auto_unavailable", targetPage, reason: "skill_run_missing", path: SKILL_RUN });
    return { ok: false, reason: "skill_run_missing" };
  }
  const sampleDir = path.join(state.baseDir, "captcha");
  await fs.mkdir(sampleDir, { recursive: true });
  const attempts = [];
  // 模拟人工：一页一道题、一次提交，用完这一次就交人工，不换题不重解。
  if (HUMAN_MODE) maxAttempts = 1;
  const tuning = await readTuning();
  const adaptFrom = Math.max(2, Math.ceil(maxAttempts / 2)); // 后半程进入自适应
  const seen = []; // 本题组已保存的样本，进入自适应后批量归档

  for (let i = 1; i <= maxAttempts; i += 1) {
    // 已出现限流信号：立刻停手，不再提交任何题目
    if (state.throttle) {
      await logEvent(state, { event: "captcha_auto_throttled", targetPage, attempt: i, throttle: state.throttle, attempts });
      log(`站点提示操作过于频繁（${state.throttle.status} ${state.throttle.message}），已停止自动重试。`);
      return { ok: false, reason: "site_throttle", throttle: state.throttle, attempts };
    }
    const adapting = !HUMAN_MODE && i > adaptFrom;
    const challenge = await nextChallenge(state, page, refreshOnce);
    if (!challenge) {
      attempts.push({ attempt: i, verdict: "no_challenge", generation: state.generation });
      await logEvent(state, { event: "captcha_auto_attempt", targetPage, attempt: i, verdict: "no_challenge", generation: state.generation });
      if (i < maxAttempts) await delay(800); // 没拿到题＝没提交，无需按限流节奏空等
      continue;
    }

    const kind = TYPE_MAP[challenge.type];
    if (!kind) {
      attempts.push({ attempt: i, verdict: "unsupported_type", type: challenge.type });
      await logEvent(state, { event: "captcha_auto_attempt", targetPage, attempt: i, verdict: "unsupported_type", type: challenge.type });
      break;
    }

    const stem = `${String(challenge.id).replace(/[^A-Za-z0-9]/g, "").slice(0, 16)}-${Date.now()}`;
    const bgPath = path.join(sampleDir, `${stem}-bg.png`);
    const tplPath = path.join(sampleDir, `${stem}-tpl.png`);
    const decode = (value) => Buffer.from(String(value).split(",").pop(), "base64");
    await fs.writeFile(bgPath, decode(challenge.backgroundImage));
    if (challenge.templateImage) await fs.writeFile(tplPath, decode(challenge.templateImage));
    seen.push({ challenge, bgPath, tplPath });

    // 自适应阶段：先把已有样本归档进参考库（改善后续同底图差分），再按通过率排序策略
    if (adapting && i === adaptFrom + 1) {
      let added = 0;
      for (const item of seen.slice(0, -1)) {
        const r = await learnSample(state, item.challenge, item.bgPath, { verified: false });
        if (r.added) added += 1;
      }
      await logEvent(state, { event: "captcha_auto_adapt", targetPage, addedSamples: added, corpus: LEARNED_DIR });
      log(`验证码自适应：参考库新增 ${added} 张样本，改用备选策略重试。`);
    }

    const rowHint = Number(challenge.data?.randomY);
    const ordered = orderStrategies(
      challenge.type,
      tuning,
      challenge.type === "CONCAT" && Number.isInteger(rowHint) && rowHint > 0
        ? ["default", "nolimit_row", "expand"]
        : ["default", "expand"]
    );
    // 人工只看一次题就给一个答案；多策略重解 = 同一题反复提交，是机器特征。
    const strategies = HUMAN_MODE ? ordered.slice(0, 1) : ordered;

    let solved = null;
    let submittedThisRound = false; // 本轮是否真的向站点提交过（未提交则不受限流节流约束）
    for (const strategy of strategies) {
      const request = { profile: "tac", type: kind, image: bgPath };
      if (challenge.templateImage) request.template = tplPath;
      if (strategy !== "nolimit_row" && Number.isInteger(rowHint) && rowHint > 0) request.row_hint = rowHint;
      // expand：额外做一次参考库增补后的重解（同一张图，差分候选可能变化）
      if (strategy === "expand") await learnSample(state, challenge, bgPath, { verified: false });

      const { answer, ms } = await solveWithSkill(request, sampleDir);
      const base = {
        attempt: i,
        page: targetPage,
        type: challenge.type,
        strategy: adapting ? `${strategy}+adapt` : strategy,
        skill_status: answer?.status,
        reason: answer?.reason ?? null,
        solve_ms: ms,
      };

      if (answer?.status !== "candidate" || !answer?.answer) {
        attempts.push({ ...base, verdict: "needs_human" });
        await logEvent(state, { event: "captcha_auto_attempt", targetPage, ...base, verdict: "needs_human" });
        log(`验证码自动识别（第 ${targetPage} 页 / 第 ${i} 轮）：${challenge.type} → 本轮跳过（${String(answer?.reason ?? answer?.status ?? "").slice(0, 50)}）`);
        continue;
      }

      // 提交并核服务端回执
      state.attempted.add(challenge.id);
      // 模拟人工：人会等控件渲染出来再动手（CONCAT 拼接题挂载较慢），最长 8 秒；
      // 期间不发任何请求，也绝不点刷新（刷新＝再申请一次挑战）。
      let ready = await waitWidgetForChallenge(page, challenge, HUMAN_MODE ? 8000 : 4000);
      if (!ready && !HUMAN_MODE) {
        // 旧版诊断模式：控件没渲染出来就要一张新题。
        await refreshWidget(page);
        await delay(600);
        ready = await waitWidgetForChallenge(page, challenge, 3000);
      }
      if (!ready) {
        const err = HUMAN_MODE ? "验证码控件 8 秒内未渲染，本轮跳过" : "验证码控件不可见（已尝试刷新题目）";
        attempts.push({ ...base, verdict: "apply_skipped", error: err });
        await logEvent(state, { event: "captcha_auto_attempt", targetPage, ...base, verdict: "apply_skipped", error: err });
        log(`验证码自动识别（第 ${targetPage} 页 / 第 ${i} 轮）：${challenge.type} → 跳过（${err}）`);
        break; // 换下一题
      }
      submittedThisRound = true;

      // 提交 + 核回执；没有回执时（控件未登记这次拖拽）同一题再试一次，避免整题被丢掉
      let verdict = "no_verdict";
      let receipt = null;
      let action = null;
      for (let submitTry = 1; submitTry <= (HUMAN_MODE ? 1 : 2); submitTry += 1) {
        const checkPromise = page
          .waitForResponse((r) => /\/captcha\/check/.test(r.url()) && r.request().method() === "POST", { timeout: 12_000 })
          .catch(() => null);
        try {
          action = await applyAnswer(page, answer.answer, await widgetScale(page));
        } catch (error) {
          const err = String(error?.message ?? error).split("\n")[0];
          attempts.push({ ...base, verdict: "apply_failed", error: err });
          await logEvent(state, { event: "captcha_auto_attempt", targetPage, ...base, verdict: "apply_failed", error: err });
          log(`验证码自动识别（第 ${targetPage} 页 / 第 ${i} 轮）：${challenge.type} → 操作失败（${err}）`);
          verdict = "apply_failed";
          break;
        }

        const response = await checkPromise;
        receipt = null;
        verdict = "no_verdict";
        if (response) {
          try {
            const body = await response.json();
            receipt = { status: response.status(), code: body?.code, success: body?.success, sameChallenge: body?.data?.id === challenge.id };
            verdict = response.ok() && body?.code === 200 && body?.success === true && receipt.sameChallenge ? "pass" : "rejected";
          } catch {
            verdict = "rejected";
            receipt = { status: response.status(), error: "NON_JSON_RESPONSE" };
          }
        }
        // 回执窗口过期 ≠ 提交失败：可能只是窗口外才回来（CONCAT 的 widgetScale 曾卡 30 秒）。
        // 补等一小会儿，再按挑战 id 从 watcher 台账捞真实服务端回执；没有回执绝不宣称通过。
        if (verdict !== "pass") {
          await delay(1500);
          const late = [...(state.checks || [])]
            .reverse()
            .find((c) => c.id === challenge.id && Date.now() - c.at < 60_000);
          if (late) {
            receipt = { status: late.status, code: late.code, success: late.success, sameChallenge: true, late: true };
            verdict = late.status >= 200 && late.status < 300 && late.code === 200 && late.success === true ? "pass" : "rejected";
            await logEvent(state, { event: "captcha_auto_late_receipt", targetPage, type: challenge.type, receipt });
          }
        }
        attempts.push({ ...base, submitTry, verdict, action, receipt });
        await recordStrategy(challenge.type, base.strategy, verdict === "pass");
        await logEvent(state, {
          event: verdict === "pass" ? "captcha_auto_solved" : "captcha_auto_attempt",
          targetPage,
          ...base,
          submitTry,
          verdict,
          action,
          receipt,
        });
        log(
          `验证码自动识别（第 ${targetPage} 页 / 第 ${i} 轮 / 策略 ${base.strategy}${submitTry > 1 ? " / 同题重提" : ""}）：${challenge.type} → ${
            verdict === "pass" ? "通过" : verdict
          }${answer?.reason ? `（${String(answer.reason).slice(0, 40)}）` : ""}`
        );
        if (verdict === "pass") {
          // 通过样本入参考库，越跑越准
          await learnSample(state, challenge, bgPath, { verified: true });
          return { ok: true, type: challenge.type, strategy: base.strategy, action, receipt, attempts };
        }
        if (HUMAN_MODE && verdict !== "pass") {
          // 本轮到此为止：绝不在同一页打第二次 check、也不再申请新题。
          // 外层会隔 90 秒以上再开新一轮（模拟人隔一会儿换题再试），全程无人接手。
          await logEvent(state, { event: "captcha_auto_round_failed", targetPage, verdict, type: challenge.type, receipt });
          log(`验证码本轮未通过（${verdict}），本页不再提交；稍后按人工间隔换题重试。`);
          return { ok: false, reason: "single_attempt_failed", verdict, attempts };
        }
        if (verdict === "rejected") {
          // ⚠️ 站点风控的真正触发点（2026-11-06 实测）：提交错误答案 = 爆破特征，
          // 一次被拒后站点立刻对 /captcha/genCaptcha 返回「访问过于频繁」并锁定验证码服务。
          // 因此被拒即停手冷却，绝不再申请新题、不再重提（重提只会加重风控）。
          const backoff = Math.max(30_000, Number(process.env.CAPTCHA_REJECT_BACKOFF_MS || 120_000));
          await logEvent(state, { event: "captcha_auto_rejected_backoff", targetPage, attempt: i, backoffMs: backoff, type: challenge.type, receipt });
          log(`验证码被拒（${challenge.type}）：立即停止重试并冷却 ${(backoff / 1000).toFixed(0)} 秒，避免触发站点「访问过于频繁」。`);
          await delay(backoff);
          return { ok: false, reason: "rejected_backoff", backoffMs: backoff, attempts };
        }
        if (verdict !== "no_verdict") break; // 操作失败等：换题
        if (submitTry < 2) await delay(1200); // 没回执（控件可能没登记这次拖拽）：同题再提一次
      }
      break; // 同一题已提交，换下一题
    }

    // 只有真的提交过才需要按限流节奏等待；未提交（跳过/无题）只留极短间隔，避免无谓空转
    if (i < maxAttempts) {
      if (submittedThisRound) await sleepGap();
      else await delay(600);
    }
    // 本轮提交或换题过程中若出现限流，下一轮开头会直接停下（这里先记一笔，便于复盘节奏）
    if (state.throttle) {
      await logEvent(state, { event: "captcha_auto_throttled", targetPage, attempt: i, throttle: state.throttle, attempts });
      log(`站点提示操作过于频繁（${state.throttle.status} ${state.throttle.message}），已停止自动重试。`);
      return { ok: false, reason: "site_throttle", throttle: state.throttle, attempts };
    }
    void solved;
  }

  await logEvent(state, { event: "captcha_auto_failed", targetPage, attempts });
  return { ok: false, reason: attempts.at(-1)?.verdict ?? "no_attempt", attempts };
}

async function exists(file) {
  try {
    await fs.access(file);
    return true;
  } catch {
    return false;
  }
}

function sleepGap() {
  // 站点限流诱因（2026-11-06 实测）：同一页 40 秒内连提 5 次验证码即触发。
  // 因此提交间隔改为「基准 9 秒 + 0~3 秒随机」，最快也要 45 秒才凑满 5 次；
  // 环境变量可覆盖，CAPTCHA_AUTO_GAP_MS 调大可进一步降速。
  const base = Math.max(3000, Number(process.env.CAPTCHA_AUTO_GAP_MS || 9000));
  const jitter = Math.max(0, Number(process.env.CAPTCHA_AUTO_GAP_JITTER_MS ?? 3000));
  return delay(base + Math.floor(Math.random() * jitter));
}

/** 汇总一次运行内的识别情况，供产物与日志使用。 */
export function summarize(state) {
  const list = state?.events ?? [];
  const attempts = list.filter((e) => e.event === "captcha_auto_attempt" || e.event === "captcha_auto_solved");
  return {
    autoAttempts: attempts.length,
    autoSolved: list.filter((e) => e.event === "captcha_auto_solved").length,
    adaptRounds: list.filter((e) => e.event === "captcha_auto_adapt").length,
    askedHuman: list.filter((e) => e.event === "captcha_auto_failed").length,
    byType: attempts.reduce((acc, e) => {
      const key = e.type || "unknown";
      acc[key] = acc[key] || { attempts: 0, pass: 0 };
      acc[key].attempts += 1;
      if (e.verdict === "pass") acc[key].pass += 1;
      return acc;
    }, {}),
  };
}
