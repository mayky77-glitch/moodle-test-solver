const DEFAULT_STATE = { enabled: true, overlayVisible: true, autoSelectEnabled: false, manualAnswerText: "" };

chrome.runtime.onInstalled.addListener(async () => {
  const state = await chrome.storage.local.get(DEFAULT_STATE);
  if (typeof state.enabled !== "boolean") {
    await chrome.storage.local.set({ enabled: DEFAULT_STATE.enabled });
  }
  if (typeof state.overlayVisible !== "boolean") {
    await chrome.storage.local.set({ overlayVisible: DEFAULT_STATE.overlayVisible });
  }
  if (typeof state.autoSelectEnabled !== "boolean") {
    await chrome.storage.local.set({ autoSelectEnabled: DEFAULT_STATE.autoSelectEnabled });
  }
  if (typeof state.manualAnswerText !== "string") {
    await chrome.storage.local.set({ manualAnswerText: DEFAULT_STATE.manualAnswerText });
  }
});

chrome.commands.onCommand.addListener(async (command) => {
  if (command !== "toggle-solver") return;
  const state = await chrome.storage.local.get(DEFAULT_STATE);
  const enabled = !state.enabled;
  await chrome.storage.local.set({ enabled });
  await notifyActiveTab(enabled);
});

async function notifyActiveTab(enabled) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return;
  chrome.tabs.sendMessage(tab.id, { type: "solver-enabled-changed", enabled }).catch(() => {});
}
