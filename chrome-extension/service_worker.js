/**
 * DirectAct-AI — Live Chrome Bridge Service Worker v1.3.0
 *
 * Key guarantees:
 *   1. The extension only controls a tab it created and marked as its own;
 *      existing user tabs are never selected as automation targets.
 *   2. Automation runs in a dedicated tab created in the background (active:false).
 *      The tab ID is persisted in chrome.storage.session across worker restarts.
 *   3. Popups, modal dialogs, cookie consent banners (e.g. "Continue", "Accept")
 *      are reliably detected, scored, and clicked via both DOM dispatch and
 *      CDP hardware mouse events across main frame and iframes.
 *   4. Commands are strictly serialized via commandQueue to eliminate race conditions.
 *   5. Native JS dialogs (alert / confirm / prompt) are auto-accepted via CDP.
 *   6. Auto-reloads automatically when backend detects an older version.
 */

const EXTENSION_VERSION = '1.3.0';

const BRIDGE_URLS = [
  'ws://127.0.0.1:8000/ws/chrome-bridge',
  'ws://localhost:8000/ws/chrome-bridge',
];

const AUTOMATION_TAB_KEY = 'directactAutomationTabId';
const AUTOMATION_TAB_OWNERSHIP_PREFIX = 'directactAutomationTab:';

let socket = null;
let bridgeUrlIndex = 0;
let commandQueue = Promise.resolve();
const attachedTabs = new Set();
let automationTabId = null;

// ── Tab protection ───────────────────────────────────────────────────────────

/**
 * Returns true when a tab cannot be controlled through CDP.  We deliberately
 * do not identify the dashboard by a title, hostname, or port: those rules
 * were brittle and could discard a valid automation tab on a local site.
 *
 * Instead, an automation target is defined solely by an ownership marker that
 * this extension writes when it creates the background tab.
 */
function isUnsafeAutomationTab(tab) {
  if (!tab) return false;
  const rawUrl = tab.url || tab.pendingUrl || '';

  try {
    const url = new URL(rawUrl);
    return ['chrome:', 'chrome-extension:', 'devtools:', 'edge:'].includes(url.protocol);
  } catch (_) {}

  return false;
}

function automationTabOwnershipKey(tabId) {
  return `${AUTOMATION_TAB_OWNERSHIP_PREFIX}${tabId}`;
}

async function isOwnedAutomationTab(tabId) {
  try {
    const key = automationTabOwnershipKey(tabId);
    const stored = await chrome.storage.session.get(key);
    return stored[key] === true;
  } catch (_) {
    return false;
  }
}

// ── Restore persisted automation tab ─────────────────────────────────────────

const automationTabReady = (async () => {
  try {
    const stored = await chrome.storage.session.get(AUTOMATION_TAB_KEY);
    const tabId = stored[AUTOMATION_TAB_KEY];
    if (Number.isInteger(tabId)) {
      const tab = await chrome.tabs.get(tabId);
      if (tab && !isUnsafeAutomationTab(tab)) {
        // v1.2 stored only the ID.  Migrate that extension-owned tab once so
        // an update does not unnecessarily open another automation tab.
        await rememberAutomationTab(tab.id);
        return;
      }
    }
  } catch (_) {}
  automationTabId = null;
})();

async function rememberAutomationTab(tabId) {
  automationTabId = tabId;
  try {
    await chrome.storage.session.set({
      [AUTOMATION_TAB_KEY]: tabId,
      [automationTabOwnershipKey(tabId)]: true,
    });
  } catch (_) {}
}

async function forgetAutomationTab(tabId = automationTabId) {
  if (tabId !== automationTabId) return;
  automationTabId = null;
  try {
    await chrome.storage.session.remove([
      AUTOMATION_TAB_KEY,
      automationTabOwnershipKey(tabId),
    ]);
  } catch (_) {}
}

async function createAutomationTab() {
  // Create automation tab in the background so the user's current tab stays in focus.
  const tab = await chrome.tabs.create({ url: 'about:blank', active: false });
  await rememberAutomationTab(tab.id);
  return tab;
}

async function getAutomationTab() {
  await automationTabReady;

  if (automationTabId !== null) {
    try {
      const tab = await chrome.tabs.get(automationTabId);
      if (tab && !isUnsafeAutomationTab(tab) && await isOwnedAutomationTab(tab.id)) {
        return tab;
      }
    } catch (_) {}
    await forgetAutomationTab();
  }

  return createAutomationTab();
}

// ── CDP helpers ──────────────────────────────────────────────────────────────

async function ensureAttached(tabId) {
  if (attachedTabs.has(tabId)) return;
  try {
    await chrome.debugger.attach({ tabId }, '1.3');
  } catch (error) {
    if (!String(error?.message || error).toLowerCase().includes('already attached')) throw error;
  }
  attachedTabs.add(tabId);
  try {
    await chrome.debugger.sendCommand({ tabId }, 'Runtime.enable');
    await chrome.debugger.sendCommand({ tabId }, 'Page.enable');
  } catch (_) {}
}

