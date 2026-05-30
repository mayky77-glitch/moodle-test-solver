const DEFAULT_BACKEND_BASE_URL = "http://127.0.0.1:8765";
const CONTENT_VERSION = "0.1.11-debug";
const POLL_MS = 1200;
const REQUEST_TIMEOUT_MS = 15000;
const EMPTY_PANEL_RESET_MS = 1800;

let lastPayloadKey = "";
let panelBody = null;
let listCollapsed = false;
let solverEnabled = true;
let overlayVisible = true;
let autoSelectEnabled = false;
let manualAnswerText = "";
let latestRenderState = { results: [] };
let requestInFlight = false;
let emptyPanelSince = 0;
let lastBackendVersion = "";

function visible(element) {
  const style = window.getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
}

function textOf(element) {
  return (element?.innerText || element?.textContent || "").replace(/\s+/g, " ").trim();
}

function normalizeOption(text) {
  return text.replace(/^(a|b|c|d|e|f|g|h|i|j|[а-яё])\.|^\d+[.)]/i, "").trim();
}

function isNonAnswerOption(text) {
  const normalized = normalizeOption(text)
    .toLowerCase()
    .replace(/[.!?…]+$/g, "")
    .trim();
  if (!normalized) return true;
  return [
    "очистить мой выбор",
    "сбросить мой выбор",
    "очистить выбор",
    "сбросить выбор",
    "clear my choice",
    "clear choice",
    "remove my choice",
    "пока нет ответа",
    "ответ сохранен",
    "отметить вопрос",
  ].includes(normalized);
}

function optionTextForInput(input) {
  const label = input.closest("label") || (input.id ? document.querySelector(`label[for="${CSS.escape(input.id)}"]`) : null);
  const container =
    input.closest(".answer .r0, .answer .r1, .answer li, .answer .option, .answer .form-check, li, .option, .form-check, label") ||
    input.parentElement;
  return normalizeOption(textOf(label || container || input));
}

function cleanQuestionText(text) {
  const junkPatterns = [
    /^текст вопроса$/i,
    /^вопрос\s*\d+$/i,
    /^пока нет ответа$/i,
    /^ответ сохранен$/i,
    /^балл\s*:/i,
    /^отметить вопрос$/i,
    /^вопрос\s*\d+\s+ответ$/i,
  ];
  return String(text || "")
    .split(/\n| {2,}/)
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter((line) => line && !junkPatterns.some((pattern) => pattern.test(line)))
    .join(" ")
    .trim();
}

function questionTextFrom(root) {
  const qtext = root.querySelector(".qtext");
  if (qtext) {
    const direct = cleanQuestionText(textOf(qtext));
    if (direct.length > 8) return direct;
  }

  const preferredNodes = Array.from(root.querySelectorAll(".qtext p, .qtext div, .formulation p, .formulation div")).filter(visible);
  const preferred = preferredNodes.map((node) => cleanQuestionText(textOf(node))).find((text) => text.length > 8);
  if (preferred) return preferred;

  const fallbackNodes = Array.from(root.querySelectorAll("legend, .prompt, [data-question], h2, h3, h4")).filter(visible);
  const fallback = fallbackNodes.map((node) => cleanQuestionText(textOf(node))).find((text) => text.length > 8);
  if (fallback) return fallback;

  return cleanQuestionText(textOf(root)).slice(0, 1200);
}

function questionNumberFrom(root) {
  const candidates = [
    textOf(root.querySelector(".info .no")),
    textOf(root.querySelector(".no")),
    root.id || "",
    textOf(root),
  ];
  for (const candidate of candidates) {
    const match = String(candidate || "").match(/(?:question-|вопрос\s*)(\d+)/i);
    if (match) return Number(match[1]);
  }
  return null;
}

function readQuestion(root) {
  const inputs = Array.from(root.querySelectorAll("input, textarea, select")).filter(visible);
  const choiceInputs = inputs.filter((input) => ["radio", "checkbox"].includes((input.type || "").toLowerCase()));
  const textInputs = inputs.filter((input) => {
    const type = (input.type || "").toLowerCase();
    return ["text", "search", ""].includes(type) || input.tagName.toLowerCase() === "textarea";
  });

  const options = choiceInputs.map(optionTextForInput).filter((option) => option && !isNonAnswerOption(option));

  const questionText = questionTextFrom(root);
  const questionNumber = questionNumberFrom(root);

  let kind = "unknown";
  if (choiceInputs.some((input) => input.type === "checkbox")) kind = "multiple_choice";
  else if (choiceInputs.some((input) => input.type === "radio")) kind = "single_choice";
  else if (textInputs.length) kind = "text";

  return { text: questionText, kind, options: [...new Set(options)], questionNumber };
}

