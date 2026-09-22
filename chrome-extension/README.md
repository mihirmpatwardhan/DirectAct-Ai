# DirectAct-AI live Chrome bridge

This extension is the live-account mode for DirectAct-AI. It uses Chrome's
official `chrome.debugger` API to operate the active tab in the user's normal
Chrome profile. It does not read passwords, cookies, or profile files.

One-time setup:

1. Keep DirectAct-AI running locally.
2. Open `chrome://extensions` in the normal signed-in Chrome window.
3. Turn on **Developer mode**, choose **Load unpacked**, and select this
   `chrome-extension` directory.
4. Accept Chrome's debugger permission warning for this extension.
5. Keep that Chrome window open. The extension will connect automatically.

After setup, check `http://127.0.0.1:8000/api/v1/chrome/bridge-status`. The
local app must report `connected: true` before it can execute live-account
browser actions.