async function cdp(tabId, method, params = {}) {
  await ensureAttached(tabId);
  return chrome.debugger.sendCommand({ tabId }, method, params);
}

// Auto-accept native browser dialogs (alert, confirm, prompt)
chrome.debugger.onEvent.addListener(async (source, method) => {
  if (method === 'Page.javascriptDialogOpening') {
    try {
      // Never handle a dialog belonging to a normal user tab.  The bridge may
      // be attached to more than one tab across a worker restart, but only its
      // explicitly owned background tab is automation scope.
      if (!source?.tabId || !(await isOwnedAutomationTab(source.tabId))) return;
      await chrome.debugger.sendCommand(source, 'Page.handleJavaScriptDialog', { accept: true });
    } catch (_) {}
  }
});

async function evaluate(tabId, expression) {
  const response = await cdp(tabId, 'Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture: true,
  });
  if (response?.exceptionDetails) {
    const desc =
      response.exceptionDetails.exception?.description ||
      response.exceptionDetails.text ||
      'Page evaluation failed';
    throw new Error(desc);
  }
  return response?.result?.value;
}

function waitForTabLoad(tabId, timeoutMs = 15000) {
  return new Promise((resolve) => {
    let timer;
    const listener = (updatedTabId, changeInfo) => {
      if (updatedTabId === tabId && changeInfo.status === 'complete') {
        chrome.tabs.onUpdated.removeListener(listener);
        clearTimeout(timer);
        resolve();
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
    timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    }, timeoutMs);
  });
}

function frameIds(frameTree) {
  const ids = [];
  const visit = (node) => {
    if (!node?.frame?.id) return;
    ids.push(node.frame.id);
    for (const child of node.childFrames || []) visit(child);
  };
  visit(frameTree);
  return ids;
}

async function evaluateInFrame(tabId, frameId, expression) {
  try {
    const world = await cdp(tabId, 'Page.createIsolatedWorld', {
      frameId,
      worldName: 'directact-automation',
      grantUniversalAccess: true,
    });
    const response = await cdp(tabId, 'Runtime.evaluate', {
      contextId: world.executionContextId,
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    });
    if (response?.exceptionDetails) return null;
    return response?.result?.value ?? null;
  } catch (_) {
    return null;
  }
}

// ── Smart Element & Popup Interaction ────────────────────────────────────────

/**
 * Builds a JS expression that reliably identifies and optionally activates an element.
 * Handles:
 *  - Modal popups / cookie banners / consent screens (prioritizes buttons inside visible modals)
 *  - Shadow DOM traversal
 *  - Custom elements (<ytd-button-renderer>, <tp-yt-paper-button>, etc.)
 *  - Exact vs partial text ranking (prefers leaf buttons over container wrappers)
 *  - Never selects dialog containers or tabindex="-1" wrappers when clicking
 */
