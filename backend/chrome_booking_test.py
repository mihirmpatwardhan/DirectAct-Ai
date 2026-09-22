"""
Chrome Live-Bridge Booking Test
================================
Books a Mirzapur movie ticket on BookMyShow using the user actual open Chrome (DirectAct-AI extension).
Usage:
    cd backend && python chrome_booking_test.py
"""
import asyncio, sys, logging, time, json
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("chrome_booking_test")

async def wait_for_bridge(timeout=90.0):
    from app.services.live_chrome_bridge import live_chrome_bridge
    print(f"\n Waiting for Chrome extension (up to {int(timeout)}s)...")
    print("   Ensure Chrome is open + DirectAct-AI extension loaded (chrome://extensions/ -> Load unpacked -> chrome-extension/)\n")
    t0 = time.time()
    dots = 0
    while time.time() - t0 < timeout:
        if live_chrome_bridge.connected:
            print(f"\n  Chrome extension connected! v={live_chrome_bridge._extension_version} caps={sorted(live_chrome_bridge._capabilities)}")
            return True
        await asyncio.sleep(1.0)
        dots += 1
        print(f"\r  {'.' * (dots % 50)}", end="", flush=True)
    print(f"\n  Bridge not connected after {int(timeout)}s")
    return False

async def main():
    from app.services.live_chrome_bridge import live_chrome_bridge
    from app.services.web_engine import WebAutomationEngine

    print("\n" + "="*65)
    print("  Mirzapur Movie Ticket via Chrome Automation")
    print("="*65)

    ok = await wait_for_bridge(90.0)
    if not ok:
        print("\n  Cannot proceed. Load the extension and retry.")
        return

    print("\n Taking initial screenshot...")
    try:
        shot = await live_chrome_bridge.request("screenshot", timeout=10)
        if shot: print(f"  Screenshot ok len={len(shot.get('data',''))}")
        tab = await live_chrome_bridge.request("tab", timeout=5)
        if tab: print(f"  Current tab: {tab.get('url','?')}")
    except Exception as e:
        print(f"  Screenshot err: {e}")

    engine = WebAutomationEngine()
    task = (
        "Go to https://in.bookmyshow.com and search for the movie Mirzapur. "
        "Find available showtimes. Select a showtime. Start the booking process. "
        "STOP before any payment or OTP step."
    )
    print(f"\n Starting agent...\n  Task: {task[:100]}\n")
    t0 = time.time()
    result = await engine._run_live_chrome_agent_task(
        session_id="chrome-booking-01",
        task_query=task,
        start_url="https://in.bookmyshow.com",
        task_id="",
    )
    elapsed = time.time() - t0

    print("\n" + "="*65)
    print(f"  success : {result.get('success')}")
    print(f"  output  : {str(result.get('output',''))[:200]}")
    print(f"  url     : {result.get('url','')}")
    print(f"  steps   : {result.get('steps','?')}")
    print(f"  time    : {elapsed:.1f}s")
    if result.get("error"): print(f"  error   : {str(result.get('error'))[:200]}")
    steps = result.get("partial_progress") or []
    if steps:
        print("\n  History:")
        for s in steps: print(f"    {s}")
    print("="*65)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Stopped]")
