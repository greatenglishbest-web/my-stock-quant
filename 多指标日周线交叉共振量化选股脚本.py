# -*- coding: utf-8 -*-
"""
选股策略：日周共振 · 交叉首红（近3日首次）
【速度优化 + 公共缓存版】

核心逻辑：
1. 日线：SAR / MACD / KDJ / BBIBOLL中轨 / 肯特纳中轨 五者中，
   至少 3 个为「近3日首次转红」
2. 周线（由日线聚合模拟）：任一首周转红/向上（可交叉）
3. 运行日首次同步：前1、前2日不得已同步
4. 预筛：涨幅 > 0.5%
5. 含当日：禁止三连红、三连涨
6. 当日量比 > 1.2
7. 日线 RSI、KDJ-J 方向向上；周线（聚合）RSI、KDJ-J 方向向上
8. 前一日：下跌/绿K，且最低价位于 BBI 中轨下方
9. 近5日：至少2根绿K或下跌K
"""
import tushare as ts
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, time as dttime
import os
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import time
import random
import glob

warnings.filterwarnings('ignore')

# ==================== 颜色输出 ====================
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
    TUSHARE_TOKEN = 'a91652800def39d213abec3af9563e4485e83940bfbbd8276f8adaa8'  # 请确保Token有效
    OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', '日周交叉首红_选股结果')
    CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股日线公共缓存')
    LOOKBACK_DAYS = 500
    USE_PARQUET = True

