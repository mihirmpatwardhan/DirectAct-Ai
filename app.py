import os
import sys
import json
import tempfile
import subprocess
import time
import streamlit as st
from dotenv import load_dotenv

try:
    import psutil
except ImportError:
    psutil = None

load_dotenv()

TEMP_DIR = tempfile.gettempdir()
RUN_META_PATH = os.path.join(TEMP_DIR, "lakshya_run_meta.json")
RUN_OUT_PATH = os.path.join(TEMP_DIR, "lakshya_run.out.log")
RUN_ERR_PATH = os.path.join(TEMP_DIR, "lakshya_run.err.log")
MAX_LOG_CHARS = 14000

chrome_profile_name = (os.getenv("CHROME_PROFILE_NAME") or "Default").strip()
browser_label = f"Google Chrome ({chrome_profile_name})"


def load_run_meta() -> dict:
    if not os.path.exists(RUN_META_PATH):
        return {}
    try:
        with open(RUN_META_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_run_meta(meta: dict) -> None:
    with open(RUN_META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def read_log(path: str) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return f"Could not read log: {type(e).__name__}: {e}"

    if len(content) <= MAX_LOG_CHARS:
        return content
    return content[-MAX_LOG_CHARS:]


def is_process_running(pid: int | None) -> bool:
    if not pid or psutil is None:
        return False
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def stop_process(pid: int | None) -> bool:
    if not pid or psutil is None:
        return False
    try:
        process = psutil.Process(pid)
        process.terminate()
        try:
            process.wait(timeout=5)
        except psutil.TimeoutExpired:
            process.kill()
        return True
    except Exception:
        return False


def derive_status(meta: dict, stdout_text: str, stderr_text: str) -> str:
    pid = meta.get("pid")
    combined = f"{stdout_text}\n{stderr_text}"
    if is_process_running(pid):
        return "Running"
    if "Task finished successfully." in combined or "PC Desktop Automation Pipeline finished" in combined:
        return "Completed"
    if "FATAL ERROR" in combined or "ERROR:" in combined or "Traceback" in combined:
        return "Failed"
    if pid:
        return "Stopped"
    return "Idle"


def status_class_name(status: str) -> str:
    if status == "Completed":
        return "status-success"
    if status == "Failed":
        return "status-error"
    return "status-info"


def launch_task(task: str, mode: str) -> tuple[bool, str]:
    existing_meta = load_run_meta()
    existing_pid = existing_meta.get("pid")
    if is_process_running(existing_pid):
        stop_process(existing_pid)
        time.sleep(1)

    task_file = os.path.join(TEMP_DIR, f"lakshya_task_{int(time.time())}.json")
    with open(task_file, "w", encoding="utf-8") as f:
        json.dump({"task": task, "mode": mode}, f, ensure_ascii=False, indent=2)

    for path in (RUN_OUT_PATH, RUN_ERR_PATH):
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

    cmd = [sys.executable, "-u", "main.py", task_file]
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0

    with open(RUN_OUT_PATH, "w", encoding="utf-8", buffering=1) as out_file, open(
        RUN_ERR_PATH, "w", encoding="utf-8", buffering=1
    ) as err_file:
        process = subprocess.Popen(
            cmd,
            cwd=os.getcwd(),
            stdout=out_file,
            stderr=err_file,
            creationflags=creationflags,
        )

    meta = {
        "pid": process.pid,
        "task": task,
        "mode": mode,
        "task_file": task_file,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stdout_path": RUN_OUT_PATH,
        "stderr_path": RUN_ERR_PATH,
    }
    save_run_meta(meta)
    return True, f"Automation Process spawned with PID {process.pid}."


st.set_page_config(
    page_title="Lakshya AI Portal",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
html, body, [class*="css"] {
    background: linear-gradient(135deg, #07111f 0%, #0b172a 40%, #0d1b2e 100%);
    color: #ffffff;
    font-family: "Segoe UI", sans-serif;
}
.stApp {
    background:
        radial-gradient(circle at top left, rgba(0,255,255,0.06), transparent 25%),
        radial-gradient(circle at bottom right, rgba(59,130,246,0.10), transparent 30%),
        linear-gradient(135deg, #07111f 0%, #0b172a 40%, #0d1b2e 100%);
}
.block-container {
    padding-top: 2rem;
    padding-bottom: 2rem;
    max-width: 1250px;
}
#MainMenu, footer, header {
    visibility: hidden;
}
.hero-wrap {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    margin-bottom: 2rem;
}
.hero-card {
    width: 100%;
    padding: 28px 28px 18px 28px;
    border-radius: 26px;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.08);
    box-shadow: 0 0 40px rgba(0,0,0,0.20);
    backdrop-filter: blur(8px);
}
.hero-title {
    text-align: center;
    font-size: 3.2rem;
    font-weight: 800;
    letter-spacing: 0.4px;
    margin-top: 0.6rem;
    margin-bottom: 0.2rem;
    color: #ffffff;
}
.hero-sub {
    text-align: center;
    font-size: 1.08rem;
    color: #b7c7d8;
    margin-bottom: 0.2rem;
}
.input-card {
    padding: 24px;
    border-radius: 24px;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.08);
    box-shadow: 0 0 35px rgba(0,0,0,0.16);
    backdrop-filter: blur(8px);
}
.card-title {
    font-size: 1.45rem;
    font-weight: 700;
    margin-bottom: 0.35rem;
    color: #ffffff;
}
.card-sub {
    font-size: 0.98rem;
    color: #aac0d5;
    margin-bottom: 1rem;
}
.stTextArea textarea {
    background: rgba(7, 19, 35, 0.95) !important;
    color: #ffffff !important;
    border-radius: 18px !important;
    border: 1px solid rgba(0,255,255,0.12) !important;
    min-height: 140px !important;
    padding: 18px !important;
    font-size: 1.05rem !important;
}
.stSelectbox div[data-baseweb="select"] {
    background: rgba(7, 19, 35, 0.95) !important;
    color: #ffffff !important;
    border-radius: 14px !important;
    border: 1px solid rgba(0,255,255,0.12) !important;
}
.stButton > button {
    width: 100%;
    height: 56px;
    border: none;
    border-radius: 16px;
    font-size: 1.05rem;
    font-weight: 700;
    color: white;
    background: linear-gradient(90deg, #00c2ff 0%, #0078ff 45%, #21d4c5 100%);
    box-shadow: 0 10px 24px rgba(0, 138, 255, 0.22);
    transition: all 0.22s ease;
}
.stButton > button:hover {
    transform: translateY(-1px);
    box-shadow: 0 14px 28px rgba(0, 138, 255, 0.30);
}
.status-success {
    margin-top: 18px;
    padding: 16px 18px;
    border-radius: 16px;
    background: rgba(16, 185, 129, 0.12);
    border: 1px solid rgba(16, 185, 129, 0.25);
    color: #bbf7d0;
    font-size: 1rem;
    font-weight: 600;
}
.status-info {
    margin-top: 12px;
    padding: 15px 18px;
    border-radius: 16px;
    background: rgba(59, 130, 246, 0.12);
    border: 1px solid rgba(59, 130, 246, 0.25);
    color: #bfdbfe;
    font-size: 0.98rem;
}
.status-error {
    margin-top: 18px;
    padding: 16px 18px;
    border-radius: 16px;
    background: rgba(239, 68, 68, 0.12);
    border: 1px solid rgba(239, 68, 68, 0.25);
    color: #fecaca;
    font-size: 1rem;
    font-weight: 600;
}
.tip-box {
    padding: 18px;
    border-radius: 18px;
    background: rgba(255,255,255,0.035);
    border: 1px solid rgba(255,255,255,0.07);
    margin-top: 18px;
}
.tip-title {
    font-size: 1.08rem;
    font-weight: 700;
    margin-bottom: 0.6rem;
    color: #ffffff;
}
.tip-item {
    color: #cbd5e1;
    margin-bottom: 0.4rem;
    font-size: 0.97rem;
}
</style>
""",
    unsafe_allow_html=True,
)

run_meta = load_run_meta()
stdout_text = read_log(RUN_OUT_PATH)
stderr_text = read_log(RUN_ERR_PATH)
current_status = derive_status(run_meta, stdout_text, stderr_text)

if "task_input" not in st.session_state:
    st.session_state["task_input"] = run_meta.get("task", "")

st.markdown('<div class="hero-wrap"><div class="hero-card">', unsafe_allow_html=True)
st.markdown('<div class="hero-title">🚀 Lakshya AI Hub</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="hero-sub">Turning Human Goals into Autonomous Actions (Powered by Gemini Engine)</div>',
    unsafe_allow_html=True,
)
st.markdown('</div></div>', unsafe_allow_html=True)

left, right = st.columns([1.65, 1.0], gap="large")

with left:
    st.markdown('<div class="input-card">', unsafe_allow_html=True)
    st.markdown('<div class="card-title">🤖 Select Automation Mode</div>', unsafe_allow_html=True)
    
    if "automation_mode" not in st.session_state:
        st.session_state["automation_mode"] = "PC Local App Automation"

    selected_mode = st.selectbox(
        "Automation Mode",
        options=["Web Browser Automation", "PC Local App Automation"],
        label_visibility="collapsed",
        key="automation_mode"
    )

    mode_key = "pc" if selected_mode == "PC Local App Automation" else "web"
    
    st.markdown('<div class="card-title" style="margin-top:15px;">🎯 Enter Automation Intent</div>', unsafe_allow_html=True)
    
    placeholder_text = (
        "Example: Open Amazon and search for books"
        if mode_key == "web" else
        "Example: close the already open vs code app"
    )

    task = st.text_area(
        "Task",
        placeholder=placeholder_text,
        label_visibility="collapsed",
        height=120,
        key="task_input"
    )

    if st.button("🚀 Launch Execution Stream", use_container_width=True):
        if task.strip():
            ok, message = launch_task(task.strip(), mode_key)
            if ok:
                st.session_state["launch_message"] = f"{message} Mode: {mode_key.upper()}"
                st.rerun()
            else:
                st.markdown(f'<div class="status-error">❌ Error: {message}</div>', unsafe_allow_html=True)
        else:
            st.warning("⚠️ Please specify a command query first.")

    launch_message = st.session_state.pop("launch_message", None)
    if launch_message:
        st.markdown(
            f'<div class="status-success">✅ Pipeline launched successfully. {launch_message} Logs updating below.</div>',
            unsafe_allow_html=True
        )

    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="tip-box">', unsafe_allow_html=True)
    st.markdown('<div class="tip-title">📊 Live Pipeline Monitor</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="{status_class_name(current_status)}">Current Status: {current_status} (Mode: {run_meta.get("mode","N/A").upper()})</div>', unsafe_allow_html=True)

    monitor_left, monitor_right = st.columns(2)
    with monitor_left:
        if st.button("🔄 Refresh Monitor Logs", use_container_width=True):
            st.rerun()
    with monitor_right:
        if st.button("⛔ Force Halt Current Process", use_container_width=True):
            if stop_process(run_meta.get("pid")):
                run_meta["stopped_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                save_run_meta(run_meta)
                st.rerun()
            else:
                st.warning("⚠️ No running subprocess runtime stack detected.")

    combined_log = ""
    if stdout_text:
        combined_log += "--- STDOUT STREAM ---\n" + stdout_text.strip()
    if stderr_text:
        if combined_log:
            combined_log += "\n\n"
        combined_log += "--- STDERR STREAM ---\n" + stderr_text.strip()

    if combined_log:
        st.code(combined_log, language="text")
    else:
        st.info("ℹ️ No active pipeline runtime capture arrays found.")
    st.markdown('</div>', unsafe_allow_html=True)

with right:
    st.markdown('<div class="tip-box">', unsafe_allow_html=True)
    if mode_key == "web":
        st.markdown('<div class="tip-title">💡 Web Sample Intents</div>', unsafe_allow_html=True)
        st.markdown('<div class="tip-item">• Open Amazon and search for laptop under 50000</div>', unsafe_allow_html=True)
        st.markdown('<div class="tip-item">• Open Google and search for weather</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="tip-title">💡 PC Local App Intents</div>', unsafe_allow_html=True)
        st.markdown('<div class="tip-item">• close the already open vs code app</div>', unsafe_allow_html=True)
        st.markdown('<div class="tip-item">• open spotify and play linkin park</div>', unsafe_allow_html=True)
        st.markdown('<div class="tip-item">• open notepad and write hello</div>', unsafe_allow_html=True)
        st.markdown('<div class="tip-item">• open vs code, create folder AI_Pro on Desktop, write hello world program</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="tip-box">', unsafe_allow_html=True)
    st.markdown('<div class="tip-title">⚙️ System Configurations</div>', unsafe_allow_html=True)
    st.markdown('<div class="feature-box">', unsafe_allow_html=True)
    st.markdown(f'<div class="tip-item">🧠 LLM Engine: Google Gemini 2.5</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="tip-item">🗂️ Profile Mapping: {browser_label}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="tip-item">📁 Mode: {selected_mode}</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)