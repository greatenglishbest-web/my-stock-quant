# -*- coding: utf-8 -*-
import streamlit as st
import pandas as pd
from datetime import datetime
import os
import glob
import subprocess
import sys
import re

# 页面基础配置
st.set_page_config(page_title="A股量化多策略集成终端", layout="wide")

st.title("A股量化多策略自动化控制中心")
st.markdown("---")

# 自动扫描当前文件夹下的所有 .py 脚本文件
current_dir = os.path.dirname(os.path.abspath(__file__))
py_files = glob.glob(os.path.join(current_dir, "*.py"))
script_names = [os.path.basename(f) for f in py_files if os.path.basename(f).lower() != "app.py"]
script_names.sort()

# 侧边栏：策略自动发现中心
st.sidebar.header("策略自动发现中心")
if not script_names:
    st.sidebar.warning("当前文件夹下未发现其他 Python 脚本！")
    selected_script = None
else:
    selected_script = st.sidebar.selectbox("请选择要运行的量化脚本：", script_names)

trade_date = st.sidebar.text_input("交易日期 (YYYYMMDD)", value=datetime.now().strftime('%Y%m%d'))
run_button = st.sidebar.button("一键运行所选脚本")

# 主界面显示区
st.subheader(f"当前选中脚本：【 {selected_script or '无'} 】")
st.info(f"目标交易日：{trade_date} | 脚本所在目录：{current_dir}")

def clean_ansi(text):
    """清除终端的 ANSI 颜色和样式控制代码，让网页显示干净的文字"""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

if run_button and selected_script:
    script_path = os.path.join(current_dir, selected_script)
    st.write(f"正在后台调用并执行脚本：`{selected_script}`，目标日期: `{trade_date}` ...")
    
    log_placeholder = st.empty()
    log_placeholder.info("策略正在后台高速计算中（多线程下载与计算通常需要 1~2 分钟，请耐心等待）...")
    
    try:
        process = subprocess.Popen(
            [sys.executable, script_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=current_dir
        )
        
        stdout_bytes, stderr_bytes = process.communicate(input=f"{trade_date}\n".encode('gbk'), timeout=900)
        
        stdout_data = stdout_bytes.decode('gbk', errors='ignore')
        stderr_data = stderr_bytes.decode('gbk', errors='ignore')
        
        # 清除颜色代码
        clean_stdout = clean_ansi(stdout_data)
        clean_stderr = clean_ansi(stderr_data)
        
        if process.returncode == 0:
            log_placeholder.success("脚本执行成功！")
            st.markdown("### 📊 详细运行日志与选股结果：")
            # 展示清洗掉颜色代码后的干净日志
            st.text(clean_stdout if clean_stdout else "脚本执行完毕，未输出文本日志。")
        else:
            log_placeholder.error(f"脚本运行出错 (返回码 {process.returncode})：")
            st.text(clean_stderr)
                
    except subprocess.TimeoutExpired:
        log_placeholder.error("运行超时：脚本执行时间超过了 15 分钟。")
    except Exception as e:
        log_placeholder.error(f"启动脚本失败，错误原因：{e}")