class CustomStrategyFilter:
    def __init__(self, token):
        self.pro = ts.pro_api(token)
        self.rt_data_dict = {}
        self.level = 1
        self.verbose = True
        self._rate_limited = False
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
                    print(f"{YELLOW}未检测到 pyarrow/fastparquet，缓存将使用 CSV（稍慢但兼容）{RESET}")

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
                        print(f"{YELLOW}[警告] API限流/连接错误，等待 {wait} 秒后重试...{RESET}")
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
        cal = self._call_with_retry(
            self.pro.trade_cal,
            start_date=start_approx,
            end_date=end_date,
            is_open='1'
        )
        dates = sorted(cal['cal_date'].tolist())
        return dates[-n_days:] if len(dates) >= n_days else dates

    # ==================== 缓存读写 ====================
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
            print(f"  保存缓存 {trade_date} 失败: {e}")

    def load_day_from_cache(self, trade_date):
        path = self._cache_path(trade_date)
        if not os.path.exists(path):
            return None
        try:
            if self.use_parquet:
                return pd.read_parquet(path)
            else:
                return pd.read_csv(path)
        except Exception as e:
            print(f"  读取缓存 {trade_date} 失败: {e}")
            return None

    def get_cached_dates(self):
        pattern = os.path.join(GlobalConfig.CACHE_DIR, '*.parquet' if self.use_parquet else '*.csv')
        files = glob.glob(pattern)
        dates = set()
        for f in files:
            name = os.path.basename(f)
            d = name.split('.')[0]
            if len(d) == 8 and d.isdigit():
                dates.add(d)
        return dates

    def get_stock_pool(self):
        print("正在加载股票池（排除ST/北交所/银行/保险/证券 + 上市≥7天）...")
        df = self.pro.stock_basic(list_status='L', fields='ts_code,name,list_date')
        today = datetime.now().strftime('%Y%m%d')
        filtered = []
        for _, row in df.iterrows():
            name = str(row['name'])
            code = row['ts_code']
            if 'ST' in name or code.startswith('8') or '银行' in name or '保险' in name or '证券' in name:
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
                print(f"获取 {pattern} 实时行情失败: {str(e)[:50]}")
                time.sleep(3)
        if rt_dfs:
            full_df = pd.concat(rt_dfs, ignore_index=True).drop_duplicates(subset=['ts_code'])
            self.rt_data_dict = full_df.set_index('ts_code').to_dict('index')
            print(f"实时数据获取完成，共 {len(self.rt_data_dict)} 只股票")

    def get_candidate_stocks(self, trade_date, is_intraday=False):
        """候选池：当日涨幅 > 0.5%"""
        all_stocks = self.get_stock_pool()
        strong = []
        print("正在预筛：当日涨幅 > 0.5% ...")
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
                    if pre_close > 0:
                        pct_chg = (close - pre_close) / pre_close * 100
                        if pct_chg > 0.5:
                            strong.append(row.to_dict())
                except:
                    continue
        else:
            try:
                df = self.load_day_from_cache(trade_date)
                if df is None or df.empty:
                    df = self._call_with_retry(
                        self.pro.daily,
                        trade_date=trade_date,
                        fields='ts_code,open,close,pct_chg,high,low,vol'
                    )
                    if not df.empty:
                        self.save_day_to_cache(trade_date, df)
                if not df.empty:
                    if 'pct_chg' not in df.columns:
                        df['pct_chg'] = np.nan
                    df['pct_chg'] = pd.to_numeric(df['pct_chg'], errors='coerce')
                    mask = df['pct_chg'] > 0.5
                    codes = df.loc[mask, 'ts_code'].tolist()
                    valid = set(all_stocks['ts_code'])
                    codes = [c for c in codes if c in valid]
                    strong = all_stocks[all_stocks['ts_code'].isin(codes)].to_dict('records')
            except Exception as e:
                print(f"批量获取T日数据失败: {e}")
        strong_df = pd.DataFrame(strong)
        print(f"预筛完成：涨幅>0.5% 共 {len(strong_df)} 只")
        return strong_df

    # ==================== 通达信指标复刻 ====================
    def TDX_SMA(self, series, n, m):
        res = np.zeros(len(series))
        for i in range(len(series)):
            if pd.isna(series.iloc[i]):
                res[i] = np.nan
            else:
                if i == 0 or pd.isna(res[i - 1]):
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
        sar[0] = low[0]
        trend[0] = 1
        ep[0] = high[0]
        af_array[0] = af
        for i in range(1, length):
            if trend[i - 1] == 1:
                sar[i] = sar[i - 1] + af_array[i - 1] * (ep[i - 1] - sar[i - 1])
                if i >= 2:
                    sar[i] = min(sar[i], low[i - 1], low[i - 2])
                else:
                    sar[i] = min(sar[i], low[i - 1])
                if low[i] < sar[i]:
                    trend[i] = -1
                    sar[i] = ep[i - 1]
                    ep[i] = low[i]
                    af_array[i] = af
                else:
                    trend[i] = 1
                    if high[i] > ep[i - 1]:
                        ep[i] = high[i]
                        af_array[i] = min(af_array[i - 1] + af, amax)
                    else:
                        ep[i] = ep[i - 1]
                        af_array[i] = af_array[i - 1]
            else:
                sar[i] = sar[i - 1] + af_array[i - 1] * (ep[i - 1] - sar[i - 1])
                if i >= 2:
                    sar[i] = max(sar[i], high[i - 1], high[i - 2])
                else:
                    sar[i] = max(sar[i], high[i - 1])
                if high[i] > sar[i]:
                    trend[i] = 1
                    sar[i] = ep[i - 1]
                    ep[i] = high[i]
                    af_array[i] = af
                else:
                    trend[i] = -1
                    if low[i] < ep[i - 1]:
                        ep[i] = low[i]
                        af_array[i] = min(af_array[i - 1] + af, amax)
                    else:
                        ep[i] = ep[i - 1]
                        af_array[i] = af_array[i - 1]
        return pd.Series(sar, index=high.index)

    def calc_rsi(self, close, period=14):
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / (avg_loss + 1e-10)
        return 100 - (100 / (1 + rs))

    # ==================== 增量缓存拉取 ====================
    def batch_fetch_daily_history(self, trade_date, stock_codes):
        print(f"\n{CYAN}正在准备历史日线数据（公共缓存 + 增量更新）...{RESET}")
        trade_dates = self.get_trade_dates(trade_date, GlobalConfig.LOOKBACK_DAYS)
        print(f"需要交易日范围: {trade_dates[0]} ~ {trade_dates[-1]}，共 {len(trade_dates)} 天")

        cached = self.get_cached_dates()
        need_download = [d for d in trade_dates if d not in cached]
        already_have = [d for d in trade_dates if d in cached]
        print(f"公共缓存已有: {len(already_have)} 天 | 需要下载: {len(need_download)} 天")

        if need_download:
            print(f"{YELLOW}开始增量下载缺失交易日...{RESET}")
            for i, d in enumerate(tqdm(need_download, desc="增量下载")):
                try:
                    df = self._call_with_retry(
                        self.pro.daily,
                        trade_date=d,
                        fields='ts_code,trade_date,open,high,low,close,vol,pct_chg'
                    )
                    if not df.empty:
                        self.save_day_to_cache(d, df)
                    if (i + 1) % 20 == 0:
                        time.sleep(1.2)
                    else:
                        time.sleep(0.2)
                except Exception as e:
                    print(f"  下载 {d} 失败: {str(e)[:60]}")
                    time.sleep(3)
            print(f"{GREEN}增量下载完成{RESET}")
        else:
            print(f"{GREEN}全部交易日均已在公共缓存，无需下载{RESET}")

        print(f"{CYAN}从公共缓存加载数据...{RESET}")
        all_dfs = []
        code_set = set(stock_codes)
        for d in tqdm(trade_dates, desc="加载缓存"):
            df = self.load_day_from_cache(d)
            if df is not None and not df.empty:
                df = df[df['ts_code'].isin(code_set)]
                if not df.empty:
                    all_dfs.append(df)

        if not all_dfs:
            print("未获取到任何历史日线数据！")
            return {}

        full = pd.concat(all_dfs, ignore_index=True)
        full = full.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
        data_dict = {code: group.reset_index(drop=True)
                     for code, group in full.groupby('ts_code')}
        print(f"{GREEN}历史数据就绪，共 {len(data_dict)} 只股票有效数据{RESET}")
        return data_dict

    def process_stock_from_df(self, stock_info, df_daily, trade_date, is_intraday=False):
        """日周交叉首红(日线≥3) + 防三连 + 量比 + 日周RSI/J + 前日洗盘"""
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

            # ========== 日线五项指标 ==========
            sar_val = self.calculate_sar(high_p, low_p, af=0.02, amax=0.2)
            sar_red = close_p > sar_val

            ema8 = close_p.ewm(span=8, adjust=False).mean()
            ema20 = close_p.ewm(span=20, adjust=False).mean()
            dif8 = ema8 - ema20
            dea6 = dif8.ewm(span=6, adjust=False).mean()
            macd_bar = 2 * (dif8 - dea6)
            macd_red = macd_bar > 0

            low_min = low_p.rolling(14).min()
            high_max = high_p.rolling(14).max()
            rsv = (close_p - low_min) / (high_max - low_min + 1e-10) * 100
            k_line = self.TDX_SMA(rsv, 3, 1)
            d_line = self.TDX_SMA(k_line, 3, 1)
            j_line = 3 * k_line - 2 * d_line
            kdj_bar = 2 * (k_line - d_line)
            kdj_red = kdj_bar > 0

            bbi = (close_p.rolling(3).mean() + close_p.rolling(6).mean() +
                   close_p.rolling(12).mean() + close_p.rolling(24).mean()) / 4
            bbi_red = bbi > bbi.shift(1)

            kc_mid = close_p.ewm(span=20, adjust=False).mean()
            kc_red = kc_mid > kc_mid.shift(1)

            d_sar = sar_red & (sar_red.astype(int).rolling(3).sum() == 1)
            d_macd = macd_red & (macd_red.astype(int).rolling(3).sum() == 1)
            d_kdj = kdj_red & (kdj_red.astype(int).rolling(3).sum() == 1)
            d_bbi = bbi_red & (bbi_red.astype(int).rolling(3).sum() == 1)
            d_kc = kc_red & (kc_red.astype(int).rolling(3).sum() == 1)

            # ★ 日线至少 3 个指标近3日首红
            daily_cnt = (d_sar.astype(int) + d_macd.astype(int) + d_kdj.astype(int) +
                         d_bbi.astype(int) + d_kc.astype(int))
            daily_ok = daily_cnt >= 3

            # ========== 周线模拟 ==========
            w_bbi = (close_p.rolling(15).mean() + close_p.rolling(30).mean() +
                     close_p.rolling(60).mean() + close_p.rolling(120).mean()) / 4
            w_bbi_up = w_bbi >= w_bbi.shift(5)
            w_bbi_first = w_bbi_up & (~w_bbi_up.shift(5).fillna(False))

            w_kc = close_p.ewm(span=100, adjust=False).mean()
            w_kc_up = w_kc >= w_kc.shift(5)
            w_kc_first = w_kc_up & (~w_kc_up.shift(5).fillna(False))

            w_ema_fast = close_p.ewm(span=40, adjust=False).mean()
            w_ema_slow = close_p.ewm(span=100, adjust=False).mean()
            w_dif = w_ema_fast - w_ema_slow
            w_dea = w_dif.ewm(span=30, adjust=False).mean()
            w_macd_bar = 2 * (w_dif - w_dea)
            w_macd_up = w_macd_bar > 0
            w_macd_first = w_macd_up & (~w_macd_up.shift(5).fillna(False))

            w_k_up = k_line >= k_line.shift(5)
            w_k_first = w_k_up & (~w_k_up.shift(5).fillna(False))

            w_sar_up = sar_red
            w_sar_first = w_sar_up & (~w_sar_up.shift(5).fillna(False))

            weekly_any = w_bbi_first | w_kc_first | w_macd_first | w_k_first | w_sar_first

            sync = daily_ok & weekly_any
            first_sync = sync & (~sync.shift(1).fillna(False)) & (~sync.shift(2).fillna(False))

            # ========== 含当日禁止三连红 / 三连涨 ==========
            is_red = close_p > open_p
            is_up = close_p > close_p.shift(1)
            is_green = close_p < open_p
            is_down = close_p < close_p.shift(1)
            no_3_red = is_red.rolling(3).sum() < 3
            no_3_up = is_up.rolling(3).sum() < 3
            no_triple = no_3_red & no_3_up

            # ========== 当日量比 > 1.2 ==========
            vol_ok = vol_p / (vol_p.rolling(5).mean().shift(1) + 1e-10) > 1.2

            # ========== 日线 RSI、J 方向向上 ==========
            rsi14 = self.calc_rsi(close_p, 14)
            d_rsi_up = rsi14 > rsi14.shift(1)
            d_j_up = j_line > j_line.shift(1)
            d_rj_up = d_rsi_up & d_j_up

            # ========== 周线 RSI、J 方向向上 ==========
            w_rsi_up = rsi14 >= rsi14.shift(5)
            w_j_up = j_line >= j_line.shift(5)
            w_rj_up = w_rsi_up & w_j_up

            # ========== 前一日下跌/绿K，且最低价 < BBI中轨 ==========
            prev_down = (close_p.shift(1) < open_p.shift(1)) | (close_p.shift(1) < close_p.shift(2))
            prev_low_below_bbi = low_p.shift(1) < bbi.shift(1)
            prev_wash = prev_down & prev_low_below_bbi

            # ========== 近5日至少2根绿K或下跌K（不含当日） ==========
            soft = is_green | is_down
            soft_5 = soft.shift(1).rolling(5).sum() >= 2

            # ========== 最终 ==========
            final = (first_sync & no_triple & vol_ok & d_rj_up & w_rj_up &
                     prev_wash & soft_5)

            if final.iloc[-1] == True:
                t_day = df_daily.iloc[-1]
                t1_day = df_daily.iloc[-2] if len(df_daily) > 1 else None
                pct_chg = (float(t_day['close']) / float(t1_day['close']) - 1) * 100 if t1_day is not None else 0

                d_list = []
                if d_sar.iloc[-1]:
                    d_list.append("SAR")
                if d_macd.iloc[-1]:
                    d_list.append("MACD")
                if d_kdj.iloc[-1]:
                    d_list.append("KDJ")
                if d_bbi.iloc[-1]:
                    d_list.append("BBI")
                if d_kc.iloc[-1]:
                    d_list.append("肯特纳")
                d_str = "+".join(d_list) if d_list else "-"

                w_list = []
                if w_bbi_first.iloc[-1]:
                    w_list.append("周BBI")
                if w_kc_first.iloc[-1]:
                    w_list.append("周肯特纳")
                if w_macd_first.iloc[-1]:
                    w_list.append("周MACD")
                if w_k_first.iloc[-1]:
                    w_list.append("周KDJ")
                if w_sar_first.iloc[-1]:
                    w_list.append("周SAR")
                w_str = "+".join(w_list) if w_list else "-"

                vol_ratio = float(vol_p.iloc[-1] / (vol_p.iloc[-6:-1].mean() + 1e-10)) if len(vol_p) >= 6 else 0

                return {
                    'ts_code': ts_code,
                    'name': name,
                    'current_price': round(float(t_day['close']), 2),
                    'pct_chg': round(pct_chg, 2),
                    'signal': '日周交叉首红(≥3)',
                    'daily_first': d_str,
                    'daily_cnt': int(daily_cnt.iloc[-1]),
                    'weekly_first': w_str,
                    'bbi': round(float(bbi.iloc[-1]), 2),
                    'kc_mid': round(float(kc_mid.iloc[-1]), 2),
                    'macd_bar': round(float(macd_bar.iloc[-1]), 4),
                    'kdj_bar': round(float(kdj_bar.iloc[-1]), 4),
                    'sar': round(float(sar_val.iloc[-1]), 2),
                    'w_bbi': round(float(w_bbi.iloc[-1]), 2),
                    'rsi': round(float(rsi14.iloc[-1]), 2),
                    'j_val': round(float(j_line.iloc[-1]), 2),
                    'vol_ratio': round(vol_ratio, 2),
                }, 'selected'
            return None, 'condition_fail'
        except Exception:
            return None, 'exception'

    def filter_market_stocks(self, trade_date, is_intraday=False):
        print(f"\n开始【日周交叉首红共振(日线≥3)】过滤，日期: {trade_date}")
        stocks = self.get_candidate_stocks(trade_date, is_intraday)
        if len(stocks) == 0:
            print("候选池为空，结束运行")
            return []

        stock_codes = stocks['ts_code'].tolist()
        data_dict = self.batch_fetch_daily_history(trade_date, stock_codes)

        selected = []
        filter_stats = {'selected': 0, 'data_short': 0, 'condition_fail': 0, 'exception': 0, 'no_data': 0}

        print(f"\n{CYAN}开始本地计算指标并筛选信号...{RESET}")
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {}
            for _, row in stocks.iterrows():
                info = row.to_dict()
                code = info['ts_code']
                df = data_dict.get(code)
                if df is None or df.empty:
                    filter_stats['no_data'] += 1
                    continue
                futures[executor.submit(self.process_stock_from_df, info, df, trade_date, is_intraday)] = code

            for future in tqdm(as_completed(futures), total=len(futures), desc="选股进度"):
                res, reason = future.result()
                if res:
                    selected.append(res)
                    filter_stats['selected'] += 1
                else:
                    filter_stats[reason] = filter_stats.get(reason, 0) + 1

        selected = sorted(selected, key=lambda x: x.get('pct_chg', 0), reverse=True)
        print(f"\n{GREEN}筛选完成！共选中 {len(selected)} 只股票{RESET}")
        for k, v in filter_stats.items():
            print(f"  {k}: {v}")
        return selected

    def save_results(self, trade_date, results, is_intraday=False):
        if not results:
            print("无符合条件股票")
            return
        os.makedirs(GlobalConfig.OUTPUT_DIR, exist_ok=True)
        mode = "_实时" if is_intraday else ""
        path = os.path.join(GlobalConfig.OUTPUT_DIR, f"日周交叉首红_选股_{trade_date}{mode}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"日周交叉首红共振(日线≥3) - {trade_date}\n")
            f.write(f"共选中 {len(results)} 只\n\n")
            for i, s in enumerate(results, 1):
                f.write(f"{i:3d}. {s['ts_code']} {s['name']} 价格:{s['current_price']:.2f} "
                        f"涨幅:{s['pct_chg']:+.2f}% 日线首红({s['daily_cnt']}):{s['daily_first']} "
                        f"周线首红:{s['weekly_first']} 量比:{s['vol_ratio']} "
                        f"RSI:{s['rsi']} J:{s['j_val']}\n")
        print(f"结果已保存至: {path}")

    def print_console_results(self, results):
        print("\n" + "=" * 130)
        print(f"{BOLD}{CYAN}【最终选股结果】日周交叉首红(日线≥3)（按涨幅排序）- 共 {len(results)} 只{RESET}")
        print("=" * 130)
        if not results:
            print("无符合条件股票")
            return
        for i, s in enumerate(results, 1):
            color = RED if i % 2 == 1 else YELLOW
            print(f"\n{color}{BOLD}{'─' * 120}{RESET}")
            print(f"{color}{BOLD}  {i:2d}. {s['ts_code']}  {s['name']:<12}{RESET}")
            print(f"{color}      现价: {s['current_price']:8.2f}    涨幅: {s['pct_chg']:+7.2f}%    信号: {s['signal']}    量比: {s['vol_ratio']:.2f}{RESET}")
            print(f"{color}      日线近3日首红({s['daily_cnt']}个): {s['daily_first']:<28}{RESET}")
            print(f"{color}      周线本周首红: {s['weekly_first']:<28}{RESET}")
            print(f"{color}      日BBI: {s['bbi']:.2f}    日肯特纳: {s['kc_mid']:.2f}    周BBI: {s['w_bbi']:.2f}{RESET}")
            print(f"{color}      MACD柱: {s['macd_bar']:.4f}    KDJ柱: {s['kdj_bar']:.4f}    SAR: {s['sar']:.2f}{RESET}")
            print(f"{color}      RSI: {s['rsi']:.2f}    J: {s['j_val']:.2f}{RESET}")
            time.sleep(0.06)
        print(f"\n{GREEN}{BOLD}{'=' * 130}{RESET}")
        print(f"{GREEN}{BOLD}选股完成，以上共 {len(results)} 只股票符合全部条件{RESET}")
        print(f"{GREEN}{BOLD}{'=' * 130}{RESET}\n")

def main():
    print("=" * 110)
    print(f"{BOLD} 日周交叉首红共振(日线≥3) 量化选股脚本 【公共缓存加速版】{RESET}")
    print("=" * 110)
    filter_tool = CustomStrategyFilter(GlobalConfig.TUSHARE_TOKEN)
    filter_tool.set_level(1)

    date_input = input("\n输入T日日期(YYYYMMDD)，回车使用今天: ").strip()
    validated_date = filter_tool.validate_trade_date(date_input)
    print(f"使用交易日: {validated_date}")
    print(f"公共缓存目录: {GlobalConfig.CACHE_DIR}")
    print(f"选股结果目录: {GlobalConfig.OUTPUT_DIR}")

    now = datetime.now()
    is_today = (validated_date == now.strftime('%Y%m%d'))
    trading_time = is_today and (now.weekday() < 5) and ((9 <= now.hour < 12) or (13 <= now.hour < 15))

    if trading_time:
        print("\n当前为交易时间，是否使用实时盘中数据？")
        mode_input = input("请输入 1=收盘模式  2=实时盘中模式 (默认2): ").strip()
        is_intraday = (mode_input != '1')
    else:
        is_intraday = False
        print("当前日期非交易日或不在交易时间，使用历史数据")

    results = filter_tool.filter_market_stocks(validated_date, is_intraday)
    filter_tool.save_results(validated_date, results, is_intraday)
    filter_tool.print_console_results(results)

if __name__ == '__main__':
    main()