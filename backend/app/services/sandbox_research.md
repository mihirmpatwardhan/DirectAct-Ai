# Ephemeral Sandbox Isolation Feasibility Research & Engineering Thesis

**System:** DirectAct-AI (Define.Direct.Done)  
**Module:** Active Cybersecurity Guard (`app.services.security_guard`)

---

## 1. Executive Summary

Browser-based and desktop automation agents encounter untrusted third-party content (malicious websites, drive-by downloads, compromised browser extensions, and untrusted executable binaries). To mitigate zero-day risks, process tampering, and credential exfiltration, **DirectAct-AI** implements active security interception layered with isolated execution environments.

This paper evaluates three architectural approaches for ephemeral sandbox containment.

---

## 2. Sandbox Isolation Approaches

### Approach 1: Ephemeral Playwright User Profile Isolation (Active Default)
- **Mechanism:** Each automation session launches Chromium with a dynamically generated profile path (`--user-data-dir=./logs/browser_profiles/{session_id}`).
- **Key Flags:** `--disable-blink-features=AutomationControlled`, `--disable-extensions-except=`, `--no-first-run`, `--disable-default-apps`.
- **Security Characteristics:**
  - Prevents cross-session cookie/session leakage.
  - Guarantees clean state upon session start.
  - Automatically deleted or quarantined upon session termination.
- **Performance:** Sub-100ms cold start latency. High efficiency.

### Approach 2: Ephemeral Windows Sandbox VM (`.wsb`)
- **Mechanism:** Utilizing Windows 10/11 Pro built-in Hyper-V container technology (`WindowsSandbox.exe`).
- **Workflow:**
  1. Generate a transient `.wsb` XML configuration file mapping host download directories read-only.
  2. Launch untrusted scripts/files inside the hardware-isolated sandbox VM.
  3. Automatically discard VM state upon closing.
- **Security Characteristics:**
  - Hardware-enforced kernel isolation.
  - Complete protection against host disk or registry compromise.
- **Performance Cost:** ~3–6 seconds cold-start latency. Recommended for high-risk binary execution.

### Approach 3: WSL2 / Ephemeral Docker Container Isolation
- **Mechanism:** `docker run --rm --network none playwright-sandbox`
- **Security Characteristics:**
  - Complete POSIX/Linux process and network namespace isolation.
  - Filesystem overlay discarded automatically (`--rm`).
- **Performance Cost:** ~1 second container launch overhead.

---

## 3. Recommended Multi-Tier Architecture

DirectAct-AI deploys a **Risk-Proportional Isolation Pipeline**:
1. **Low / None Threat:** Ephemeral Profile Isolation (Approach 1).
2. **Medium / High Threat (Downloaded Binaries/Scripts):** File Checksum (SHA-256) + VirusTotal API scanning + Human Approval Gate.
3. **Critical Threat:** Windows Sandbox VM (Approach 2) isolated execution container.