function interactionExpression(target, activate = false) {
  return `(() => {
    const raw = ${JSON.stringify(String(target || ''))};
    const needle = raw
      .replace(/^(?:click|press|tap)\\s+/i, '')
      .replace(/\\s+(?:button|btn|link|option)$/i, '')
      .trim()
      .toLowerCase();
    if (!needle) return null;

    const isVisible = (el) => {
      if (!el) return false;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return false;
      const s = window.getComputedStyle(el);
      return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
    };

    // Collect all roots including open shadow roots
    const roots = [document];
    const seenRoots = new Set(roots);
    for (let i = 0; i < roots.length; i++) {
      for (const el of roots[i].querySelectorAll('*')) {
        if (el.shadowRoot && !seenRoots.has(el.shadowRoot)) {
          roots.push(el.shadowRoot);
          seenRoots.add(el.shadowRoot);
        }
      }
    }

    // 1. Direct CSS selector match (if valid selector)
    let directMatch = null;
    for (const root of roots) {
      try {
        const found = root.querySelector(raw);
        if (found && isVisible(found)) {
          directMatch = found;
          break;
        }
      } catch (_) {}
    }

    const clickElement = (el) => {
      const clickable = el.closest('button,[role="button"],a,[role="link"]') || el;
      clickable.scrollIntoView({ behavior: 'auto', block: 'center', inline: 'center' });
      const rect = clickable.getBoundingClientRect();

      if (${activate ? 'true' : 'false'}) {
        clickable.focus?.();
        const evtOpts = { bubbles: true, cancelable: true, view: window, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 };
        clickable.dispatchEvent(new PointerEvent('pointerdown', evtOpts));
        clickable.dispatchEvent(new MouseEvent('mousedown', evtOpts));
        clickable.dispatchEvent(new PointerEvent('pointerup', evtOpts));
        clickable.dispatchEvent(new MouseEvent('mouseup', evtOpts));
        clickable.click();
      }

      return {
        text: (clickable.innerText || clickable.value || '').slice(0, 120),
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2,
        activated: ${activate ? 'true' : 'false'},
      };
    };

    if (directMatch) {
      return clickElement(directMatch);
    }

    // 2. Text / Label Matching
    // Check for visible modal dialogs or consent overlays
    const modalSelectors = [
      'dialog[open]', '[role="dialog"]', '[role="alertdialog"]', '[aria-modal="true"]',
      '.modal:not([style*="display: none"])', '.popup:not([style*="display: none"])',
      '[class*="modal" i]:not([style*="display: none"])',
      '[class*="dialog" i]:not([style*="display: none"])',
      '[class*="consent" i]:not([style*="display: none"])',
      '[class*="cookie" i]:not([style*="display: none"])',
      '[class*="banner" i]:not([style*="display: none"])',
      '[id*="consent" i]', '[id*="cookie" i]', 'ytd-consent-bump-v2-lightbox'
    ];
    const activeModals = [];
    for (const root of roots) {
      for (const m of root.querySelectorAll(modalSelectors.join(','))) {
        if (isVisible(m)) activeModals.push(m);
      }
    }

    // Selectors for interactive candidates — EXCLUDE containers
    const interactiveSelectors = 'button, [role="button"], a, [role="link"], input, [role="menuitem"], [role="option"], [role="tab"], select, textarea, [tabindex]:not([tabindex="-1"])';

    const textFor = (el) =>
      [
        el.innerText,
        el.value,
        el.getAttribute('aria-label'),
        el.getAttribute('title'),
        el.getAttribute('alt'),
        el.getAttribute('placeholder'),
        el.getAttribute('data-testid'),
      ]
        .filter(Boolean)
        .join(' ')
        .replace(/\\s+/g, ' ')
        .trim()
        .toLowerCase();

    const candidates = [];
    for (const root of roots) {
      for (const el of root.querySelectorAll(interactiveSelectors)) {
        if (!isVisible(el)) continue;

        // Skip modal dialog containers itself
        const tag = el.tagName.toLowerCase();
        if (tag === 'dialog' || el.getAttribute('role') === 'dialog' || el.getAttribute('role') === 'alertdialog') continue;

        const text = textFor(el);
        if (!text) continue;

        let score = 0;
        if (text === needle) {
          score = 1000 - text.length;
        } else if (text.startsWith(needle + ' ') || text.endsWith(' ' + needle)) {
          score = 600 - text.length;
        } else if (text.includes(' ' + needle + ' ')) {
          score = 400 - text.length;
        } else if (text.includes(needle)) {
          score = 200 - text.length;
        }

        if (score > 0) {
          // Tag bonuses
          if (tag === 'button' || tag === 'input' || tag === 'a') score += 150;
          if (el.getAttribute('role') === 'button') score += 100;
          // Priority bonus if inside a visible modal
          if (activeModals.some(m => m.contains(el))) score += 350;
          candidates.push({ el, text, score });
        }
      }
    }

    if (candidates.length === 0) return null;

    // Pick candidate with highest score
    candidates.sort((a, b) => b.score - a.score);
    return clickElement(candidates[0].el);
  })()`;
}

async function dispatchMouseClick(tabId, x, y) {
  const roundX = Math.round(x);
  const roundY = Math.round(y);
  // 1. Move mouse to target position to satisfy hit test and hover states
  await cdp(tabId, 'Input.dispatchMouseEvent', {
    type: 'mouseMoved', x: roundX, y: roundY,
  });
  // 2. Press mouse
  await cdp(tabId, 'Input.dispatchMouseEvent', {
    type: 'mousePressed', x: roundX, y: roundY, button: 'left', clickCount: 1,
  });
  // 3. Release mouse
  await cdp(tabId, 'Input.dispatchMouseEvent', {
    type: 'mouseReleased', x: roundX, y: roundY, button: 'left', clickCount: 1,
  });
}

/**
 * Click an element identified by text or CSS selector.
 * Evaluates and activates in page context, then follows up with CDP hardware click.
 */