function reviewStatusFrom(root) {
  const classText = root.className || "";
  const bodyText = textOf(root).toLowerCase();
  if (/\bincorrect\b/.test(classText) || /(?:неверно|ваш ответ неверный|баллов:\s*0\s+из)/i.test(bodyText)) {
    return "incorrect";
  }
  if (/\bpartiallycorrect\b/.test(classText) || /(?:частично|частично верно)/i.test(bodyText)) {
    return "partial";
  }
  if (/\bcorrect\b/.test(classText) || /(?:верно|ваш ответ верный|баллов:\s*1\s+из\s*1)/i.test(bodyText)) {
    return "correct";
  }
  return "unknown";
}

function isReviewPage() {
  if (location.pathname.includes("/mod/quiz/review.php")) return true;

  const reviewSelectors = [
    ".rightanswer",
    ".correctanswer",
    ".specificfeedback",
    ".outcome",
    ".que.correct",
    ".que.incorrect",
    ".que.partiallycorrect",
  ];
  if (reviewSelectors.some((selector) => document.querySelector(selector))) return true;

  const bodyText = textOf(document.body).toLowerCase();
  return /(?:ваш ответ верный|ваш ответ неверный|правильный ответ|баллов:\s*\d+\s+из\s*\d+)/i.test(bodyText);
}

function isStatsPage() {
  return ["/mod/quiz/summary.php", "/mod/quiz/view.php"].some((path) => location.pathname.includes(path));
}

function readQuestions() {
  const moodleQuestions = Array.from(document.querySelectorAll(".que")).filter(visible);
  if (!moodleQuestions.length && !location.pathname.includes("/mod/quiz/attempt.php")) {
    return [];
  }
  const roots = moodleQuestions.length ? moodleQuestions : [document.querySelector("form") || document.body];
  return roots.map(readQuestion).filter((question) => question.text || question.options.length);
}

