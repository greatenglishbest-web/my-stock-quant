# -*- coding: utf-8 -*-
"""
BBIBOLL首穿 + SKDJ首金 + 周线动能护航 + MACD过滤 + 严控高开 (终极排雷版)
"""
import tushare as ts
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import os
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import time
import random
import glob
import traceback

warnings.filterwarnings('ignore')

RED = '\033[91m'
YELLOW = '\033[93m'
GREEN = '\033[92m'
CYAN = '\033[96m'
BOLD = '\033[1m'
RESET = '\033[0m'

if os.name == 'nt':
    os.system('')
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 0x0007 | 0x0004)
    except:
        pass

class GlobalConfig:
    TUSHARE_TOKEN = 'a91652800def39d213abec3af9563e4485e83940bfbbd8276f8adaa8'  # 替换为你的有效Token
    OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'SKDJ首金_选股结果')
    CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股日线公共缓存')
    LOOKBACK_DAYS = 500
    USE_PARQUET = True

class BBIBOLLSKDJFilter:
    def __init__(self, token):
        self.pro = ts.pro_api(token)
        self.rt_data_dict = {}
        self._rate_limited = False
        self.errors = [] # 记录异常信息
        os.makedirs(GlobalConfig.CACHE_DIR, exist_ok=True)
        os.makedirs(GlobalConfig.OUTPUT_DIR, exist_ok=True)
        self.use_parquet = GlobalConfig.USE_PARQUET
        if self.use_parquet:
            try:
                import pyarrow  # noqa
            except ImportError:
                try:
                    import fastparquet  # noqa
                except ImportError:
                    self.use_parquet = False
                    print(f"{YELLOW}未检测到 pyarrow/fastparquet，缓存使用 CSV{RESET}")

    def _call_with_retry(self, api_func, **kwargs):
        retries = 0
        max_retries = 8
        while retries < max_retries:
            try:
                return api_func(**kwargs)
            except Exception as e:
                err_str = str(e).lower()
                if any(kw in err_str for kw in ["限流", "频率超限", "max retries", "connection", "timeout", "500", "502", "503", "429"]):
                    retries += 1
                    wait = 15 + random.randint(5, 20)
                    if not self._rate_limited:
                        print(f"{YELLOW}[警告] API限流，等待 {wait}s...{RESET}")
                        self._rate_limited = True
                    time.sleep(wait)
                else:
                    raise
        raise Exception(f"API调用失败多次: {kwargs}")

    def validate_trade_date(self, input_date=''):
        if not input_date:
            input_date = datetime.now().strftime('%Y%m%d')
        try:
            cal = self.pro.trade_cal(start_date=input_date, end_date=input_date)
            if not cal.empty and cal['is_open'].iloc[0] == 1:
                return input_date
        except:
            pass
        date_obj = datetime.strptime(input_date, '%Y%m%d')
        for i in range(1, 40):
            prev = (date_obj - timedelta(days=i)).strftime('%Y%m%d')
            try:
                cal = self.pro.trade_cal(start_date=prev, end_date=prev)
                if not cal.empty and cal['is_open'].iloc[0] == 1:
                    return prev
            except:
                continue
        return datetime.now().strftime('%Y%m%d')

    def get_trade_dates(self, end_date, n_days=500):
        start_approx = (datetime.strptime(end_date, '%Y%m%d') - timedelta(days=int(n_days * 1.8))).strftime('%Y%m%d')
        cal = self._call_with_retry(self.pro.trade_cal, start_date=start_approx, end_date=end_date, is_open='1')
        dates = sorted(cal['cal_date'].tolist())
        return dates[-n_days:] if len(dates) >= n_days else dates

    def _cache_path(self, trade_date):
        ext = 'parquet' if self.use_parquet else 'csv'
        return os.path.join(GlobalConfig.CACHE_DIR, f"{trade_date}.{ext}")

    def save_day_to_cache(self, trade_date, df):
        if df is None or df.empty: return
        path = self._cache_path(trade_date)
        try:
            if self.use_parquet: df.to_parquet(path, index=False)
            else: df.to_csv(path, index=False, encoding='utf-8-sig')
        except: pass

    def load_day_from_cache(self, trade_date):
        path = self._cache_path(trade_date)
        if not os.path.exists(path): return None
        try:
            return pd.read_parquet(path) if self.use_parquet else pd.read_csv(path)
        except: return None

    def get_cached_dates(self):
        pattern = os.path.join(GlobalConfig.CACHE_DIR, '*.parquet' if self.use_parquet else '*.csv')
        dates = set()
        for f in glob.glob(pattern):
            d = os.path.basename(f).split('.')[0]
            if len(d) == 8 and d.isdigit(): dates.add(d)
        return dates

    def get_stock_pool(self):
        print("正在加载股票池（排除ST/北交所 + 上市≥60天）...")
        df = self.pro.stock_basic(list_status='L', fields='ts_code,name,list_date')
        today = datetime.now().strftime('%Y%m%d')
        filtered = []
        for _, row in df.iterrows():
            name, code = str(row['name']), row['ts_code']
            # 【修复】全面过滤：ST、.BJ结尾、8开头、92开头
            if 'ST' in name or code.endswith('.BJ') or code.startswith('8') or code.startswith('92'): 
                continue
            list_date = str(row['list_date'])
            if len(list_date) == 8:
                days = (datetime.strptime(today, '%Y%m%d') - datetime.strptime(list_date, '%Y%m%d')).days
                if days > 60: filtered.append(row.to_dict())
        stocks = pd.DataFrame(filtered)
        print(f"股票池加载完成，共 {len(stocks)} 只")
        return stocks

    def fetch_rt_k_batch(self):
        patterns = ['60*.SH', '688*.SH', '00*.SZ', '002*.SZ', '003*.SZ', '300*.SZ', '301*.SZ']
        rt_dfs = []
        print("获取实时行情（盘中模式）...")
        for pattern in patterns:
            try:
                df = self._call_with_retry(self.pro.rt_k, ts_code=pattern)
                if not df.empty: rt_dfs.append(df)
                time.sleep(1.0)
            except: pass
        if rt_dfs:
            full_df = pd.concat(rt_dfs, ignore_index=True).drop_duplicates(subset=['ts_code'])
            self.rt_data_dict = full_df.set_index('ts_code').to_dict('index')
            print(f"实时数据获取完成，共 {len(self.rt_data_dict)} 只")

    def get_candidate_stocks(self, trade_date, is_intraday=False):
        all_stocks = self.get_stock_pool()
        strong = []
        print("正在预筛：当日红盘上涨K线 ...")
        if is_intraday:
            self.fetch_rt_k_batch()
            for _, row in all_stocks.iterrows():
                ts_code = row['ts_code']
                if ts_code not in self.rt_data_dict: continue
                rt = self.rt_data_dict[ts_code]
                try:
                    pre_close = float(rt.get('pre_close', 0))
                    close = float(rt.get('close', 0))
                    open_p = float(rt.get('open', 0))
                    if pre_close > 0 and close > open_p and close > pre_close:
                        strong.append(row.to_dict())
                except: continue
        else:
            try:
                df = self.load_day_from_cache(trade_date)
                if df is None or df.empty:
                    df = self._call_with_retry(
                        self.pro.daily, trade_date=trade_date,
                        fields='ts_code,open,close,pct_chg,high,low,vol,amount'
                    )
                    if not df.empty: self.save_day_to_cache(trade_date, df)
                if not df.empty:
                    df['pct_chg'] = pd.to_numeric(df['pct_chg'], errors='coerce')
                    mask = (df['close'] > df['open']) & (df['pct_chg'] > 0)
                    codes = [c for c in df.loc[mask, 'ts_code'].tolist() if c in set(all_stocks['ts_code'])]
                    strong = all_stocks[all_stocks['ts_code'].isin(codes)].to_dict('records')
            except Exception as e:
                print(f"预筛失败: {e}")
        strong_df = pd.DataFrame(strong)
        print(f"预筛完成：共 {len(strong_df)} 只")
        return strong_df

    def TDX_SMA(self, series, n, m):
        res = np.zeros(len(series))
        for i in range(len(series)):
            val = series.iloc[i]
            if pd.isna(val) or np.isinf(val): 
                res[i] = np.nan
            elif i == 0 or pd.isna(res[i - 1]): 
                res[i] = val
            else: 
                res[i] = (m * val + (n - m) * res[i - 1]) / n
        return pd.Series(res, index=series.index)

    def batch_fetch_daily_history(self, trade_date, stock_codes):
        print(f"\n{CYAN}准备日线（公共缓存 + 增量）...{RESET}")
        trade_dates = self.get_trade_dates(trade_date, GlobalConfig.LOOKBACK_DAYS)
        print(f"日线范围: {trade_dates[0]} ~ {trade_dates[-1]}，共 {len(trade_dates)} 天")
        cached = self.get_cached_dates()
        need = [d for d in trade_dates if d not in cached]
        print(f"日线缓存已有 {len(trade_dates)-len(need)} | 需下载 {len(need)}")
        if need:
            for i, d in enumerate(tqdm(need, desc="日线增量")):
                try:
                    df = self._call_with_retry(
                        self.pro.daily, trade_date=d,
                        fields='ts_code,trade_date,open,high,low,close,vol,amount,pct_chg'
                    )
                    if not df.empty: self.save_day_to_cache(d, df)
                    time.sleep(1.2 if (i + 1) % 20 == 0 else 0.2)
                except: time.sleep(3)
        all_dfs = []
        code_set = set(stock_codes)
        for d in tqdm(trade_dates, desc="加载日线"):
            df = self.load_day_from_cache(d)
            if df is not None and not df.empty:
                df = df[df['ts_code'].isin(code_set)]
                if not df.empty: all_dfs.append(df)
        if not all_dfs: return {}
        full = pd.concat(all_dfs, ignore_index=True).sort_values(['ts_code', 'trade_date'])
        return {code: g.reset_index(drop=True) for code, g in full.groupby('ts_code')}

    def process_stock_from_df(self, stock_info, df_daily, trade_date, is_intraday=False):
        ts_code = stock_info['ts_code']
        name = stock_info.get('name', '')
        try:
            if len(df_daily) < 60: return None, 'data_short'

            # 安全机制：强制过滤空数据并转为 float
            df_daily = df_daily.dropna(subset=['open', 'high', 'low', 'close', 'vol'])
            if len(df_daily) < 60: return None, 'data_short'

            # 实时数据修复
            if is_intraday and ts_code in self.rt_data_dict:
                rt = self.rt_data_dict[ts_code]
                last_date = str(df_daily.iloc[-1]['trade_date'])
                if last_date == trade_date:
                    idx = len(df_daily) - 1
                    df_daily = df_daily.copy()
                    df_daily.at[idx, 'close'] = float(rt['close'])
                    df_daily.at[idx, 'high'] = max(float(df_daily.at[idx, 'high']), float(rt['high']))
                    df_daily.at[idx, 'low'] = min(float(df_daily.at[idx, 'low']), float(rt['low']))
                    if float(rt.get('open', 0)) > 0: df_daily.at[idx, 'open'] = float(rt['open'])
                    df_daily.at[idx, 'vol'] = float(rt['vol'])
                else:
                    new_row = {
                        'ts_code': ts_code, 'trade_date': trade_date,
                        'open': float(rt['open']), 'high': float(rt['high']),
                        'low': float(rt['low']), 'close': float(rt['close']),
                        'vol': float(rt['vol']),
                        'pct_chg': (float(rt['close']) / float(rt.get('pre_close', rt['close'])) - 1) * 100
                    }
                    df_daily = pd.concat([df_daily, pd.DataFrame([new_row])], ignore_index=True)

            O = pd.to_numeric(df_daily['open'], errors='coerce').astype(float)
            C = pd.to_numeric(df_daily['close'], errors='coerce').astype(float)
            H = pd.to_numeric(df_daily['high'], errors='coerce').astype(float)
            L = pd.to_numeric(df_daily['low'], errors='coerce').astype(float)
            V = pd.to_numeric(df_daily['vol'], errors='coerce').astype(float)

            # ================= 1. 基础指标计算 =================
            BBI = (C.rolling(3).mean() + C.rolling(6).mean() + C.rolling(12).mean() + C.rolling(24).mean()) / 4
            
            # SKDJ(14,3) (加入 +1e-10 防止除 0)
            LL14 = L.rolling(14).min()
            HH14 = H.rolling(14).max()
            RSV = (C - LL14) / (HH14 - LL14 + 1e-10) * 100
            K_VAL = self.TDX_SMA(RSV, 3, 1)
            D_VAL = self.TDX_SMA(K_VAL, 3, 1)
            
            SKDJ_JC = (K_VAL > D_VAL) & (K_VAL.shift(1) <= D_VAL.shift(1))
            MID_UP = BBI > BBI.shift(1)
            # 【修复】强类型转换
            CORE_SIG = (SKDJ_JC & MID_UP).fillna(False).astype(bool)

            # ================= 2. 前一个交易日条件 =================
            PREV_DOWN = ((C.shift(1) < O.shift(1)) | (C.shift(1) < C.shift(2))).fillna(False).astype(bool)
            PREV_BELOW = (L.shift(1) < BBI.shift(1)).fillna(False).astype(bool)
            PREV_COND = (PREV_DOWN & PREV_BELOW).fillna(False).astype(bool)

            # ================= 3. 当日量价与首次突破 =================
            IS_RED_UP = ((C > O) & (C > C.shift(1))).fillna(False).astype(bool)
            VOL_OK = (V / V.rolling(5).mean().shift(1) > 1.2).fillna(False).astype(bool)
            
            # 【核心修复】前1日不满足核心条件 (即今天必须是近期首次)
            # shift后产生的NaN强制转换为False，避免~报错
            CORE_SIG_PREV = CORE_SIG.shift(1).fillna(False).astype(bool)
            FIRST_TIME = (CORE_SIG & (~CORE_SIG_PREV)).fillna(False).astype(bool)

            # ================= 4. 日线 MACD(8,20,6) =================
            DIF8 = C.ewm(span=8, adjust=False).mean() - C.ewm(span=20, adjust=False).mean()
            DEA6 = DIF8.ewm(span=6, adjust=False).mean()
            MACD_BAR = 2 * (DIF8 - DEA6)

            MACD_RED = ((MACD_BAR > 0) & (MACD_BAR.shift(1) <= 0)).fillna(False).astype(bool)
            MACD_SHRINK = ((MACD_BAR < 0) & (MACD_BAR.shift(1) < 0) & (MACD_BAR > MACD_BAR.shift(1) * 0.7)).fillna(False).astype(bool)
            MACD_COND = (MACD_RED | MACD_SHRINK).fillna(False).astype(bool)

            # ================= 5. 聚合后的周线级别条件 =================
            # 采用日线参数×5放大法，完美模拟真实周线
            W_BBI = (C.rolling(15).mean() + C.rolling(30).mean() + C.rolling(60).mean() + C.rolling(120).mean()) / 4
            W_MID_UP = (W_BBI > W_BBI.shift(1)).fillna(False).astype(bool)

            W_LL70 = L.rolling(70).min()
            W_HH70 = H.rolling(70).max()
            W_RSV = (C - W_LL70) / (W_HH70 - W_LL70 + 1e-10) * 100
            W_K = self.TDX_SMA(W_RSV, 15, 1)
            W_D = self.TDX_SMA(W_K, 15, 1)
            W_JC = ((W_K > W_D) & (W_K.shift(1) <= W_D.shift(1))).fillna(False).astype(bool)

            WEEK_COND = (W_JC | W_MID_UP).fillna(False).astype(bool)

            # ================= 6. 高开过滤条件 =================
            # 定义高开3%：当日开盘价 > 昨日收盘价 * 1.03
            HIGH_OPEN = (O > C.shift(1) * 1.03).fillna(False).astype(bool)

            NO_HIGH_OPEN_TODAY = (O <= C.shift(1) * 1.03).fillna(False).astype(bool)
            NO_HIGH_OPEN_5 = (HIGH_OPEN.rolling(5).sum() == 0).fillna(False).astype(bool)

            # ================= 7. 最终判定 =================
            FINAL_SIGNAL = (PREV_COND & IS_RED_UP & VOL_OK & FIRST_TIME & WEEK_COND & MACD_COND & NO_HIGH_OPEN_TODAY & NO_HIGH_OPEN_5).fillna(False).astype(bool)

            # 判断最后一天（T日）是否触发
            if FINAL_SIGNAL.iloc[-1] == True:
                last_row = df_daily.iloc[-1]
                pct_chg = (float(last_row['close']) / float(df_daily.iloc[-2]['close']) - 1) * 100
                
                return {
                    'ts_code': ts_code,
                    'name': name,
                    'close': round(float(last_row['close']), 2),
                    'pct_chg': round(pct_chg, 2)
                }, 'selected'

            return None, 'condition_fail'

        except Exception as e:
            if len(self.errors) < 5:
                self.errors.append(f"股票 {ts_code} 报错: {str(e)}\n{traceback.format_exc()}")
            return None, 'exception'

    def filter_market_stocks(self, trade_date, is_intraday=False):
        print(f"\n开始【SKDJ首金+BBIBOLL首穿】过滤，日期: {trade_date}")
        stocks = self.get_candidate_stocks(trade_date, is_intraday)
        if len(stocks) == 0:
            print("候选池为空")
            return []
        if is_intraday and not self.rt_data_dict:
            self.fetch_rt_k_batch()

        stock_codes = stocks['ts_code'].tolist()
        daily_dict = self.batch_fetch_daily_history(trade_date, stock_codes)

        selected = []
        stats = {'selected': 0, 'data_short': 0, 'condition_fail': 0, 'exception': 0, 'no_data': 0}
        self.errors = []

        print(f"\n{CYAN}本地计算...{RESET}")
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {}
            for _, row in stocks.iterrows():
                info = row.to_dict()
                code = info['ts_code']
                ddf = daily_dict.get(code)
                if ddf is None or ddf.empty:
                    stats['no_data'] += 1
                    continue
                
                futures[executor.submit(
                    self.process_stock_from_df, info, ddf, trade_date, is_intraday
                )] = code

            for future in tqdm(as_completed(futures), total=len(futures), desc="选股进度"):
                res, reason = future.result()
                if res:
                    selected.append(res)
                    stats['selected'] += 1
                else:
                    stats[reason] = stats.get(reason, 0) + 1

        # 打印具体异常原因
        if self.errors:
            print(f"\n{YELLOW}--- 发现异常计算(已抓取部分样本) ---{RESET}")
            for err in self.errors:
                print(err)
            with open(os.path.join(GlobalConfig.OUTPUT_DIR, "error_log.txt"), "w", encoding="utf-8") as f:
                for err in self.errors:
                    f.write(err + "\n")
            print(f"{YELLOW}完整异常日志已保存到桌面 error_log.txt{RESET}")

        selected = sorted(selected, key=lambda x: x.get('pct_chg', 0), reverse=True)
        print(f"\n{GREEN}筛选完成！共 {len(selected)} 只{RESET}")
        for k, v in stats.items():
            print(f"  {k}: {v}")
        return selected

    def save_results(self, trade_date, results, is_intraday=False):
        if not results:
            print("无符合条件股票")
            return
        os.makedirs(GlobalConfig.OUTPUT_DIR, exist_ok=True)
        mode = "_实时" if is_intraday else ""
        path = os.path.join(GlobalConfig.OUTPUT_DIR, f"SKDJ首金_选股_{trade_date}{mode}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"SKDJ首金 + BBIBOLL首穿 - {trade_date}\n共选中 {len(results)} 只\n\n")
            for i, s in enumerate(results, 1):
                f.write(f"{i:3d}. {s['ts_code']} {s['name']} 价:{s['close']:.2f} 涨:{s['pct_chg']:+.2f}%\n")
        print(f"结果已保存: {path}")

    def print_console_results(self, results):
        print("\n" + "=" * 130)
        print(f"{BOLD}{CYAN}【结果】SKDJ首金 + BBIBOLL首穿 - 共 {len(results)} 只{RESET}")
        print("=" * 130)
        if not results:
            print("无符合条件股票")
            return
        for i, s in enumerate(results, 1):
            color = RED if i % 2 == 1 else YELLOW
            print(f"\n{color}{BOLD}{'─' * 120}{RESET}")
            print(f"{color}{BOLD}  {i:2d}. {s['ts_code']}  {s['name']:<12}{RESET}")
            print(f"{color}      现价: {s['close']:8.2f}  涨幅: {s['pct_chg']:+7.2f}%{RESET}")
            time.sleep(0.05)
        print(f"\n{GREEN}{BOLD}{'=' * 130}{RESET}")
        print(f"{GREEN}{BOLD}完成，共 {len(results)} 只{RESET}\n")

def main():
    print("=" * 110)
    print(f"{BOLD} SKDJ首金 + BBIBOLL首穿 策略 【公共缓存 + 并发版】{RESET}")
    print("=" * 110)
    filter_tool = BBIBOLLSKDJFilter(GlobalConfig.TUSHARE_TOKEN)

    date_input = input("\n输入T日日期(YYYYMMDD)，回车使用今天: ").strip()
    validated_date = filter_tool.validate_trade_date(date_input)
    print(f"使用交易日: {validated_date}")
    print(f"日线缓存: {GlobalConfig.CACHE_DIR}")

    now = datetime.now()
    is_today = (validated_date == now.strftime('%Y%m%d'))
    trading_time = is_today and (now.weekday() < 5) and ((9 <= now.hour < 12) or (13 <= now.hour < 15))
    if trading_time:
        mode_input = input("\n1=收盘模式  2=实时盘中(默认2): ").strip()
        is_intraday = (mode_input != '1')
    else:
        is_intraday = False
        print("非交易时段，使用历史数据")

    results = filter_tool.filter_market_stocks(validated_date, is_intraday)
    filter_tool.save_results(validated_date, results, is_intraday)
    filter_tool.print_console_results(results)

if __name__ == '__main__':
    main()