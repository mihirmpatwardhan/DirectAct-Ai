"""
Desktop Automation Live Test Suite
====================================
Tests all desktop automation layers:
  Phase 1: Broker primitive commands (ping, snapshot, launch, focus, type, keys)
  Phase 2: Full LLM-in-loop agent on Notepad task
  Phase 3: Calculator automation

Usage:
    cd backend
    python desktop_test.py
"""
import asyncio
import sys
import logging
import time

sys.path.insert(0, ".")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("desktop_test")


async def test_broker_primitives():
    from app.services.desktop_agent_client import desktop_client

    print("\n" + "="*60)
    print("  PHASE 1: Broker Primitive Commands")
    print("="*60)

    print("\n[1] Ping broker...")
    ok = await desktop_client.ping()
    print(f"    ping ok={ok}")
    assert ok, "Broker ping failed!"

    print("\n[2] Snapshot desktop...")
    snap = await desktop_client.snapshot()
    lines = snap.strip().splitlines()
    print(f"    snapshot has {len(lines)} lines")
    for ln in lines[:5]:
        print(f"      {ln}")

    print("\n[3] Screenshot desktop...")
    img = await desktop_client.screenshot()
    if img:
        print(f"    screenshot ok (base64 len={len(img)})")
    else:
        print("    screenshot returned None (PIL not installed?)")

    print("\n[4] Launch Notepad...")
    result = await desktop_client.launch("notepad")
    print(f"    launch ok={result.get(chr(39)+'ok'+chr(39))} msg={result.get(chr(39)+'msg'+chr(39))}")
    await asyncio.sleep(2.0)

    print("\n[5] Snapshot after Notepad launch...")
    snap2 = await desktop_client.snapshot()
    notepad_visible = "notepad" in snap2.lower()
    print(f"    Notepad in snapshot: {notepad_visible}")
    if not notepad_visible:
        for ln in snap2.splitlines()[:10]:
            print(f"      {ln}")

    print("\n[6] Focus Notepad...")
    result = await desktop_client.focus("Notepad")
    print(f"    focus ok={result.get(chr(39)+'ok'+chr(39))} msg={result.get(chr(39)+'msg'+chr(39))}")
    await asyncio.sleep(0.5)

    print("\n[7] Type text into Notepad...")
    result = await desktop_client.type_text("Notepad", "Hello from DirectAct-AI Desktop Agent!")
    print(f"    type ok={result.get(chr(39)+'ok'+chr(39))} msg={result.get(chr(39)+'msg'+chr(39))}")
    await asyncio.sleep(0.5)

    print("\n[8] Press Ctrl+A in Notepad...")
    result = await desktop_client.keys("Notepad", "^a")
    print(f"    keys ok={result.get(chr(39)+'ok'+chr(39))} msg={result.get(chr(39)+'msg'+chr(39))}")
    await asyncio.sleep(0.3)

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[9] Type timestamp: {ts}")
    result = await desktop_client.type_text("Notepad", f"DirectAct-AI Desktop Test -- {ts}")
    print(f"    type ok={result.get(chr(39)+'ok'+chr(39))} msg={result.get(chr(39)+'msg'+chr(39))}")
    await asyncio.sleep(0.5)

    print("\n[10] Wait 1s via broker...")
    result = await desktop_client.wait(1.0)
    print(f"    wait ok={result.get(chr(39)+'ok'+chr(39))} msg={result.get(chr(39)+'msg'+chr(39))}")

    print("\n[OK] Phase 1 complete\n")
    return True


async def test_llm_agent_notepad():
    from app.services.os_engine import os_engine

    print("="*60)
    print("  PHASE 2: LLM Agent Loop - Notepad Task")
    print("="*60)

    task = "Open Notepad and type: DirectAct-AI automation is working!"
    print(f"\nTask: {task}\n")

    result = await os_engine.run_desktop_agent(
        user_intent=task,
        session_id="test_session",
    )
    print(f"\nAgent Result:")
    print(f"  success  = {result.success}")
    print(f"  output   = {result.output}")
    print(f"  error    = {result.error}")
    print(f"  duration = {result.duration_ms:.0f}ms")

    if result.success:
        print("\n[OK] Phase 2 complete\n")
    else:
        print(f"\n[WARN] Phase 2 failure: {result.error}\n")
    return result.success


async def test_calculator():
    from app.services.os_engine import os_engine

    print("="*60)
    print("  PHASE 3: Calculator Automation")
    print("="*60)

    task = "Open Calculator app"
    print(f"\nTask: {task}\n")

    result = await os_engine.run_desktop_agent(
        user_intent=task,
        session_id="test_session",
    )
    print(f"\nAgent Result:")
    print(f"  success  = {result.success}")
    print(f"  output   = {result.output}")
    print(f"  error    = {result.error}")
    print(f"  duration = {result.duration_ms:.0f}ms")

    if result.success:
        print("\n[OK] Phase 3 complete\n")
    else:
        print(f"\n[WARN] Phase 3: {result.error}\n")
    return result.success


async def main():
    print("\n" + "="*60)
    print("  DirectAct-AI Desktop Automation Test Suite")
    print("="*60)

    results = {}

    try:
        results["broker_primitives"] = await test_broker_primitives()
    except Exception as e:
        print(f"\n[FAIL] Phase 1 exception: {e}")
        import traceback; traceback.print_exc()
        results["broker_primitives"] = False

    try:
        results["llm_agent_notepad"] = await test_llm_agent_notepad()
    except Exception as e:
        print(f"\n[FAIL] Phase 2 exception: {e}")
        import traceback; traceback.print_exc()
        results["llm_agent_notepad"] = False

    try:
        results["calculator"] = await test_calculator()
    except Exception as e:
        print(f"\n[FAIL] Phase 3 exception: {e}")
        import traceback; traceback.print_exc()
        results["calculator"] = False

    print("\n" + "="*60)
    print("  TEST RESULTS SUMMARY")
    print("="*60)
    all_passed = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}]  {name}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("  All tests passed! Desktop automation is fully working.")
    else:
        print("  Some tests failed - check logs above.")
    print("="*60 + "\n")

    from app.services.desktop_agent_client import desktop_client
    await desktop_client.close()


if __name__ == "__main__":
    asyncio.run(main())
