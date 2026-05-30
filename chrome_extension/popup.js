const POPUP_VERSION = "0.1.11-debug";
const DEFAULT_STATE = { enabled: true, backendBaseUrl: "http://127.0.0.1:8765", overlayVisible: true, autoSelectEnabled: false, manualAnswerText: "" };

async function readState() {
  return chrome.storage.local.get(DEFAULT_STATE);
}

async function writeState(enabled) {
  await chrome.storage.local.set({ enabled });
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id) {
    chrome.tabs.sendMessage(tab.id, { type: "solver-enabled-changed", enabled }).catch(() => {});
  }
  const state = await readState();
  render(enabled, state.backendBaseUrl, state.overlayVisible, state.autoSelectEnabled, state.manualAnswerText);
}

async function writeOverlayVisible(overlayVisible) {
  await chrome.storage.local.set({ overlayVisible });
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id) {
    chrome.tabs.sendMessage(tab.id, { type: "solver-overlay-changed", overlayVisible }).catch(() => {});
  }
  const state = await readState();
  render(state.enabled, state.backendBaseUrl, overlayVisible, state.autoSelectEnabled, state.manualAnswerText);
}

async function writeAutoSelectSettings() {
  const autoSelectEnabled = document.getElementById("autoSelectEnabled").checked;
  const manualAnswerText = document.getElementById("manualAnswerText").value.trim();
  await chrome.storage.local.set({ autoSelectEnabled, manualAnswerText });
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id) {
    chrome.tabs.sendMessage(tab.id, { type: "solver-autoselect-changed", autoSelectEnabled, manualAnswerText }).catch(() => {});
  }
  document.getElementById("backendStatus").textContent = autoSelectEnabled
    ? "Автовыбор включен."
    : "Автовыбор выключен.";
  const state = await readState();
  render(state.enabled, state.backendBaseUrl, state.overlayVisible, autoSelectEnabled, manualAnswerText);
}

async function writeBackendUrl(backendBaseUrl) {
  const normalizedUrl = normalizeBackendUrl(backendBaseUrl);
  await chrome.storage.local.set({ backendBaseUrl: normalizedUrl });
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id) {
    chrome.tabs.sendMessage(tab.id, { type: "solver-backend-changed", backendBaseUrl: normalizedUrl }).catch(() => {});
  }
  document.getElementById("backendStatus").textContent = `Сохранено: ${normalizedUrl}`;
}

async function resetPanel() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id) {
    chrome.tabs.sendMessage(tab.id, { type: "solver-reset-panel" }).catch(() => {});
  }
  document.getElementById("backendStatus").textContent = "Панель сброшена. Если версия в окне не изменилась, обнови расширение.";
}

function render(
  enabled,
  backendBaseUrl = DEFAULT_STATE.backendBaseUrl,
  overlayVisible = DEFAULT_STATE.overlayVisible,
  autoSelectEnabled = DEFAULT_STATE.autoSelectEnabled,
  manualAnswerText = DEFAULT_STATE.manualAnswerText
) {
  document.getElementById("status").textContent = enabled ? "Вкл" : "Выкл";
  document.getElementById("toggle").textContent = enabled ? "Выключить" : "Включить";
  document.getElementById("backendUrl").value = backendBaseUrl;
  document.getElementById("overlayVisible").checked = overlayVisible !== false;
  document.getElementById("autoSelectEnabled").checked = autoSelectEnabled === true;
  document.getElementById("manualAnswerText").value = manualAnswerText || "";
  document.getElementById("version").textContent = `popup v${POPUP_VERSION}`;
}

document.getElementById("toggle").addEventListener("click", async () => {
  const state = await readState();
  await writeState(!state.enabled);
});

document.getElementById("saveBackend").addEventListener("click", async () => {
  const input = document.getElementById("backendUrl");
  await writeBackendUrl(input.value);
});

document.getElementById("overlayVisible").addEventListener("change", async (event) => {
  await writeOverlayVisible(event.target.checked);
});

document.getElementById("resetPanel").addEventListener("click", resetPanel);
document.getElementById("autoSelectEnabled").addEventListener("change", writeAutoSelectSettings);
document.getElementById("saveManualAnswer").addEventListener("click", writeAutoSelectSettings);

function normalizeBackendUrl(value) {
  return String(value || DEFAULT_STATE.backendBaseUrl).trim().replace(/\/+$/, "");
}

readState().then((state) =>
  render(state.enabled, state.backendBaseUrl, state.overlayVisible, state.autoSelectEnabled, state.manualAnswerText)
);
