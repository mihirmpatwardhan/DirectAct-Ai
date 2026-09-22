import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app.services.llm_service import llm_service
from app.services.task_router import task_router, TaskType
from app.services.app_discovery import app_discovery
from app.services.os_engine import os_engine, _desktop_ui_snapshot
from app.services.security_guard import malware_guard, GuardContext
from app.schemas.action_vocabulary import LaunchApp, OpenURL

async def run_tests():
    print("=== DirectAct-AI Generalization & Hardcode Removal Verification ===")
    
    # 1. Test Task Router - Generalization (any website, any app, data-independent)
    print("\n--- Testing Task Router Generalization ---")
    web_tests = [
        "open https://example.org/dashboard",
        "go to mycompany.internal.net and view analytics",
        "browse soundcloud.com and search for lo-fi beats",
        "play ambient study music video",
    ]
    for wt in web_tests:
        dec = task_router.classify(wt)
        assert dec.task_type == TaskType.WEB, f"Expected WEB for '{wt}', got {dec.task_type}"
        print(f" [PASS] WEB: '{wt}' -> {dec.extracted_intent} (url={dec.parameters.get('url')})")

    desktop_tests = [
        "open notepad and write notes",
        "launch calculator to compute sum",
        "start paint and draw a diagram",
        "run obs64 for screen recording",
    ]
    for dt in desktop_tests:
        dec = task_router.classify(dt)
        assert dec.task_type == TaskType.DESKTOP, f"Expected DESKTOP for '{dt}', got {dec.task_type}"
        print(f" [PASS] DESKTOP: '{dt}' -> {dec.extracted_intent} (app={dec.parameters.get('app_name')})")

    # 2. Test App Discovery (Dynamic resolution, no hardcoding)
    print("\n--- Testing Dynamic App Discovery ---")
    apps = app_discovery.get_all()
    print(f" [INFO] Dynamically discovered {len(apps)} apps on system")
    assert len(apps) > 0, "Expected to find installed applications"
    
    notepad_app = app_discovery.resolve("notepad")
    assert notepad_app is not None, "Expected to dynamically resolve notepad"
    print(f" [PASS] Resolved notepad: {notepad_app.name} (exe={notepad_app.executable_path})")

    calc_app = app_discovery.resolve("calculator")
    assert calc_app is not None, "Expected to dynamically resolve calculator"
    print(f" [PASS] Resolved calculator: {calc_app.name}")

    # 3. Test Security Guard (Allows valid apps, blocks truly destructive commands)
    print("\n--- Testing Security Guard ---")
    ctx = GuardContext(session_id="test_sess", action_id="test_act", task_id="test_task", execution_mode="autonomous")
    
    # Valid app launch
    res = await malware_guard.validate(LaunchApp(app_id="notepad", description="Open Notepad"), ctx)
    assert res.allowed, "Valid app launch should be allowed"
    print(" [PASS] LaunchApp(notepad) allowed by security guard")

    # Valid URL
    res_url = malware_guard.scan_url("https://github.com/trending")
    assert res_url.allowed, "Valid URL should be allowed"
    print(" [PASS] scan_url(github.com) allowed")

    # Destructive command
    ctx_destruct = GuardContext(session_id="test_sess", action_id="test_act", task_id="test_task", execution_mode="autonomous", raw_input="format c: /q")
    res_destruct = await malware_guard.validate(LaunchApp(app_id="cmd", description="format c: /q"), ctx_destruct)
    assert not res_destruct.allowed, "format C: must be blocked"
    print(" [PASS] 'format c:' correctly blocked by security guard")

    # 4. Test LLM Service complete_json and resilience
    print("\n--- Testing LLM Service complete_json ---")
    llm_res = await llm_service.complete_json(
        'Respond with JSON: {"status": "ok", "app": "any_dynamic_app", "general": true}'
    )
    print(f" [INFO] LLM complete_json response: {llm_res}")
    assert isinstance(llm_res, dict) and len(llm_res) > 0, "Expected non-empty dict response from complete_json"
    print(" [PASS] LLM complete_json verified working!")

    # 5. Test Desktop UI Snapshot helper
    print("\n--- Testing Desktop UI Snapshot Helper ---")
    snap = _desktop_ui_snapshot()
    print(f" [PASS] Snapshot taken successfully (length: {len(snap)})")

    print("\n=======================================================")
    print(" ALL VERIFICATION TESTS PASSED SUCCESSFULLY!")
    print("=======================================================")

if __name__ == "__main__":
    asyncio.run(run_tests())