function parseRightAnswerText(text) {
  const cleaned = text.replace(/\s+/g, " ").trim();
  const match = cleaned.match(/(?:правильн(?:ый|ые)\s+ответ(?:ы)?|the correct answer is|correct answer(?:s)?)(?:\s+is)?\s*[:：]\s*(.+)$/i);
  if (!match) return [];
  return match[1]
    .split(/\s*(?:;|,|\bor\b|\bили\b)\s*/i)
    .map((answer) => normalizeOption(answer.replace(/^["'«]|["'»]$/g, "")))
    .filter(Boolean);
}

function readCorrectQuestion(root) {
  const question = readQuestion(root);
  const answers = [];
  const selectedAnswers = Array.from(root.querySelectorAll("input[type='radio']:checked, input[type='checkbox']:checked"))
    .map((input) => {
      const label = input.closest("label") || document.querySelector(`label[for="${CSS.escape(input.id || "")}"]`);
      const container = input.closest("li, .answer, .option, .form-check, .r0, .r1, label, div") || input.parentElement;
      return normalizeOption(textOf(label || container || input));
    })
    .filter(Boolean);

  for (const node of Array.from(root.querySelectorAll(".rightanswer, .correctanswer, .outcome .feedback, .specificfeedback"))) {
    answers.push(...parseRightAnswerText(textOf(node)));
  }

  for (const node of Array.from(root.querySelectorAll(".answer .correct, .answer .rightanswer, .r0.correct, .r1.correct"))) {
    const label = node.querySelector("label") || node;
    const option = normalizeOption(textOf(label));
    if (option && question.options.includes(option)) answers.push(option);
  }

  const uniqueAnswers = [...new Set(answers)].filter(Boolean);
  return {
    ...question,
    correctAnswers: uniqueAnswers,
    selectedAnswers: [...new Set(selectedAnswers)],
    reviewStatus: reviewStatusFrom(root),
    feedback: textOf(root).slice(0, 1000),
  };
}

function readCorrectQuestions() {
  if (!isReviewPage()) return [];
  const roots = Array.from(document.querySelectorAll(".que")).filter(visible);
  return roots.map(readCorrectQuestion).filter((question) => question.text || question.correctAnswers.length);
}

async function sendQuestions(questions) {
  const backendBaseUrl = await readBackendBaseUrl();
  return postJsonWithTimeout(`${backendBaseUrl}/answer`, { url: location.href, title: document.title, questions });
}

async function sendCorrectQuestions(questions) {
  const backendBaseUrl = await readBackendBaseUrl();
  return postJsonWithTimeout(`${backendBaseUrl}/correct`, { url: location.href, title: document.title, questions });
}

async function sendStatsRequest() {
  const backendBaseUrl = await readBackendBaseUrl();
  return getJsonWithTimeout(`${backendBaseUrl}/stats`);
}

async function getJsonWithTimeout(url) {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(url, { signal: controller.signal });
    const responsePayload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(responsePayload.error ? `backend ${response.status}: ${responsePayload.error}` : `backend ${response.status}`);
    }
    return responsePayload;
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new Error(`backend timeout after ${Math.round(REQUEST_TIMEOUT_MS / 1000)}s`);
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

async function postJsonWithTimeout(url, payload) {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    const responsePayload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(responsePayload.error ? `backend ${response.status}: ${responsePayload.error}` : `backend ${response.status}`);
    }
    return responsePayload;
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new Error(`backend timeout after ${Math.round(REQUEST_TIMEOUT_MS / 1000)}s`);
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function ensurePanel() {
  let panel = document.getElementById("test-solver-panel");
  if (panel) {
    if (panel.dataset.contentVersion !== CONTENT_VERSION) {
      panel.remove();
      panelBody = null;
      panel = null;
    }
  }
  if (panel) {
    panelBody = panel.querySelector("#test-solver-body");
    if (!panelBody) {
      panel.remove();
      panelBody = null;
    } else {
      if (!panelBody.textContent.trim()) {
        panelBody.innerHTML = statusHtml("Поиск ответа...");
      }
      if (localStorage.getItem("testSolverPanelCollapsed") !== "1") {
        panelBody.style.display = "block";
        panel.querySelector("#test-solver-toggle").textContent = "−";
      }
      panel.dataset.contentVersion = CONTENT_VERSION;
      return panel;
    }
  }

  panel = document.createElement("div");
  panel.id = "test-solver-panel";
  panel.dataset.contentVersion = CONTENT_VERSION;
  const savedPosition = JSON.parse(localStorage.getItem("testSolverPanelPosition") || "null");
  const isCollapsed = localStorage.getItem("testSolverPanelCollapsed") === "1";
  listCollapsed = localStorage.getItem("testSolverListCollapsed") === "1";
  const initialPosition = clampPanelPosition(savedPosition || { left: 16, top: 72 }, 420, 360);
  panel.style.cssText = [
    "position:fixed",
    `left:${initialPosition.left}px`,
    `top:${initialPosition.top}px`,
    "z-index:2147483647",
    "max-width:420px",
    "min-width:260px",
    "max-height:72vh",
    "font:14px/1.4 Arial,sans-serif",
    "background:#111827",
    "color:#f9fafb",
    "border:1px solid #374151",
    "border-radius:10px",
    "box-shadow:0 10px 30px rgba(0,0,0,.35)",
    "overflow:hidden",
  ].join(";");
  panel.innerHTML = `
    <div id="test-solver-header" style="display:flex;align-items:center;justify-content:space-between;gap:12px;cursor:move;padding:10px 12px;border-bottom:1px solid #374151">
      <div><strong>Test Solver</strong><span style="margin-left:8px;color:#9ca3af;font-size:11px">v${escapeHtml(CONTENT_VERSION)}</span></div>
      <button id="test-solver-toggle" type="button" style="cursor:pointer;border:1px solid #4b5563;background:#1f2937;color:#f9fafb;border-radius:6px;padding:2px 8px">${isCollapsed ? "+" : "−"}</button>
    </div>
    <div id="test-solver-body" style="padding:12px;${isCollapsed ? "display:none" : ""};min-height:34px;max-height:calc(72vh - 46px);overflow:hidden">${statusHtml("Читаю вопрос...")}</div>
  `;
  document.documentElement.appendChild(panel);
  panelBody = panel.querySelector("#test-solver-body");
  enablePanelControls(panel);
  requestAnimationFrame(() => clampPanelToViewport(panel));
  return panel;
}

function clampPanelPosition(position, width, height) {
  const left = Math.max(0, Math.min(window.innerWidth - Math.min(width, window.innerWidth), Number(position.left || 16)));
  const top = Math.max(0, Math.min(window.innerHeight - Math.min(height, window.innerHeight), Number(position.top || 72)));
  return { left: Math.round(left), top: Math.round(top) };
}

function clampPanelToViewport(panel) {
  const rect = panel.getBoundingClientRect();
  const left = Math.max(0, Math.min(window.innerWidth - rect.width, rect.left));
  const top = Math.max(0, Math.min(window.innerHeight - Math.min(rect.height, window.innerHeight), rect.top));
  panel.style.left = `${Math.round(left)}px`;
  panel.style.top = `${Math.round(top)}px`;
  panel.style.right = "auto";
  panel.style.bottom = "auto";
}

function removePanel() {
  document.getElementById("test-solver-panel")?.remove();
  panelBody = null;
}

function panelContentIsEmpty() {
  const body = panelBody || document.querySelector("#test-solver-body");
  return !body || !body.textContent.trim();
}

function panelBodyIsBroken() {
  const panel = document.getElementById("test-solver-panel");
  const body = panelBody || document.querySelector("#test-solver-body");
  if (!panel || !body) return false;
  if (localStorage.getItem("testSolverPanelCollapsed") === "1") return false;
  const bodyRect = body.getBoundingClientRect();
  const style = window.getComputedStyle(body);
  return style.display === "none" || style.visibility === "hidden" || bodyRect.height < 8 || !body.textContent.trim();
}

function recoverBrokenPanel() {
  if (!overlayVisible) return;
  if (!panelBodyIsBroken()) {
    emptyPanelSince = 0;
    return;
  }
  const now = Date.now();
  if (!emptyPanelSince) {
    emptyPanelSince = now;
    return;
  }
  if (now - emptyPanelSince >= EMPTY_PANEL_RESET_MS) {
    document.getElementById("test-solver-panel")?.remove();
    panelBody = null;
    emptyPanelSince = 0;
    lastPayloadKey = "";
    ensurePanel();
  }
}

function panelHasFinalAnswerState() {
  const body = panelBody || document.querySelector("#test-solver-body");
  if (!body || !body.textContent.trim()) return false;
  const text = body.textContent.trim();
  return !["Поиск ответа...", "Ищу ответ..."].includes(text);
}

async function readEnabledState() {
  if (!chrome?.storage?.local) return true;
  const state = await chrome.storage.local.get({ enabled: true });
  return state.enabled !== false;
}

async function readOverlayVisibleState() {
  if (!chrome?.storage?.local) return true;
  const state = await chrome.storage.local.get({ overlayVisible: true });
  return state.overlayVisible !== false;
}

async function readAutoSelectState() {
  if (!chrome?.storage?.local) return { autoSelectEnabled: false, manualAnswerText: "" };
  const state = await chrome.storage.local.get({ autoSelectEnabled: false, manualAnswerText: "" });
  return {
    autoSelectEnabled: state.autoSelectEnabled === true,
    manualAnswerText: String(state.manualAnswerText || "").trim(),
  };
}

async function readBackendBaseUrl() {
  if (!chrome?.storage?.local) return DEFAULT_BACKEND_BASE_URL;
  const state = await chrome.storage.local.get({ backendBaseUrl: DEFAULT_BACKEND_BASE_URL });
  return normalizeBackendBaseUrl(state.backendBaseUrl || DEFAULT_BACKEND_BASE_URL);
}

function normalizeBackendBaseUrl(value) {
  return String(value || DEFAULT_BACKEND_BASE_URL).trim().replace(/\/+$/, "");
}

async function setEnabledState(enabled) {
  solverEnabled = enabled;
  if (!enabled) {
    removePanel();
    lastPayloadKey = "";
    return;
  }
  await tick();
}

function enablePanelControls(panel) {
  const header = panel.querySelector("#test-solver-header");
  const toggle = panel.querySelector("#test-solver-toggle");
  const body = panel.querySelector("#test-solver-body");

  toggle.addEventListener("click", (event) => {
    event.stopPropagation();
    const collapsed = body.style.display !== "none";
    body.style.display = collapsed ? "none" : "block";
    toggle.textContent = collapsed ? "+" : "−";
    localStorage.setItem("testSolverPanelCollapsed", collapsed ? "1" : "0");
  });

  let dragState = null;
  header.addEventListener("mousedown", (event) => {
    if (event.target === toggle) return;
    const rect = panel.getBoundingClientRect();
    dragState = { dx: event.clientX - rect.left, dy: event.clientY - rect.top };
    event.preventDefault();
  });
  window.addEventListener("mousemove", (event) => {
    if (!dragState) return;
    const left = Math.max(0, Math.min(window.innerWidth - panel.offsetWidth, event.clientX - dragState.dx));
    const top = Math.max(0, Math.min(window.innerHeight - panel.offsetHeight, event.clientY - dragState.dy));
    panel.style.left = `${left}px`;
    panel.style.top = `${top}px`;
    panel.style.right = "auto";
    panel.style.bottom = "auto";
  });
  window.addEventListener("mouseup", () => {
    if (!dragState) return;
    const rect = panel.getBoundingClientRect();
    localStorage.setItem("testSolverPanelPosition", JSON.stringify({ left: Math.round(rect.left), top: Math.round(rect.top) }));
    dragState = null;
  });
  window.addEventListener("resize", () => clampPanelToViewport(panel));
}

function render(results, error = "") {
  renderResults({ results, error });
}

function renderStats(response) {
  if (!overlayVisible) return;
  const panel = ensurePanel();
  const body = panelBody || panel.querySelector("#test-solver-body");
  const stats = response?.stats || {};
  lastBackendVersion = response?.backendVersion || lastBackendVersion;
  body.innerHTML = `
    <div style="padding-top:4px;border-top:1px solid #374151">
      <div style="color:#93c5fd;font-weight:bold">Статистика базы</div>
      <div style="margin-top:8px;color:#cbd5e1">Всего вопросов: ${escapeHtml(stats.total || 0)}</div>
      <div style="color:#86efac">С подтверждённым ответом: ${escapeHtml(stats.answered || 0)}</div>
      <div style="color:#cbd5e1">Из Moodle review: ${escapeHtml(stats.moodleReview || 0)}</div>
      <div style="color:#fbbf24">Требуют проверки: ${escapeHtml(stats.needsReview || 0)}</div>
      <div style="color:#cbd5e1">Сохранённых неверных ответов: ${escapeHtml(stats.wrongAnswers || 0)}</div>
      <div style="margin-top:8px;color:#9ca3af;font-size:12px;word-break:break-all">CSV: ${escapeHtml(response?.csv || "")}</div>
      <div style="margin-top:10px;color:#6b7280;font-size:11px">${versionLine()}</div>
    </div>
  `;
}

function renderLoading(message = "Ищу ответ...") {
  if (!overlayVisible) return;
  const panel = ensurePanel();
  const body = panelBody || panel.querySelector("#test-solver-body");
  if (localStorage.getItem("testSolverPanelCollapsed") !== "1") {
    body.style.display = "block";
    panel.querySelector("#test-solver-toggle").textContent = "−";
  }
  body.innerHTML = statusHtml(message);
}

function statusHtml(message) {
  return `<div style="color:#cbd5e1">${escapeHtml(message)}</div><div style="margin-top:6px;color:#6b7280;font-size:11px">${versionLine()}</div>`;
}

function fallbackResultsFromQuestions(questions, status = "needs_review") {
  return questions.map((question) => ({
    ...question,
    answers: [],
    answerIndexes: [],
    confidence: 0,
    status,
    source: "",
  }));
}

function optionIndexForAnswer(options, answer) {
  const normalizedAnswer = normalizeOption(answer).toLowerCase();
  return (options || []).findIndex((option) => normalizeOption(option).toLowerCase() === normalizedAnswer);
}

function sanitizeChoiceResult(result) {
  if (!["single_choice", "multiple_choice"].includes(result?.kind) || !Array.isArray(result.options) || !result.options.length) {
    return result;
  }

  const selectedIndexes = [];
  const selectedAnswers = [];
  for (const index of Array.isArray(result.answerIndexes) ? result.answerIndexes : []) {
    if (!Number.isInteger(index) || index < 0 || index >= result.options.length || selectedIndexes.includes(index)) continue;
    selectedIndexes.push(index);
    selectedAnswers.push(result.options[index]);
  }
  for (const answer of result.answers || []) {
    const index = optionIndexForAnswer(result.options, answer);
    if (index < 0 || selectedIndexes.includes(index)) continue;
    selectedIndexes.push(index);
    selectedAnswers.push(result.options[index]);
  }

  if (result.kind === "single_choice" && selectedAnswers.length > 1) {
    return { ...result, answers: [selectedAnswers[0]], answerIndexes: [selectedIndexes[0]] };
  }
  return { ...result, answers: selectedAnswers, answerIndexes: selectedIndexes };
}

function sanitizeResults(results) {
  return (results || []).map(sanitizeChoiceResult);
}

function findQuestionRootForResult(result, roots) {
  const wantedNumber = Number(result?.questionNumber || 0);
  if (wantedNumber) {
    const byNumber = roots.find((root) => questionNumberFrom(root) === wantedNumber);
    if (byNumber) return byNumber;
  }
  return roots.length === 1 ? roots[0] : null;
}

function answerCandidatesFromRoot(root) {
  const inputs = Array.from(root.querySelectorAll("input[type='radio'], input[type='checkbox']"));
  return inputs.map((input) => {
    const label = input.closest("label") || (input.id ? document.querySelector(`label[for="${CSS.escape(input.id)}"]`) : null);
    const container =
      input.closest(".answer .r0, .answer .r1, .answer li, .answer .option, .answer .form-check, li, .option, .form-check, label") ||
      input.parentElement;
    const target = label || container || input;
    const text = optionTextForInput(input);
    return { input, target, text, normalizedText: normalizeOption(text) };
  }).filter((candidate) => (visible(candidate.target) || visible(candidate.input)) && !isNonAnswerOption(candidate.normalizedText));
}

function selectAnswerCandidate(candidate) {
  if (!candidate?.input) return false;
  if (candidate.input.checked) return true;
  if (candidate.target && candidate.target !== candidate.input) {
    candidate.target.click();
  }
  if (!candidate.input.checked) {
    candidate.input.click();
  }
  if (!candidate.input.checked) {
    candidate.input.checked = true;
  }
  candidate.input.dispatchEvent(new Event("input", { bubbles: true }));
  candidate.input.dispatchEvent(new Event("change", { bubbles: true }));
  return candidate.input.checked;
}

function clickAnswerByIndexInRoot(root, optionIndex) {
  const index = Number(optionIndex);
  if (!Number.isInteger(index) || index < 0) return false;
  const candidate = answerCandidatesFromRoot(root)[index];
  return selectAnswerCandidate(candidate);
}

function clickAnswerInRoot(root, answer) {
  const expected = String(answer || "").trim();
  if (!expected) return false;
  const normalizedExpected = normalizeOption(expected);
  const numberedMatch = expected.match(/^\s*(\d+)[.)]?\s+/);
  if (numberedMatch && clickAnswerByIndexInRoot(root, Number(numberedMatch[1]) - 1)) {
    return true;
  }

  for (const candidate of answerCandidatesFromRoot(root)) {
    const candidateText = candidate.text.trim();
    const exactMatch = candidateText === expected || candidate.normalizedText === normalizedExpected;
    const containsMatch =
      normalizedExpected.length >= 8 &&
      (candidate.normalizedText.includes(normalizedExpected) || normalizedExpected.includes(candidate.normalizedText));
    if (!exactMatch && !containsMatch) continue;
    return selectAnswerCandidate(candidate);
  }

  return false;
}

function selectResultAnswersInRoot(root, result, allowManualAnswer = true) {
  if (!root || !result) return false;
  const candidates = answerCandidatesFromRoot(root);
  if (!candidates.length) return false;

  const manualMatchesCurrentQuestion =
    allowManualAnswer &&
    manualAnswerText &&
    candidates.some((candidate) => candidate.normalizedText === normalizeOption(manualAnswerText));
  const answers = manualMatchesCurrentQuestion ? [manualAnswerText] : result.answers || [];
  let selected = false;

  answers.forEach((answer, answerIndex) => {
    const optionIndex =
      !manualMatchesCurrentQuestion && Array.isArray(result.answerIndexes) && typeof result.answerIndexes[answerIndex] === "number"
        ? result.answerIndexes[answerIndex]
        : null;
    if (optionIndex !== null && selectAnswerCandidate(candidates[optionIndex])) {
      selected = true;
      return;
    }
    if (clickAnswerInRoot(root, answer)) {
      selected = true;
    }
  });

  return selected;
}

function autoSelectAnswers(results) {
  if (!autoSelectEnabled || isReviewPage()) return;
  const roots = Array.from(document.querySelectorAll(".que")).filter(visible);
  const questionRoots = roots.length ? roots : [document.querySelector("form") || document.body];

  for (const result of results || []) {
    const root = findQuestionRootForResult(result, questionRoots);
    if (!root) continue;
    if (selectResultAnswersInRoot(root, result, true)) {
      break;
    }
  }
}

function selectAnswersForClickedQuestion(target) {
  if (!solverEnabled || isReviewPage()) return false;
  const root = target.closest(".que") || document.querySelector(".que") || document.querySelector("form") || document.body;
  const result = (latestRenderState.results || []).find((item) => findQuestionRootForResult(item, [root]) === root);
  if (!result) return false;
  return selectResultAnswersInRoot(root, result, true);
}

async function syncManualAnswerTextFromResults(results) {
  const roots = Array.from(document.querySelectorAll(".que")).filter(visible);
  const questionRoots = roots.length ? roots : [document.querySelector("form") || document.body];
  let currentResult = null;
  for (const result of results || []) {
    if (!result?.answers?.length) continue;
    if (findQuestionRootForResult(result, questionRoots)) {
      currentResult = result;
      break;
    }
  }
  if (!currentResult) {
    currentResult = (results || []).find((result) => result?.answers?.length) || null;
  }
  const currentAnswerText = currentResult?.answers?.[0] ? String(currentResult.answers[0]).trim() : "";
  if (manualAnswerText === currentAnswerText) return;
  manualAnswerText = currentAnswerText;
  if (chrome?.storage?.local) {
    await chrome.storage.local.set({ manualAnswerText: currentAnswerText });
  }
}

function renderResults({ results = [], error = "", summary = null, reviewMode = false }) {
  const safeResults = reviewMode ? results : sanitizeResults(results);
  latestRenderState = { results: safeResults, error, summary, reviewMode };
  if (!overlayVisible) {
    removePanel();
    return;
  }
  const panel = ensurePanel();
  const body = panelBody || panel.querySelector("#test-solver-body");
  if (error) {
    body.innerHTML = `<div style="color:#fca5a5">${escapeHtml(error)}</div><div style="margin-top:6px;color:#6b7280;font-size:11px">${versionLine()}</div>`;
    return;
  }

  if (!safeResults.length) {
    body.innerHTML = statusHtml("Вопросы Moodle не найдены.");
    return;
  }

  const sortedResults = [...safeResults].sort((left, right) => {
    const leftNumber = Number(left.questionNumber || 0);
    const rightNumber = Number(right.questionNumber || 0);
    if (leftNumber && rightNumber) return leftNumber - rightNumber;
    return 0;
  });
  const canCollapseList = reviewMode || Boolean(summary);
  const effectiveListCollapsed = canCollapseList && listCollapsed;
  const summaryHtml = summary
    ? `
      <div style="padding:8px 10px;background:#1f2937;border:1px solid #374151;border-radius:8px;margin-bottom:10px">
        <div style="color:#86efac">Правильные ответы сохранены: ${escapeHtml(summary.saved)}</div>
        <div style="color:#cbd5e1">Всего найдено: ${escapeHtml(summary.total)} · Верно: ${escapeHtml(summary.correct)} · Неверно: ${escapeHtml(summary.incorrect)} · Частично: ${escapeHtml(summary.partial)}</div>
        ${summary.parseErrors ? `<div style="color:#fbbf24">Без правильного ответа на странице: ${escapeHtml(summary.parseErrors)}</div>` : ""}
        ${summary.csv ? `<div style="color:#9ca3af;font-size:12px;word-break:break-all">CSV: ${escapeHtml(summary.csv)}</div>` : ""}
        <button id="test-solver-list-toggle" type="button" style="margin-top:8px;cursor:pointer;border:1px solid #4b5563;background:#111827;color:#f9fafb;border-radius:6px;padding:4px 8px">
          ${effectiveListCollapsed ? "Показать список" : "Свернуть список"}
        </button>
      </div>
    `
    : "";
  const listStyle = reviewMode ? "max-height:55vh;overflow-y:auto;padding-right:6px" : "max-height:45vh;overflow-y:auto;padding-right:6px";
  body.innerHTML = [
    summaryHtml,
    `<div id="test-solver-list" style="${listStyle};${effectiveListCollapsed ? "display:none" : ""}">`,
    ...sortedResults.map((result, index) => {
      const answers = result.answers?.length
        ? result.answers
            .map((answer, answerIndex) => {
              const optionIndex =
                Array.isArray(result.answerIndexes) && typeof result.answerIndexes[answerIndex] === "number"
                  ? result.answerIndexes[answerIndex] + 1
                  : findOptionNumber(result.options || [], answer);
              const prefix = optionIndex ? `${optionIndex}. ` : "";
              return `<li>${escapeHtml(prefix)}${escapeHtml(answer)}</li>`;
            })
            .join("")
        : "<li>Ответ не найден: needs_review</li>";
      const source =
        result.source
          ? `<div style="color:#9ca3af">Источник: ${escapeHtml(result.source)}</div>`
          : "";
      const dbBadge = result.fromDatabase || result.answerOrigin === "db" || (Array.isArray(result.pipeline) && result.pipeline.includes("db"))
        ? `<div style="color:#86efac;font-size:12px">Ответ взят из локальной БД.</div>`
        : "";
      const webTrace = result.webAttempted
        ? `<div style="color:#9ca3af;font-size:12px">Web-поиск: ${escapeHtml(result.webStatus || "проверялся")}${result.webCached ? " · cache" : ""}${result.webSource ? ` (${escapeHtml(result.webSource)}, ${Math.round((result.webConfidence || 0) * 100)}%)` : ""}${result.webError ? ` · ${escapeHtml(result.webError)}` : ""}${typeof result.webDurationMs === "number" ? ` · ${Math.round(result.webDurationMs)} мс` : ""}</div>`
        : "";
      const sourceNote =
        result.source === "web-search" || result.source === "web-search-v2"
          ? `<div style="color:#fbbf24;font-size:12px">Найдено через веб-поиск, проверьте источник вручную.</div>`
          : result.source === "best-effort"
          ? `<div style="color:#fbbf24;font-size:12px">Автоматическая гипотеза без подтверждённого источника. Проверьте вручную.</div>`
          : "";
      const pipeline = Array.isArray(result.pipeline) && result.pipeline.length
        ? `<div style="color:#6b7280;font-size:11px">Pipeline: ${escapeHtml(result.pipeline.join(" → "))}</div>`
        : "";
      const evidence =
        result.excerpt && ["web-search", "web-search-v2"].includes(result.source)
          ? `<details style="margin-top:6px;color:#cbd5e1;font-size:12px"><summary style="cursor:pointer;color:#93c5fd">Показать источник</summary><div style="margin-top:4px;white-space:pre-wrap;word-break:break-word">${escapeHtml(result.excerpt).slice(0, 1200)}</div></details>`
          : "";
      const confidence = typeof result.confidence === "number" ? Math.round(result.confidence * 100) : 0;
      const displayNumber = result.questionNumber || index + 1;
      const reviewBadge = result.reviewStatus
        ? `<span style="color:${reviewColor(result.reviewStatus)}"> · ${escapeHtml(reviewLabel(result.reviewStatus))}</span>`
        : "";
      return `
        <div style="margin-top:10px;padding-top:10px;border-top:1px solid #374151">
          <div style="color:#93c5fd">Вопрос ${escapeHtml(displayNumber)}: ${escapeHtml(result.status || "")}${reviewBadge}</div>
          <div style="margin-top:4px">${escapeHtml(result.text || "")}</div>
          <ul style="margin:8px 0 0 18px;padding:0">${answers}</ul>
          <div style="color:#9ca3af">Уверенность: ${confidence}%</div>
          ${dbBadge}
          ${source}
          ${webTrace}
          ${sourceNote}
          ${pipeline}
          ${evidence}
        </div>
      `;
    }),
    `<div style="margin-top:10px;color:#6b7280;font-size:11px">${versionLine()}</div>`,
    "</div>",
  ].join("");

  const listToggle = body.querySelector("#test-solver-list-toggle");
  if (listToggle) {
    listToggle.addEventListener("click", () => {
      listCollapsed = !listCollapsed;
      localStorage.setItem("testSolverListCollapsed", listCollapsed ? "1" : "0");
      renderResults({ results: safeResults, summary, reviewMode });
    });
  }
  requestAnimationFrame(() => clampPanelToViewport(panel));
}

function versionLine() {
  const backend = lastBackendVersion ? ` · backend v${escapeHtml(lastBackendVersion)}` : "";
  return `content v${escapeHtml(CONTENT_VERSION)}${backend}`;
}

function reviewLabel(status) {
  if (status === "correct") return "верно";
  if (status === "incorrect") return "неверно";
  if (status === "partial") return "частично";
  return "статус неизвестен";
}

function findOptionNumber(options, answer) {
  const normalizedAnswer = normalizeOption(answer).toLowerCase();
  const index = options.findIndex((option) => normalizeOption(option).toLowerCase() === normalizedAnswer);
  return index >= 0 ? index + 1 : null;
}

function reviewColor(status) {
  if (status === "correct") return "#86efac";
  if (status === "incorrect") return "#fca5a5";
  if (status === "partial") return "#fbbf24";
  return "#cbd5e1";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function tick() {
  recoverBrokenPanel();
  if (!solverEnabled) {
    removePanel();
    return;
  }

  if (isStatsPage()) {
    const statsKey = `stats:${location.href}`;
    if (statsKey === lastPayloadKey && panelHasFinalAnswerState()) return;
    lastPayloadKey = statsKey;
    try {
      renderLoading("Загружаю статистику базы...");
      const response = await sendStatsRequest();
      renderStats(response);
    } catch (error) {
      render([], `Не удалось загрузить статистику базы. Backend: ${error.message}`);
    }
    return;
  }

  if (isReviewPage()) {
    const correctQuestions = readCorrectQuestions();
    const correctKey = `correct:${JSON.stringify(correctQuestions)}`;
    if (correctKey === lastPayloadKey) return;
    lastPayloadKey = correctKey;
    if (!correctQuestions.length) {
      render([]);
      return;
    }
    try {
      const response = await sendCorrectQuestions(correctQuestions);
      lastBackendVersion = response.backendVersion || "";
      renderResults({
        results: response.results || [],
        summary: {
          saved: response.saved || 0,
          total: response.total || correctQuestions.length,
          correct: response.correct || 0,
          incorrect: response.incorrect || 0,
          partial: response.partial || 0,
          parseErrors: response.errors || 0,
          csv: response.csv || "",
        },
        reviewMode: true,
      });
      return;
    } catch (error) {
      render([], `Не удалось сохранить правильные ответы. Backend: ${error.message}`);
      return;
    }
  }

  const questions = readQuestions();
  const payloadKey = JSON.stringify(questions);
  if (requestInFlight) return;
  if (payloadKey === lastPayloadKey && panelHasFinalAnswerState()) return;
  if (panelContentIsEmpty()) lastPayloadKey = "";
  lastPayloadKey = payloadKey;

  if (!questions.length) {
    renderLoading("Вопрос не найден.");
    return;
  }

  try {
    requestInFlight = true;
    renderLoading("Отправляю в backend...");
    const response = await sendQuestions(questions);
    lastBackendVersion = response.backendVersion || "";
    renderLoading("Ответ получен.");
    const results = response.results || [];
    const renderedResults = sanitizeResults(results.length ? results : fallbackResultsFromQuestions(questions));
    await syncManualAnswerTextFromResults(renderedResults);
    render(renderedResults);
    autoSelectAnswers(renderedResults);
  } catch (error) {
    render([], `Локальный backend недоступен. Запусти: test-solver serve. Детали: ${error.message}`);
  } finally {
    requestInFlight = false;
  }
}

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === "solver-enabled-changed") {
    setEnabledState(message.enabled !== false);
  }
  if (message?.type === "solver-backend-changed") {
    lastPayloadKey = "";
    if (solverEnabled) tick();
  }
  if (message?.type === "solver-overlay-changed") {
    overlayVisible = message.overlayVisible !== false;
    if (!overlayVisible) {
      removePanel();
    } else {
      renderResults(latestRenderState);
    }
  }
  if (message?.type === "solver-autoselect-changed") {
    autoSelectEnabled = message.autoSelectEnabled === true;
    manualAnswerText = String(message.manualAnswerText || "").trim();
    autoSelectAnswers(latestRenderState.results || []);
  }
  if (message?.type === "solver-reset-panel") {
    localStorage.removeItem("testSolverPanelCollapsed");
    localStorage.removeItem("testSolverPanelPosition");
    localStorage.removeItem("testSolverListCollapsed");
    lastPayloadKey = "";
    requestInFlight = false;
    emptyPanelSince = 0;
    removePanel();
    if (solverEnabled && overlayVisible) tick();
  }
});

document.addEventListener(
  "dblclick",
  (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (!target.closest(".qtext, .formulation, .que")) return;
    if (selectAnswersForClickedQuestion(target)) {
      event.preventDefault();
      event.stopPropagation();
    }
  },
  true
);

Promise.all([readEnabledState(), readOverlayVisibleState(), readAutoSelectState()]).then(([enabled, visibleState, autoSelectState]) => {
  solverEnabled = enabled;
  overlayVisible = visibleState;
  autoSelectEnabled = autoSelectState.autoSelectEnabled;
  manualAnswerText = autoSelectState.manualAnswerText;
  if (solverEnabled) tick();
});
setInterval(tick, POLL_MS);
