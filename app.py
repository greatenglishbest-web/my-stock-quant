import streamlit as st
import subprocess
import os
import sys
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

if run_btn:
    script_path = os.path.join(current_dir, selected_script)
    
    with st.status(f"正在后台执行 `{selected_script}`，请稍候...", expanded=True) as status:
        st.write("⏳ 正在调用 Python 运行环境...")
        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            
            # 使用无超时的稳定执行方式
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
            
            # 自动喂入日期并回车
            input_payload = f"{trade_date}\n\n\n"
            raw_output, _ = process.communicate(input=input_payload)
            
            if process.returncode == 0:
                status.update(label="✅ 脚本运行成功！", state="complete", expanded=False)
            else:
                status.update(label=f"⚠️ 脚本执行结束 (返回码 {process.returncode})", state="error", expanded=True)
                
        except Exception as e:
            status.update(label="❌ 运行发生异常", state="error", expanded=True)
            raw_output = str(e)

    # 直接原汁原味展示终端输出日志
    st.subheader("📊 运行结果与终端日志：")
    st.code(raw_output, language="text")
