import streamlit as st
import subprocess
import os
import sys
import re
from datetime import datetime

# 页面全局配置
st.set_page_config(
    page_title="A股量化多策略自动化控制中心",
    page_icon="📈",
    layout="wide"
)

st.title("📈 A股量化多策略自动化控制中心")
st.markdown("---")

# ================= 侧边栏控制面板 =================
st.sidebar.title("策略自动发现中心")

current_dir = os.path.dirname(os.path.abspath(__file__))

# 正确的文件列表推导式
py_files = [f for f in os.listdir(current_dir) if f.endswith('.py') and f != os.path.basename(__file__)]
py_files.sort()

if not py_files:
    st.sidebar.warning("当前目录下未发现任何策略 .py 脚本")
    st.stop()

selected_script = st.sidebar.selectbox("请选择要运行的量化脚本:", py_files)
today_str = datetime.now().strftime("%Y%m%d")
trade_date = st.sidebar.text_input("交易日期 (YYYYMMDD)", value=today_str)
run_btn = st.sidebar.button("🚀 一键运行所选脚本", use_container_width=True, type="primary")

# ================= 主界面区域 =================
st.subheader(f"当前选中脚本：【 {selected_script} 】")
st.info(f"目标交易日: `{trade_date}` | 脚本所在目录: `{current_dir}`")

def clean_ansi(text):
    """强力清除所有终端颜色和样式控制代码，彻底杜绝乱码"""
    if not text:
        return ""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    cleaned = ansi_escape.sub('', text)
    cleaned = re.sub(r'\[[0-9;]*[mK]', '', cleaned)
    return cleaned

if run_btn:
    script_path = os.path.join(current_dir, selected_script)
    
    with st.status(f"正在后台执行 `{selected_script}`，请稍候...", expanded=True) as status:
        st.write("⏳ 正在唤醒 Python 进程并运行策略...")
        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"  # 强制无缓存，彻底防止死锁
            
            process = subprocess.Popen(
                [sys.executable, script_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=current_dir,
                env=env
            )
            
            # 自动把交易日期喂给脚本
            input_payload = f"{trade_date}\n\n\n"
            raw_output, _ = process.communicate(input=input_payload)
            
            # 关键：在这里把所有的颜色乱码彻底洗净！
            clean_output = clean_ansi(raw_output)
            
            if process.returncode == 0:
                status.update(label="✅ 策略执行成功！", state="complete", expanded=False)
            else:
                status.update(label=f"⚠️ 脚本执行结束 (返回码 {process.returncode})", state="error", expanded=True)
                
        except Exception as e:
            status.update(label="❌ 运行发生异常", state="error", expanded=True)
            clean_output = str(e)

    # 完美、无损、无乱码地展示终端日志输出
    st.subheader("📊 策略运行结果与终端日志（已自动过滤颜色乱码）：")
    st.code(clean_output, language="text")