async function interact(tab, payload) {
  if (payload.action !== 'click') throw new Error(`Unsupported interaction: ${payload.action}`);

  // 1. Try in main frame
  try {
    const match = await evaluate(tab.id, interactionExpression(payload.target, true));
    if (match && Number.isFinite(match.x) && Number.isFinite(match.y)) {
      // interactionExpression already dispatches the browser event sequence
      // and calls element.click().  A second CDP mouse click can submit a form
      // twice or click through a modal while it is disappearing.
      return { clicked: true, text: match.text, mechanism: 'dom' };
    }
  } catch (_) {}

  // 2. Try in child iframes (for iframe-based cookie banners or embeds)
  try {
    const frameTree = await cdp(tab.id, 'Page.getFrameTree');
    const ids = frameIds(frameTree.frameTree);
    const mainFrameId = frameTree.frameTree?.frame?.id;
    for (const frameId of ids) {
      if (frameId === mainFrameId) continue;
      const frameMatch = await evaluateInFrame(tab.id, frameId, interactionExpression(payload.target, true));
      if (frameMatch) {
        return { clicked: true, text: frameMatch.text, mechanism: 'iframe-dom' };
      }
    }
  } catch (_) {}

  return { clicked: false, text: '' };
}

// ── Command dispatcher ───────────────────────────────────────────────────────

async function handleCommand(command, payload) {
  let tab = await getAutomationTab();

  // Do not attempt to attach the debugger to a Chrome-internal page.  A fresh
  // owned background tab is safer than reusing any existing browser tab.
  if (isUnsafeAutomationTab(tab)) {
    console.warn('DirectAct Guard: Automation tab is not controllable. Creating a new background tab.');
    await forgetAutomationTab();
    tab = await createAutomationTab();
  }

  if (command === 'navigate') {
    const targetUrl = payload.url || 'about:blank';
    if (/^(?:chrome|chrome-extension|devtools|edge):/i.test(targetUrl)) {
      throw new Error('Navigation to browser-internal pages is not supported');
    }
    // Register the listener before navigation.  Registering it afterwards can
    // miss a fast load and needlessly block each command for the timeout.
    const loaded = waitForTabLoad(tab.id);
    await chrome.tabs.update(tab.id, { url: targetUrl, active: false });
    await loaded;
    return { tab_id: tab.id, url: targetUrl };
  }

  if (command === 'evaluate') return evaluate(tab.id, payload.expression);

  if (command === 'interact') return interact(tab, payload);

  if (command === 'click') {
    await dispatchMouseClick(tab.id, Number(payload.x || 0), Number(payload.y || 0));
    return { clicked: true, x: Number(payload.x || 0), y: Number(payload.y || 0) };
  }

  if (command === 'type') {
    await cdp(tab.id, 'Input.insertText', { text: String(payload.text || '') });
    return true;
  }

  if (command === 'screenshot') {
    const shot = await cdp(tab.id, 'Page.captureScreenshot', { format: 'jpeg', quality: 65 });
    return { data: shot.data, format: 'jpeg' };
  }

  if (command === 'tab') {
    const refreshed = await chrome.tabs.get(tab.id);
    return { tab_id: refreshed.id, url: refreshed.url, title: refreshed.title };
  }

  throw new Error(`Unsupported bridge command: ${command}`);
}

// ── WebSocket bridge connection ──────────────────────────────────────────────

function connect() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  const connection = new WebSocket(BRIDGE_URLS[bridgeUrlIndex]);
  socket = connection;

  connection.onopen = () => {
    connection.send(JSON.stringify({
      type: 'hello',
      version: EXTENSION_VERSION,
      capabilities: ['evaluate', 'navigate', 'screenshot', 'click', 'interact', 'reload'],
    }));
  };

  connection.onmessage = (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }

    if (message.type === 'reload') {
      console.log('Reload signal received from backend — reloading extension');
      chrome.runtime.reload();
      return;
    }

    if (message.type !== 'command') return;

    commandQueue = commandQueue
      .then(async () => {
        try {
          const result = await handleCommand(message.command, message.payload || {});
          connection.send(JSON.stringify({ request_id: message.request_id, ok: true, result }));
        } catch (error) {
          connection.send(JSON.stringify({
            request_id: message.request_id,
            ok: false,
            error: String(error?.message || error),
          }));
        }
      })
      .catch(() => {});
  };

  connection.onclose = () => {
    // A stale socket must never tear down a newer successful connection.
    if (socket !== connection) return;
    socket = null;
    bridgeUrlIndex = (bridgeUrlIndex + 1) % BRIDGE_URLS.length;
    setTimeout(connect, 1500);
  };
  connection.onerror = () => connection.close();
}

chrome.runtime.onInstalled.addListener(() => connect());
chrome.runtime.onStartup.addListener(() => connect());

setInterval(() => {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: 'ping' }));
  } else {
    connect();
  }
}, 20000);

// ── Cleanup listeners ────────────────────────────────────────────────────────

chrome.debugger.onDetach.addListener(({ tabId }) => attachedTabs.delete(tabId));

chrome.tabs.onRemoved.addListener((tabId) => {
  attachedTabs.delete(tabId);
  void forgetAutomationTab(tabId);
});

// ── Boot ─────────────────────────────────────────────────────────────────────
connect();
