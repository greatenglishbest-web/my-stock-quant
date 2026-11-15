# -*- coding: utf-8 -*-
"""
选股策略：日周共振 · 中轨必红 + 动能任选
【速度优化 + 公共缓存版】

周线精度：
  - 已收盘周：Tushare pro.weekly（每周最后一个交易日一根完整周K）
  - 本周未收盘：用日线从「上一完整周次日」聚合到 T 日，拼成当前进行中的周K
  - 周线指标在「完整周 + 当前未完成周」序列上计算
  - 方向对比：本周(含T) vs 上一根已收盘周

日线条件不变。
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
    TUSHARE_TOKEN = 'a91652800def39d213abec3af9563e4485e83940bfbbd8276f8adaa8'
    OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', '日周中轨动能共振_选股结果')
    CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股日线公共缓存')
    WEEKLY_CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股周线公共缓存')
    LOOKBACK_DAYS = 500
    LOOKBACK_WEEKS = 80          # 周线指标足够用（BBI需约24周）
    USE_PARQUET = True
    SAR_STICK_PCT = 0.015

class CustomStrategyFilter:
    def __init__(self, token):
        self.pro = ts.pro_api(token)
        self.rt_data_dict = {}
        self.level = 1
        self.verbose = True
        self._rate_limited = False
        os.makedirs(GlobalConfig.CACHE_DIR, exist_ok=True)
        os.makedirs(GlobalConfig.WEEKLY_CACHE_DIR, exist_ok=True)
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

    def set_level(self, level):
        self.level = level

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
                    print(f"其他错误: {str(e)[:120]}")
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

    # ==================== 日线缓存 ====================
    def _cache_path(self, trade_date):
        ext = 'parquet' if self.use_parquet else 'csv'
        return os.path.join(GlobalConfig.CACHE_DIR, f"{trade_date}.{ext}")

    def save_day_to_cache(self, trade_date, df):
        if df is None or df.empty:
            return
        path = self._cache_path(trade_date)
        try:
            if self.use_parquet:
                df.to_parquet(path, index=False)
            else:
                df.to_csv(path, index=False, encoding='utf-8-sig')
        except Exception as e:
            print(f"  保存日线缓存 {trade_date} 失败: {e}")

    def load_day_from_cache(self, trade_date):
        path = self._cache_path(trade_date)
        if not os.path.exists(path):
            return None
        try:
            return pd.read_parquet(path) if self.use_parquet else pd.read_csv(path)
        except Exception as e:
            print(f"  读取日线缓存 {trade_date} 失败: {e}")
            return None

    def get_cached_dates(self):
        pattern = os.path.join(GlobalConfig.CACHE_DIR, '*.parquet' if self.use_parquet else '*.csv')
        dates = set()
        for f in glob.glob(pattern):
            d = os.path.basename(f).split('.')[0]
            if len(d) == 8 and d.isdigit():
                dates.add(d)
        return dates

    # ==================== 周线缓存（按周结束日） ====================
    def _weekly_cache_path(self, week_end_date):
        ext = 'parquet' if self.use_parquet else 'csv'
        return os.path.join(GlobalConfig.WEEKLY_CACHE_DIR, f"{week_end_date}.{ext}")

    def save_week_to_cache(self, week_end_date, df):
        if df is None or df.empty:
            return
        path = self._weekly_cache_path(week_end_date)
        try:
            if self.use_parquet:
                df.to_parquet(path, index=False)
            else:
                df.to_csv(path, index=False, encoding='utf-8-sig')
        except Exception as e:
            print(f"  保存周线缓存 {week_end_date} 失败: {e}")

    def load_week_from_cache(self, week_end_date):
        path = self._weekly_cache_path(week_end_date)
        if not os.path.exists(path):
            return None
        try:
            return pd.read_parquet(path) if self.use_parquet else pd.read_csv(path)
        except Exception as e:
            print(f"  读取周线缓存 {week_end_date} 失败: {e}")
            return None

    def get_cached_week_dates(self):
        pattern = os.path.join(GlobalConfig.WEEKLY_CACHE_DIR, '*.parquet' if self.use_parquet else '*.csv')
        dates = set()
        for f in glob.glob(pattern):
            d = os.path.basename(f).split('.')[0]
            if len(d) == 8 and d.isdigit():
                dates.add(d)
        return dates

    def get_week_end_dates(self, end_date, n_weeks=80):
        """取 end_date 之前（含）的周线结束交易日列表（日历里每周最后一个交易日）"""
        start_approx = (datetime.strptime(end_date, '%Y%m%d') - timedelta(days=int(n_weeks * 10))).strftime('%Y%m%d')
        cal = self._call_with_retry(
            self.pro.trade_cal, start_date=start_approx, end_date=end_date, is_open='1'
        )
        cal = cal.sort_values('cal_date')
        # 交易日转周（周五结束）
        cal['dt'] = pd.to_datetime(cal['cal_date'])
        cal['week'] = cal['dt'].dt.to_period('W-FRI')
        week_ends = cal.groupby('week')['cal_date'].max().tolist()
        week_ends = sorted([d for d in week_ends if d <= end_date])
        return week_ends[-n_weeks:] if len(week_ends) >= n_weeks else week_ends

    def get_stock_pool(self):
        print("正在加载股票池（排除ST/北交所 + 上市≥7天）...")
        df = self.pro.stock_basic(list_status='L', fields='ts_code,name,list_date')
        today = datetime.now().strftime('%Y%m%d')
        filtered = []
        for _, row in df.iterrows():
            name, code = str(row['name']), row['ts_code']
            if 'ST' in name or code.startswith('8'):
                continue
            list_date = str(row['list_date'])
            if len(list_date) == 8:
                days = (datetime.strptime(today, '%Y%m%d') - datetime.strptime(list_date, '%Y%m%d')).days
                if days > 7:
                    filtered.append(row.to_dict())
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
                if not df.empty:
                    rt_dfs.append(df)
                time.sleep(1.0)
            except Exception as e:
                print(f"获取 {pattern} 失败: {str(e)[:50]}")
                time.sleep(3)
        if rt_dfs:
            full_df = pd.concat(rt_dfs, ignore_index=True).drop_duplicates(subset=['ts_code'])
            self.rt_data_dict = full_df.set_index('ts_code').to_dict('index')
            print(f"实时数据获取完成，共 {len(self.rt_data_dict)} 只")

    def get_candidate_stocks(self, trade_date, is_intraday=False):
        all_stocks = self.get_stock_pool()
        strong = []
        print("正在预筛：涨幅 > 1.2% ...")
        if is_intraday:
            self.fetch_rt_k_batch()
            for _, row in all_stocks.iterrows():
                ts_code = row['ts_code']
                if ts_code not in self.rt_data_dict:
                    continue
                rt = self.rt_data_dict[ts_code]
                try:
                    pre_close = float(rt.get('pre_close', 0))
                    close = float(rt.get('close', 0))
                    open_p = float(rt.get('open', 0))
                    if pre_close > 0:
                        pct = (close - pre_close) / pre_close * 100
                        if pct > 1.2 and close > open_p:
                            strong.append(row.to_dict())
                except:
                    continue
        else:
            try:
                df = self.load_day_from_cache(trade_date)
                if df is None or df.empty:
                    df = self._call_with_retry(
                        self.pro.daily, trade_date=trade_date,
                        fields='ts_code,open,close,pct_chg,high,low,vol'
                    )
                    if not df.empty:
                        self.save_day_to_cache(trade_date, df)
                if not df.empty:
                    df['pct_chg'] = pd.to_numeric(df.get('pct_chg', np.nan), errors='coerce')
                    mask = (df['pct_chg'] > 1.2) & (df['close'] > df['open'])
                    codes = [c for c in df.loc[mask, 'ts_code'].tolist() if c in set(all_stocks['ts_code'])]
                    strong = all_stocks[all_stocks['ts_code'].isin(codes)].to_dict('records')
            except Exception as e:
                print(f"批量获取失败: {e}")
        strong_df = pd.DataFrame(strong)
        print(f"预筛完成：共 {len(strong_df)} 只")
        return strong_df

    def TDX_SMA(self, series, n, m):
        res = np.zeros(len(series))
        for i in range(len(series)):
            if pd.isna(series.iloc[i]):
                res[i] = np.nan
            elif i == 0 or pd.isna(res[i - 1]):
                res[i] = series.iloc[i]
            else:
                res[i] = (m * series.iloc[i] + (n - m) * res[i - 1]) / n
        return pd.Series(res, index=series.index)

    def calculate_sar(self, high, low, af=0.02, amax=0.2):
        length = len(high)
        sar = np.zeros(length)
        trend = np.zeros(length)
        ep = np.zeros(length)
        af_array = np.zeros(length)
        if length < 2:
            return pd.Series(sar, index=high.index)
        sar[0], trend[0], ep[0], af_array[0] = low[0], 1, high[0], af
        for i in range(1, length):
            if trend[i - 1] == 1:
                sar[i] = sar[i - 1] + af_array[i - 1] * (ep[i - 1] - sar[i - 1])
                sar[i] = min(sar[i], low[i - 1], low[i - 2]) if i >= 2 else min(sar[i], low[i - 1])
                if low[i] < sar[i]:
                    trend[i], sar[i], ep[i], af_array[i] = -1, ep[i - 1], low[i], af
                else:
                    trend[i] = 1
                    if high[i] > ep[i - 1]:
                        ep[i] = high[i]
                        af_array[i] = min(af_array[i - 1] + af, amax)
                    else:
                        ep[i], af_array[i] = ep[i - 1], af_array[i - 1]
            else:
                sar[i] = sar[i - 1] + af_array[i - 1] * (ep[i - 1] - sar[i - 1])
                sar[i] = max(sar[i], high[i - 1], high[i - 2]) if i >= 2 else max(sar[i], high[i - 1])
                if high[i] > sar[i]:
                    trend[i], sar[i], ep[i], af_array[i] = 1, ep[i - 1], high[i], af
                else:
                    trend[i] = -1
                    if low[i] < ep[i - 1]:
                        ep[i] = low[i]
                        af_array[i] = min(af_array[i - 1] + af, amax)
                    else:
                        ep[i], af_array[i] = ep[i - 1], af_array[i - 1]
        return pd.Series(sar, index=high.index)

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
                        fields='ts_code,trade_date,open,high,low,close,vol,pct_chg'
                    )
                    if not df.empty:
                        self.save_day_to_cache(d, df)
                    time.sleep(1.2 if (i + 1) % 20 == 0 else 0.2)
                except Exception as e:
                    print(f"  下载日线 {d} 失败: {str(e)[:60]}")
                    time.sleep(3)
        all_dfs = []
        code_set = set(stock_codes)
        for d in tqdm(trade_dates, desc="加载日线"):
            df = self.load_day_from_cache(d)
            if df is not None and not df.empty:
                df = df[df['ts_code'].isin(code_set)]
                if not df.empty:
                    all_dfs.append(df)
        if not all_dfs:
            return {}
        full = pd.concat(all_dfs, ignore_index=True).sort_values(['ts_code', 'trade_date'])
        return {code: g.reset_index(drop=True) for code, g in full.groupby('ts_code')}

    def batch_fetch_weekly_history(self, trade_date, stock_codes):
        """按周结束日批量拉取 pro.weekly，本地缓存"""
        print(f"\n{CYAN}准备周线（pro.weekly + 公共缓存）...{RESET}")
        week_ends = self.get_week_end_dates(trade_date, GlobalConfig.LOOKBACK_WEEKS)
        # 若 T 日本身不是某周结束日，则 week_ends 最后一根是「上一完整周」
        # 本周未完成部分用日线补
        if not week_ends:
            print("未找到周结束日")
            return {}

        # 只有「严格早于 T 日」或「等于 T 且 T 是该周最后交易日」的周结束日才有完整 weekly 接口数据
        # trade_date 参数 = 每周最后一个交易日；若 T 不是周结束，最后完整周 = week_ends[-1]（<=T）
        complete_ends = [d for d in week_ends if d < trade_date]
        # 若 T 恰好是某周最后交易日，weekly 可能已更新
        if week_ends and week_ends[-1] == trade_date:
            complete_ends = week_ends
        elif week_ends and week_ends[-1] < trade_date:
            complete_ends = week_ends

        cached = self.get_cached_week_dates()
        need = [d for d in complete_ends if d not in cached]
        print(f"完整周结束日共 {len(complete_ends)} | 缓存已有 {len(complete_ends)-len(need)} | 需下载 {len(need)}")

        if need:
            for i, d in enumerate(tqdm(need, desc="周线增量")):
                try:
                    df = self._call_with_retry(
                        self.pro.weekly,
                        trade_date=d,
                        fields='ts_code,trade_date,open,high,low,close,vol'
                    )
                    if not df.empty:
                        self.save_week_to_cache(d, df)
                    time.sleep(1.5 if (i + 1) % 10 == 0 else 0.35)
                except Exception as e:
                    print(f"  下载周线 {d} 失败: {str(e)[:60]}")
                    time.sleep(3)

        all_dfs = []
        code_set = set(stock_codes)
        for d in tqdm(complete_ends, desc="加载周线"):
            df = self.load_week_from_cache(d)
            if df is not None and not df.empty:
                df = df[df['ts_code'].isin(code_set)]
                if not df.empty:
                    all_dfs.append(df)

        weekly_dict = {}
        if all_dfs:
            full = pd.concat(all_dfs, ignore_index=True).sort_values(['ts_code', 'trade_date'])
            weekly_dict = {code: g.reset_index(drop=True) for code, g in full.groupby('ts_code')}
        print(f"{GREEN}完整周线就绪，共 {len(weekly_dict)} 只{RESET}")
        return weekly_dict, complete_ends

    def build_weekly_with_current(self, df_weekly_complete, df_daily, trade_date, last_complete_week_end):
        """
        完整周K + 本周未完成周K（日线聚合到 T）
        last_complete_week_end: 上一根完整周的 trade_date
        """
        cols = ['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'vol']
        if df_weekly_complete is not None and not df_weekly_complete.empty:
            w = df_weekly_complete[cols].copy()
            w = w.sort_values('trade_date').reset_index(drop=True)
        else:
            w = pd.DataFrame(columns=cols)

        # 本周：日线 trade_date > last_complete_week_end 且 <= T
        d = df_daily.copy()
        d['trade_date'] = d['trade_date'].astype(str)
        mask = (d['trade_date'] > str(last_complete_week_end)) & (d['trade_date'] <= str(trade_date))
        cur = d.loc[mask]
        if cur.empty:
            # 可能 T 就是完整周结束日，无需追加
            return w

        partial = {
            'ts_code': cur['ts_code'].iloc[-1] if 'ts_code' in cur.columns else '',
            'trade_date': str(trade_date),  # 用 T 日标记「进行中的周」
            'open': float(cur['open'].iloc[0]),
            'high': float(cur['high'].max()),
            'low': float(cur['low'].min()),
            'close': float(cur['close'].iloc[-1]),
            'vol': float(cur['vol'].sum()),
        }
        # 若最后一根完整周 trade_date == T，不重复追加
        if not w.empty and str(w['trade_date'].iloc[-1]) == str(trade_date):
            return w
        w = pd.concat([w, pd.DataFrame([partial])], ignore_index=True)
        return w

    def process_stock_from_df(self, stock_info, df_daily, df_weekly_complete, trade_date,
                              last_complete_week_end, is_intraday=False):
        ts_code = stock_info['ts_code']
        name = stock_info.get('name', '')
        try:
            if len(df_daily) < 130:
                return None, 'data_short'

            if is_intraday and ts_code in self.rt_data_dict:
                rt = self.rt_data_dict[ts_code]
                last_date = str(df_daily.iloc[-1]['trade_date'])
                if last_date == trade_date:
                    idx = len(df_daily) - 1
                    df_daily = df_daily.copy()
                    df_daily.at[idx, 'close'] = float(rt['close'])
                    df_daily.at[idx, 'high'] = max(float(df_daily.at[idx, 'high']), float(rt['high']))
                    df_daily.at[idx, 'low'] = min(float(df_daily.at[idx, 'low']), float(rt['low']))
                    if float(rt.get('open', 0)) > 0:
                        df_daily.at[idx, 'open'] = float(rt['open'])
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

            close_p = pd.Series(df_daily['close'].values.astype(float))
            high_p = pd.Series(df_daily['high'].values.astype(float))
            low_p = pd.Series(df_daily['low'].values.astype(float))
            open_p = pd.Series(df_daily['open'].values.astype(float))
            vol_p = pd.Series(df_daily['vol'].values.astype(float))

            # ========== 日线条件 ==========
            is_red = close_p > open_p
            is_up = close_p > close_p.shift(1)
            red_up = is_red & is_up
            vol_ok = vol_p / (vol_p.rolling(5).mean().shift(1) + 1e-10) > 1.2
            pct = (close_p / close_p.shift(1) - 1) * 100
            pct_ok = pct > 1.2

            prev_down = (close_p.shift(1) < open_p.shift(1)) | (close_p.shift(1) < close_p.shift(2))
            bbi = (close_p.rolling(3).mean() + close_p.rolling(6).mean() +
                   close_p.rolling(12).mean() + close_p.rolling(24).mean()) / 4
            prev_low_below = low_p.shift(1) < bbi.shift(1)
            prev_wash = prev_down & prev_low_below
            d_bbi_up = bbi > bbi.shift(1)

            ema8 = close_p.ewm(span=8, adjust=False).mean()
            ema20 = close_p.ewm(span=20, adjust=False).mean()
            dif = ema8 - ema20
            dea = dif.ewm(span=6, adjust=False).mean()
            macd_bar = 2 * (dif - dea)
            d_macd_ok = ((dif > dea) & (dif.shift(1) <= dea.shift(1))) | (macd_bar > 0)

            llv = low_p.rolling(14).min()
            hhv = high_p.rolling(14).max()
            rsv = (close_p - llv) / (hhv - llv + 1e-10) * 100
            k_line = self.TDX_SMA(rsv, 3, 1)
            d_line = self.TDX_SMA(k_line, 3, 1)
            j_line = 3 * k_line - 2 * d_line
            kdj_bar = 2 * (k_line - d_line)
            d_kdj_ok = ((k_line > d_line) & (k_line.shift(1) <= d_line.shift(1))) | (kdj_bar > 0)

            kc_mid = close_p.ewm(span=20, adjust=False).mean()
            d_kc_up = kc_mid > kc_mid.shift(1)

            sar_val = self.calculate_sar(high_p, low_p)
            sar_red = close_p > sar_val
            sar_green = ~sar_red
            sar_first_red = sar_red & (sar_red.astype(int).rolling(3).sum() == 1)
            sar_stick = sar_green & ((close_p - sar_val).abs() / (close_p + 1e-10) < GlobalConfig.SAR_STICK_PCT)
            d_sar_ok = sar_first_red | sar_stick

            d_cnt = (d_macd_ok.astype(int) + d_kdj_ok.astype(int) + d_kc_up.astype(int) + d_sar_ok.astype(int))
            daily_ok = prev_wash & red_up & vol_ok & pct_ok & d_bbi_up & (d_cnt >= 2)

            if not bool(daily_ok.iloc[-1]):
                return None, 'condition_fail'

            # ========== 真周线：完整周 + 本周(含T) ==========
            wdf = self.build_weekly_with_current(
                df_weekly_complete, df_daily, trade_date, last_complete_week_end
            )
            if wdf is None or len(wdf) < 30:
                return None, 'data_short'

            wc = pd.Series(wdf['close'].values.astype(float))
            wh = pd.Series(wdf['high'].values.astype(float))
            wl = pd.Series(wdf['low'].values.astype(float))

            # 周 BBI 必向上：本周(含T) > 上一完整周
            w_bbi = (wc.rolling(3).mean() + wc.rolling(6).mean() +
                     wc.rolling(12).mean() + wc.rolling(24).mean()) / 4
            w_bbi_up = w_bbi.iloc[-1] > w_bbi.iloc[-2]

            # 周 MACD
            w_dif = wc.ewm(span=8, adjust=False).mean() - wc.ewm(span=20, adjust=False).mean()
            w_dea = w_dif.ewm(span=6, adjust=False).mean()
            w_macd_ok = w_dif.iloc[-1] > w_dea.iloc[-1]

            # 周 SAR
            w_sar = self.calculate_sar(wh, wl)
            w_sar_ok = wc.iloc[-1] > w_sar.iloc[-1]

            # 周 KDJ
            w_llv = wl.rolling(14).min()
            w_hhv = wh.rolling(14).max()
            w_rsv = (wc - w_llv) / (w_hhv - w_llv + 1e-10) * 100
            w_k = self.TDX_SMA(w_rsv, 3, 1)
            w_d = self.TDX_SMA(w_k, 3, 1)
            w_j = 3 * w_k - 2 * w_d
            w_kdj_ok = (w_j.iloc[-1] > w_d.iloc[-1]) and (w_j.iloc[-1] >= w_j.iloc[-2])

            w_extra = int(w_macd_ok) + int(w_sar_ok) + int(w_kdj_ok)
            weekly_ok = w_bbi_up and (w_extra >= 1)

            if not weekly_ok:
                return None, 'condition_fail'

            t_day = df_daily.iloc[-1]
            t1 = df_daily.iloc[-2] if len(df_daily) > 1 else None
            pct_chg = (float(t_day['close']) / float(t1['close']) - 1) * 100 if t1 is not None else 0
            vol_ratio = float(vol_p.iloc[-1] / (vol_p.iloc[-6:-1].mean() + 1e-10)) if len(vol_p) >= 6 else 0

            d_list = []
            if d_macd_ok.iloc[-1]:
                d_list.append("MACD")
            if d_kdj_ok.iloc[-1]:
                d_list.append("KDJ")
            if d_kc_up.iloc[-1]:
                d_list.append("肯特纳")
            if d_sar_ok.iloc[-1]:
                d_list.append("SAR粘合" if (sar_stick.iloc[-1] and not sar_first_red.iloc[-1]) else "SAR首红")

            w_list = ["周BBI"]
            if w_macd_ok:
                w_list.append("周MACD")
            if w_sar_ok:
                w_list.append("周SAR")
            if w_kdj_ok:
                w_list.append("周KDJ")

            return {
                'ts_code': ts_code,
                'name': name,
                'current_price': round(float(t_day['close']), 2),
                'pct_chg': round(pct_chg, 2),
                'signal': '日周中轨动能共振',
                'daily_sig': "+".join(d_list),
                'weekly_sig': "+".join(w_list),
                'bbi': round(float(bbi.iloc[-1]), 2),
                'w_bbi': round(float(w_bbi.iloc[-1]), 2),
                'w_bbi_prev': round(float(w_bbi.iloc[-2]), 2),
                'macd_bar': round(float(macd_bar.iloc[-1]), 4),
                'kdj_bar': round(float(kdj_bar.iloc[-1]), 4),
                'j': round(float(j_line.iloc[-1]), 2),
                'kc_mid': round(float(kc_mid.iloc[-1]), 2),
                'sar': round(float(sar_val.iloc[-1]), 2),
                'vol_ratio': round(vol_ratio, 2),
                'd_cnt': int(d_cnt.iloc[-1]),
                'last_week_end': str(last_complete_week_end),
            }, 'selected'
        except Exception:
            return None, 'exception'

    def filter_market_stocks(self, trade_date, is_intraday=False):
        print(f"\n开始【日周中轨动能共振·真周线】过滤，日期: {trade_date}")
        stocks = self.get_candidate_stocks(trade_date, is_intraday)
        if len(stocks) == 0:
            print("候选池为空")
            return []

        if is_intraday and not self.rt_data_dict:
            self.fetch_rt_k_batch()

        stock_codes = stocks['ts_code'].tolist()
        daily_dict = self.batch_fetch_daily_history(trade_date, stock_codes)
        weekly_dict, complete_ends = self.batch_fetch_weekly_history(trade_date, stock_codes)
        last_complete = complete_ends[-1] if complete_ends else ''
        print(f"上一完整周结束日: {last_complete} | 本周用日线聚合至 {trade_date}")

        selected = []
        stats = {'selected': 0, 'data_short': 0, 'condition_fail': 0, 'exception': 0, 'no_data': 0}

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
                wdf = weekly_dict.get(code)
                futures[executor.submit(
                    self.process_stock_from_df, info, ddf, wdf, trade_date, last_complete, is_intraday
                )] = code

            for future in tqdm(as_completed(futures), total=len(futures), desc="选股进度"):
                res, reason = future.result()
                if res:
                    selected.append(res)
                    stats['selected'] += 1
                else:
                    stats[reason] = stats.get(reason, 0) + 1

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
        path = os.path.join(GlobalConfig.OUTPUT_DIR, f"日周中轨动能共振_{trade_date}{mode}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"日周中轨动能共振(真周线) - {trade_date}\n共选中 {len(results)} 只\n\n")
            for i, s in enumerate(results, 1):
                f.write(f"{i:3d}. {s['ts_code']} {s['name']} 价:{s['current_price']:.2f} "
                        f"涨:{s['pct_chg']:+.2f}% 量比:{s['vol_ratio']} "
                        f"日:{s['daily_sig']} 周:{s['weekly_sig']} "
                        f"周BBI:{s['w_bbi']}(上周{s['w_bbi_prev']}) 完整周止:{s['last_week_end']}\n")
        print(f"结果已保存: {path}")

    def print_console_results(self, results):
        print("\n" + "=" * 130)
        print(f"{BOLD}{CYAN}【结果】日周中轨动能共振·真周线 - 共 {len(results)} 只{RESET}")
        print("=" * 130)
        if not results:
            print("无符合条件股票")
            return
        for i, s in enumerate(results, 1):
            color = RED if i % 2 == 1 else YELLOW
            print(f"\n{color}{BOLD}{'─' * 120}{RESET}")
            print(f"{color}{BOLD}  {i:2d}. {s['ts_code']}  {s['name']:<12}{RESET}")
            print(f"{color}      现价: {s['current_price']:8.2f}  涨幅: {s['pct_chg']:+7.2f}%  量比: {s['vol_ratio']:.2f}{RESET}")
            print(f"{color}      日线({s['d_cnt']}): {s['daily_sig']:<28}  周线: {s['weekly_sig']}{RESET}")
            print(f"{color}      日BBI: {s['bbi']:.2f}  本周BBI: {s['w_bbi']:.2f}  上完整周BBI: {s['w_bbi_prev']:.2f}  "
                  f"完整周止: {s['last_week_end']}{RESET}")
            print(f"{color}      MACD柱: {s['macd_bar']:.4f}  KDJ柱: {s['kdj_bar']:.4f}  J: {s['j']:.2f}  "
                  f"肯特纳: {s['kc_mid']:.2f}  SAR: {s['sar']:.2f}{RESET}")
            time.sleep(0.05)
        print(f"\n{GREEN}{BOLD}{'=' * 130}{RESET}")
        print(f"{GREEN}{BOLD}完成，共 {len(results)} 只{RESET}\n")

def main():
    print("=" * 110)
    print(f"{BOLD} 日周中轨动能共振（pro.weekly完整周 + 日线补本周）【公共缓存】{RESET}")
    print("=" * 110)
    filter_tool = CustomStrategyFilter(GlobalConfig.TUSHARE_TOKEN)
    filter_tool.set_level(1)

    date_input = input("\n输入T日日期(YYYYMMDD)，回车使用今天: ").strip()
    validated_date = filter_tool.validate_trade_date(date_input)
    print(f"使用交易日: {validated_date}")
    print(f"日线缓存: {GlobalConfig.CACHE_DIR}")
    print(f"周线缓存: {GlobalConfig.WEEKLY_CACHE_DIR}")
    print(f"结果目录: {GlobalConfig.OUTPUT_DIR}")

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