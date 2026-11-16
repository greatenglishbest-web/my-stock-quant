import streamlit as st
import subprocess
import os
import re
import sys
from datetime import datetime

# 页面全局配置
st.set_page_config(
    page_title="A股量化多策略自动化控制中心",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# 注入全局高级科技感 CSS 与动态入场动画
st.markdown("""
<style>
/* 卡片逐个滑入淡出动画 */
@keyframes slideInUp {
    0% {
        opacity: 0;
        transform: translateY(25px) scale(0.98);
    }
    100% {
        opacity: 1;
        transform: translateY(0) scale(1);
    }
}

/* 涨停/大涨脉冲红光呼吸动画 */
@keyframes pulseRed {
    0% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.5); }
    70% { box-shadow: 0 0 0 10px rgba(239, 68, 68, 0); }
    100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0); }
}

.quant-card {
    background: linear-gradient(145deg, #1e293b 0%, #0f172a 100%);
    border: 1px solid #334155;
    border-radius: 12px;
    padding: 18px 20px;
    margin-bottom: 16px;
    opacity: 0;
    animation: slideInUp 0.5s cubic-bezier(0.16, 1, 0.3, 1) forwards;
    transition: all 0.25s ease;
}

.quant-card:hover {
    transform: translateY(-4px);
    border-color: #60a5fa;
}

/* 高亮红框卡片（针对大涨/涨停股） */
.quant-card-hot {
    background: linear-gradient(145deg, #2a1215 0%, #0f172a 100%);
    border: 1.5px solid #ef4444;
    animation: slideInUp 0.5s cubic-bezier(0.16, 1, 0.3, 1) forwards, pulseRed 2s infinite;
}

.card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid rgba(255, 255, 255, 0.1);
    padding-bottom: 10px;
    margin-bottom: 14px;
    flex-wrap: wrap;
    gap: 8px;
}

.stock-name {
    font-size: 20px;
    font-weight: 800;
    color: #f8fafc;
    letter-spacing: 0.5px;
}

.stock-code {
    font-size: 13px;
    color: #94a3b8;
    background: rgba(255, 255, 255, 0.08);
    padding: 2px 8px;
    border-radius: 4px;
    margin-left: 8px;
}

.price-box {
    display: flex;
    align-items: center;
    gap: 10px;
}

.stock-price {
    font-size: 19px;
    font-weight: 700;
    color: #38bdf8;
    font-family: 'Courier New', monospace;
}

.pct-hot {
    background: linear-gradient(90deg, #dc2626, #ef4444);
    color: #ffffff;
    font-weight: 800;
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 15px;
    box-shadow: 0 2px 8px rgba(239, 68, 68, 0.4);
}

.pct-up {
    background: rgba(239, 68, 68, 0.18);
    color: #f87171;
    border: 1px solid rgba(239, 68, 68, 0.4);
    font-weight: 700;
    padding: 3px 10px;
    border-radius: 6px;
    font-size: 15px;
}

.pct-down {
    background: rgba(16, 185, 129, 0.18);
    color: #34d399;
    border: 1px solid rgba(16, 185, 129, 0.4);
    font-weight: 700;
    padding: 3px 10px;
    border-radius: 6px;
    font-size: 15px;
}

.metrics-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(135px, 1fr));
    gap: 10px;
}

.metric-chip {
    background: rgba(15, 23, 42, 0.7);
    border: 1px solid rgba(51, 65, 85, 0.7);
    border-radius: 8px;
    padding: 8px 12px;
    display: flex;
    flex-direction: column;
}

.metric-label {
    font-size: 11px;
    color: #94a3b8;
    margin-bottom: 2px;
}

.metric-value {
    font-size: 14px;
    font-weight: 700;
    color: #e2e8f0;
    font-family: 'Consolas', monospace;
}

.metric-value-highlight {
    color: #fb7185;
}
</style>
""", unsafe_allow_html=True)

def clean_ansi(text):
    """清除终端颜色控制符"""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

def parse_stocks_from_log(log_text):
    """
    通用智能解析引擎：从任意选股脚本的输出日志中提取股票、价格、涨幅及所有动态指标
    """
    lines = log_text.splitlines()
    stocks = []
    current_stock = None
    
    # 匹配行首包含序号与股票代码的行，例如: 1. 688507.SH 索辰科技 ...
    stock_header_pattern = re.compile(r'^\s*(\d+)[\.、\s]+(\d{6}\.[A-Z]{2})\s+([^\s]+)\s+(.*)$')
    # 匹配所有的 "指标名:数值" 键值对，例如 价: 171.16  涨:+20.00%  昨BIAS:-1.42
    kv_pattern = re.compile(r'([^\s:：]+)\s*[:：]\s*([+-]?\d+(?:\.\d+)?%?)')

    for line in lines:
        line_clean = line.strip()
        if not line_clean:
            continue
            
        match = stock_header_pattern.match(line_clean)
        if match:
            if current_stock:
                stocks.append(current_stock)
            idx, code, name, rest_of_line = match.groups()
            current_stock = {
                "id": int(idx),
                "code": code,
                "name": name,
                "price": "--",
                "pct": "0.00%",
                "pct_num": 0.0,
                "metrics": []
            }
            # 提取同一行里的指标
            for k, v in kv_pattern.findall(rest_of_line):
                if k in ["价", "现价", "收盘", "最新价"]:
                    current_stock["price"] = v
                elif k in ["涨", "涨幅", "涨跌幅"]:
                    current_stock["pct"] = v
                    try:
                        current_stock["pct_num"] = float(v.replace("+", "").replace("%", ""))
                    except:
                        pass
                else:
                    current_stock["metrics"].append((k, v))
        elif current_stock:
            # 如果是紧跟在股票下方的指标行（缩进行），继续提取里面的所有指标
            kvs = kv_pattern.findall(line_clean)
            if kvs:
                for k, v in kvs:
                    if k in ["价", "现价", "收盘", "最新价"] and current_stock["price"] == "--":
                        current_stock["price"] = v
                    elif k in ["涨", "涨幅", "涨跌幅"] and current_stock["pct"] == "0.00%":
                        current_stock["pct"] = v
                        try:
                            current_stock["pct_num"] = float(v.replace("+", "").replace("%", ""))
                        except:
                            pass
                    else:
                        current_stock["metrics"].append((k, v))
            else:
                # 遇到分割线或非指标段落，结束当前股票收集
                if line_clean.startswith("=") or line_clean.startswith("-") or line_clean.startswith("【"):
                    stocks.append(current_stock)
                    current_stock = None

    if current_stock:
        stocks.append(current_stock)
    return stocks

def render_dynamic_cards(stocks):
    """渲染高能动态卡片看板"""
    st.markdown(f"### 🚀 策略精选战报（共命中 **{len(stocks)}** 只标的）")
    
    # 顶部核心统计栏
    if stocks:
        max_stock = max(stocks, key=lambda x: x["pct_num"])
        hot_count = sum(1 for s in stocks if s["pct_num"] >= 9.5)
        c1, c2, c3 = st.columns(3)
        c1.metric("命中标的数", f"{len(stocks)} 只")
        c2.metric("大涨/涨停股 (≥9.5%)", f"{hot_count} 只", delta="高能共振" if hot_count > 0 else None)
        c3.metric("领涨龙头", f"{max_stock['name']}", delta=max_stock['pct'])

    st.markdown("---")

    for i, s in enumerate(stocks):
        is_hot = s["pct_num"] >= 9.5
        card_class = "quant-card quant-card-hot" if is_hot else "quant-card"
        
        if s["pct_num"] >= 9.5:
            pct_class = "pct-hot"
            fire_icon = "🔥 "
        elif s["pct_num"] > 0:
            pct_class = "pct-up"
            fire_icon = ""
        else:
            pct_class = "pct-down"
            fire_icon = ""

        # 构建所有动态指标磁贴 HTML
        metrics_html = ""
        for label, val in s["metrics"]:
            # 对关键指标（如斜率、量比）做高亮处理
            val_class = "metric-value metric-value-highlight" if ("斜率" in label or "量比" in label) else "metric-value"
            metrics_html += f"""
            <div class="metric-chip">
                <span class="metric-label">{label}</span>
                <span class="{val_class}">{val}</span>
            </div>
            """

        delay = min(i * 0.08, 1.2)  # 阶梯式延迟入场，产生波浪动画效果
        card_html = f"""
        <div class="{card_class}" style="animation-delay: {delay}s;">
            <div class="card-header">
                <div>
                    <span class="stock-name">{fire_icon}{s['id']}. {s['name']}</span>
                    <span class="stock-code">{s['code']}</span>
                </div>
                <div class="price-box">
                    <span class="stock-price">¥{s['price']}</span>
                    <span class="{pct_class}">{s['pct']}</span>
                </div>
            </div>
            <div class="metrics-grid">
                {metrics_html}
            </div>
        </div>
        """
        st.markdown(card_html, unsafe_allow_html=True)

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
st.title("A股量化多策略自动化控制中心")
st.markdown("---")
st.subheader(f"当前选中脚本：【 {selected_script} 】")
st.info(f"目标交易日: `{trade_date}` | 脚本所在目录: `{current_dir}`")

if run_btn:
    script_path = os.path.join(current_dir, selected_script)
    
    with st.status(f"正在后台高速运行 `{selected_script}`，请稍候...", expanded=True) as status:
        st.write("⏳ 正在初始化环境并传递交易日期参数...")
        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            
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
            
            # 自动喂入日期并连续发送回车跳过结尾暂停
            input_payload = f"{trade_date}\n\n\n"
            raw_output, _ = process.communicate(input=input_payload, timeout=300)
            clean_output = clean_ansi(raw_output)
            
            if process.returncode == 0:
                status.update(label="✅ 策略执行完毕！正在生成高能动态战报...", state="complete", expanded=False)
            else:
                status.update(label=f"⚠️ 脚本执行结束 (返回码 {process.returncode})", state="error", expanded=True)
                
        except subprocess.TimeoutExpired:
            process.kill()
            status.update(label="❌ 运行超时（超过5分钟已自动终止）", state="error", expanded=True)
            clean_output = "运行超时，请检查网络连接或数据接口状态。"
        except Exception as e:
            status.update(label="❌ 调用发生异常", state="error", expanded=True)
            clean_output = str(e)

    # 1. 智能解析并渲染动态炫酷卡片
    parsed_stocks = parse_stocks_from_log(clean_output)
    if parsed_stocks:
        render_dynamic_cards(parsed_stocks)
    else:
        st.warning("本次运行未解析到符合条件的具体个股，或该策略输出格式暂未命中股票列表。请展开下方日志查看详情。")

    # 2. 将原始运行日志收纳进可折叠面板，保持界面极简高级
    with st.expander("📄 查看原始终端运行日志（点击展开/收起）", expanded=not bool(parsed_stocks)):
        st.code(clean_output, language="text")
