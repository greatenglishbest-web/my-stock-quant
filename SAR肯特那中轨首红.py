# -*- coding: utf-8 -*-
"""
双策略起爆系统 (前复权极速版)
==========================================================
【策略一】底部分型起爆：SAR首红 + 肯特纳首向上 + 动能共振反转
【策略二】底部反包起爆：前日SAR红+破中轨 + 今日穿中轨+KDJ多头+AVWAP3/5拐头

数据源：全部转换为前复权价格计算
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


def norm_date(d):
    if d is None or (isinstance(d, float) and np.isnan(d)):
        return ''
    s = str(d).strip().replace('-', '').replace('/', '').replace('.0', '').replace(' ', '')
    return s[:8] if len(s) >= 8 else s


class GlobalConfig:
    TUSHARE_TOKEN = 'a91652800def39d213abec3af9563e4485e83940bfbbd8276f8adaa8'
    OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', '底部分型起爆_选股结果')
    CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股日线公共缓存')
    LOOKBACK_DAYS = 500
    USE_PARQUET = True


class BottomReversalFilter:
    def __init__(self, token):
        self.pro = ts.pro_api(token)
        self.rt_data_dict = {}
        os.makedirs(GlobalConfig.CACHE_DIR, exist_ok=True)
        os.makedirs(GlobalConfig.OUTPUT_DIR, exist_ok=True)
        self.use_parquet = GlobalConfig.USE_PARQUET
        
        self._stock_pool_cache = None
        self._trade_dates_cache = {}

    # ---------------- 基础工具 ----------------
    def _call_with_retry(self, api_func, **kwargs):
        retries = 0
        while retries < 5:
            try:
                return api_func(**kwargs)
            except Exception as e:
                if any(kw in str(e).lower() for kw in
                       ["限流", "频率超限", "max retries", "connection", "timeout", "500", "502", "503", "429"]):
                    retries += 1
                    time.sleep(2 + retries)
                else:
                    raise
        raise Exception(f"API调用失败: {kwargs}")

    def validate_trade_date(self, input_date=''):
        if not input_date:
            input_date = datetime.now().strftime('%Y%m%d')
        input_date = norm_date(input_date)
        try:
            cal = self.pro.trade_cal(start_date=input_date, end_date=input_date)
            if not cal.empty and int(cal['is_open'].iloc[0]) == 1:
                return input_date
        except:
            pass
        date_obj = datetime.strptime(input_date, '%Y%m%d')
        for i in range(1, 40):
            prev = (date_obj - timedelta(days=i)).strftime('%Y%m%d')
            try:
                cal = self.pro.trade_cal(start_date=prev, end_date=prev)
                if not cal.empty and int(cal['is_open'].iloc[0]) == 1:
                    return prev
            except:
                continue
        return datetime.now().strftime('%Y%m%d')

    def get_trade_dates(self, end_date, n_days=500):
        cache_key = f"{end_date}_{n_days}"
        if cache_key in self._trade_dates_cache:
            return self._trade_dates_cache[cache_key]

        end_date = norm_date(end_date)
        start_approx = (datetime.strptime(end_date, '%Y%m%d')
                        - timedelta(days=int(n_days * 1.8))).strftime('%Y%m%d')
        cal = self._call_with_retry(self.pro.trade_cal, start_date=start_approx,
                                    end_date=end_date, is_open='1')
        dates = sorted([norm_date(d) for d in cal['cal_date'].tolist()])
        res = dates[-n_days:] if len(dates) >= n_days else dates
        self._trade_dates_cache[cache_key] = res
        return res

    # ---------------- 缓存 ----------------
    def _cache_path(self, trade_date):
        trade_date = norm_date(trade_date)
        ext = 'parquet' if self.use_parquet else 'csv'
        return os.path.join(GlobalConfig.CACHE_DIR, f"{trade_date}.{ext}")

    def save_day_to_cache(self, trade_date, df):
        if df is None or df.empty:
            return
        trade_date = norm_date(trade_date)
        path = self._cache_path(trade_date)
        try:
            df = df.copy()
            df['trade_date'] = trade_date
            if self.use_parquet:
                df.to_parquet(path, index=False)
            else:
                df.to_csv(path, index=False, encoding='utf-8-sig')
        except:
            pass

    def load_day_from_cache(self, trade_date):
        trade_date = norm_date(trade_date)
        path = self._cache_path(trade_date)
        if not os.path.exists(path):
            return None
        try:
            df = pd.read_parquet(path) if self.use_parquet else pd.read_csv(path)
            if df is None or df.empty:
                return None
            
            if 'adj_factor' not in df.columns:
                return None

            df['trade_date'] = trade_date
            for col in ['open', 'close', 'pct_chg', 'high', 'low', 'vol', 'amount', 'adj_factor']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            return df
        except:
            return None

    def get_cached_dates(self):
        pattern = os.path.join(GlobalConfig.CACHE_DIR, '*.parquet' if self.use_parquet else '*.csv')
        dates = set()
        for f in glob.glob(pattern):
            d = norm_date(os.path.basename(f).split('.')[0])
            if len(d) == 8 and d.isdigit():
                dates.add(d)
        return dates

    # ---------------- 股票池 ----------------
    def get_stock_pool(self):
        if self._stock_pool_cache is not None:
            return self._stock_pool_cache

        print("正在加载股票池（排除ST/北交所 + 上市≥60天）...")
        df = self.pro.stock_basic(list_status='L', fields='ts_code,name,list_date')
        today = datetime.now().strftime('%Y%m%d')
        filtered = []
        for _, row in df.iterrows():
            name, code = str(row['name']), row['ts_code']
            if name.startswith('S') or 'ST' in name or name.startswith('*'):
                continue
            if code.endswith('.BJ') or code.startswith('8') or code.startswith('4') or code.startswith('92'):
                continue
            list_date = norm_date(row['list_date'])
            if len(list_date) == 8:
                days = (datetime.strptime(today, '%Y%m%d') - datetime.strptime(list_date, '%Y%m%d')).days
                if days > 60:
                    filtered.append(row.to_dict())
        
        self._stock_pool_cache = pd.DataFrame(filtered)
        print(f"股票池加载完成，共 {len(self._stock_pool_cache)} 只")
        return self._stock_pool_cache

    # ---------------- 实时行情 ----------------
    def fetch_rt_k_batch(self):
        self.rt_data_dict = {}
        patterns = ['60*.SH', '688*.SH', '00*.SZ', '002*.SZ', '003*.SZ', '300*.SZ', '301*.SZ']
        rt_dfs = []
        print("获取实时行情（盘中模式）...")
        for pattern in patterns:
            try:
                df = self._call_with_retry(self.pro.rt_k, ts_code=pattern)
                if not df.empty:
                    rt_dfs.append(df)
                time.sleep(0.4)
            except:
                pass
        if rt_dfs:
            full_df = pd.concat(rt_dfs, ignore_index=True).drop_duplicates(subset=['ts_code'])
            self.rt_data_dict = full_df.set_index('ts_code').to_dict('index')
            print(f"实时数据获取完成，共 {len(self.rt_data_dict)} 只")

    # ---------------- 预筛（当日红盘上涨K线） ----------------
    def get_candidate_stocks(self, trade_date, is_intraday=False):
        trade_date = norm_date(trade_date)
        all_stocks = self.get_stock_pool()
        strong = []
        print("正在预筛：当日红盘上涨K线 (T_RED)...")
        
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
                    if pre_close > 0 and close > open_p and close > pre_close:
                        strong.append(row.to_dict())
                except:
                    continue
        else:
            try:
                df = self.load_day_from_cache(trade_date)
                if df is None or df.empty:
                    df = self._call_with_retry(
                        self.pro.daily, trade_date=trade_date,
                        fields='ts_code,trade_date,open,close,pct_chg,high,low,vol,amount')
                    if not df.empty:
                        adj_df = self._call_with_retry(
                            self.pro.adj_factor, trade_date=trade_date,
                            fields='ts_code,trade_date,adj_factor')
                        if not adj_df.empty:
                            df = pd.merge(df, adj_df, on=['ts_code', 'trade_date'], how='left')
                            df['adj_factor'] = df['adj_factor'].fillna(1.0)
                        else:
                            df['adj_factor'] = 1.0
                        self.save_day_to_cache(trade_date, df)
                        
                if not df.empty:
                    mask = (df['close'] > df['open']) & (df['pct_chg'] > 0)
                    codes = set(df.loc[mask, 'ts_code'].tolist())
                    strong = all_stocks[all_stocks['ts_code'].isin(codes)].to_dict('records')
                else:
                    print(f"{YELLOW}警告：未获取到 {trade_date} 的任何交易数据！{RESET}")
            except Exception as e:
                print(f"预筛失败: {e}")
        strong_df = pd.DataFrame(strong)
        print(f"预筛完成：共 {len(strong_df)} 只")
        return strong_df

    # ---------------- 历史日线加载 ----------------
    def batch_fetch_daily_history(self, trade_date, stock_codes):
        trade_date = norm_date(trade_date)
        print(f"\n{CYAN}准备日线（读取公共缓存）...{RESET}")
        trade_dates = self.get_trade_dates(trade_date, GlobalConfig.LOOKBACK_DAYS)
        if trade_date not in trade_dates:
            trade_dates.append(trade_date)
            trade_dates = sorted(set(trade_dates))

        need = []
        for d in trade_dates:
            p = self._cache_path(d)
            is_valid = False
            if os.path.exists(p):
                try:
                    cols = pd.read_parquet(p).columns if self.use_parquet else pd.read_csv(p, nrows=1).columns
                    if 'adj_factor' in cols:
                        is_valid = True
                except:
                    pass
            
            if not is_valid:
                need.append(d)

        if need:
            print(f"发现 {len(need)} 天数据缺失或为旧版(无复权)，极速精准补齐中...")
            for d in tqdm(need, desc="下载缺失日线"):
                try:
                    df = self._call_with_retry(
                        self.pro.daily, trade_date=d,
                        fields='ts_code,trade_date,open,high,low,close,vol,amount,pct_chg')
                    if not df.empty:
                        adj_df = self._call_with_retry(
                            self.pro.adj_factor, trade_date=d,
                            fields='ts_code,trade_date,adj_factor')
                        if not adj_df.empty:
                            df = pd.merge(df, adj_df, on=['ts_code', 'trade_date'], how='left')
                            df['adj_factor'] = df['adj_factor'].fillna(1.0)
                        else:
                            df['adj_factor'] = 1.0
                        self.save_day_to_cache(d, df)
                    time.sleep(0.15)
                except:
                    pass

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
        full = pd.concat(all_dfs, ignore_index=True)
        full['trade_date'] = full['trade_date'].map(norm_date)
        full = full.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
        return {code: g.reset_index(drop=True) for code, g in full.groupby('ts_code')}

    # ---------------- TDX 的 SAR(10,2,20) 算法 ----------------
    def calc_tdx_sar(self, high, low, n=10, step=2, max_step=20):
        length = len(high)
        sar = np.zeros(length)
        trend = np.zeros(length)
        ep = np.zeros(length)
        af = np.zeros(length)
        
        af_step = step / 100.0
        af_max = max_step / 100.0
        
        if high.iloc[1] > high.iloc[0]:
            trend[0] = 1
            sar[0] = low.iloc[0]
            ep[0] = high.iloc[1]
            af[0] = af_step
        else:
            trend[0] = -1
            sar[0] = high.iloc[0]
            ep[0] = low.iloc[1]
            af[0] = af_step
            
        for i in range(1, length):
            sar[i] = sar[i-1] + af[i-1] * (ep[i-1] - sar[i-1])
            
            if trend[i-1] == 1:
                if low.iloc[i] < sar[i]:
                    trend[i] = -1
                    sar[i] = max(ep[i-1], high.iloc[i])
                    ep[i] = low.iloc[i]
                    af[i] = af_step
                else:
                    trend[i] = 1
                    if i >= 2:
                        sar[i] = min(sar[i], low.iloc[i-1], low.iloc[i-2])
                    else:
                        sar[i] = min(sar[i], low.iloc[i-1])
                    ep[i] = max(ep[i-1], high.iloc[i])
                    af[i] = min(af[i-1] + af_step, af_max)
            else:
                if high.iloc[i] > sar[i]:
                    trend[i] = 1
                    sar[i] = min(ep[i-1], low.iloc[i])
                    ep[i] = high.iloc[i]
                    af[i] = af_step
                else:
                    trend[i] = -1
                    if i >= 2:
                        sar[i] = max(sar[i], high.iloc[i-1], high.iloc[i-2])
                    else:
                        sar[i] = max(sar[i], high.iloc[i-1])
                    ep[i] = min(ep[i-1], low.iloc[i])
                    af[i] = min(af[i-1] + af_step, af_max)
                    
        return pd.Series(sar, index=high.index)

    # ---------------- TDX 的 SMA(X,N,M) 算法 (用于 KDJ) ----------------
    def TDX_SMA(self, series, n, m):
        arr = series.values
        res = np.zeros(len(arr))
        for i in range(len(arr)):
            val = arr[i]
            if pd.isna(val) or np.isinf(val):
                res[i] = np.nan
            elif i == 0 or pd.isna(res[i - 1]):
                res[i] = val
            else:
                res[i] = (m * val + (n - m) * res[i - 1]) / n
        return pd.Series(res, index=series.index)

    # ---------------- 单只股票的核心逻辑 ----------------
    def process_stock_from_df(self, stock_info, df_daily, trade_date, is_intraday=False):
        ts_code = stock_info['ts_code']
        name = stock_info.get('name', '')
        trade_date = norm_date(trade_date)
        try:
            df_daily = df_daily.copy()
            df_daily['trade_date'] = df_daily['trade_date'].map(norm_date)
            df_daily = df_daily[df_daily['trade_date'] <= trade_date]
            df_daily = df_daily.sort_values('trade_date').reset_index(drop=True)

            if len(df_daily) < 30:
                return [], 'data_short'

            # ============ 实时行情拼接 ============
            if is_intraday and ts_code in self.rt_data_dict:
                rt = self.rt_data_dict[ts_code]
                last_date = str(df_daily.iloc[-1]['trade_date'])
                try:
                    rt_open = float(rt.get('open', 0) or 0)
                    rt_high = float(rt.get('high', 0) or 0)
                    rt_low = float(rt.get('low', 0) or 0)
                    rt_close = float(rt.get('close', 0) or 0)
                    rt_vol = float(rt.get('vol', 0) or 0)
                    rt_amt = float(rt.get('amount', 0) or 0)
                    rt_pre = float(rt.get('pre_close', rt_close) or rt_close)
                except Exception:
                    rt_open = rt_high = rt_low = rt_close = rt_vol = rt_amt = 0.0
                    rt_pre = 0.0

                if rt_close > 0 and rt_open > 0:
                    new_row = {
                        'ts_code': ts_code, 'trade_date': trade_date,
                        'open': rt_open, 'high': rt_high, 'low': rt_low, 'close': rt_close,
                        'vol': rt_vol / 100.0,          
                        'amount': rt_amt / 1000.0,      
                        'pct_chg': (rt_close / rt_pre - 1) * 100 if rt_pre > 0 else 0.0,
                        'adj_factor': 1.0
                    }
                    if last_date == trade_date:
                        idx = len(df_daily) - 1
                        for k, v in new_row.items():
                            df_daily.at[idx, k] = v
                    else:
                        prev_adj = df_daily.iloc[-1]['adj_factor'] if 'adj_factor' in df_daily.columns else 1.0
                        new_row['adj_factor'] = prev_adj
                        df_daily = pd.concat([df_daily, pd.DataFrame([new_row])], ignore_index=True)
            # ======================================================

            target_rows = df_daily.index[df_daily['trade_date'] == trade_date]
            if len(target_rows) == 0:
                return [], 'date_mismatch'
            target_idx = target_rows[-1]

            raw_open = df_daily.at[target_idx, 'open']
            if pd.isna(raw_open) or float(raw_open) <= 0:
                return [], 'suspended'

            # ================= 【核心：前复权转换】 =================
            if 'adj_factor' not in df_daily.columns:
                df_daily['adj_factor'] = 1.0
                
            base_adj = df_daily.at[target_idx, 'adj_factor']
            if pd.isna(base_adj) or base_adj == 0:
                base_adj = 1.0
                
            O = pd.to_numeric(df_daily['open'], errors='coerce').ffill().bfill() * df_daily['adj_factor'] / base_adj
            C = pd.to_numeric(df_daily['close'], errors='coerce').ffill().bfill() * df_daily['adj_factor'] / base_adj
            H = pd.to_numeric(df_daily['high'], errors='coerce').ffill().bfill() * df_daily['adj_factor'] / base_adj
            L = pd.to_numeric(df_daily['low'], errors='coerce').ffill().bfill() * df_daily['adj_factor'] / base_adj
            V = pd.to_numeric(df_daily['vol'], errors='coerce').ffill().bfill()
            AMT = pd.to_numeric(df_daily['amount'], errors='coerce').ffill().bfill()  # 千元
            # =======================================================

            # ============ 通用基础指标计算 ============
            PREV_DN = (C.shift(1) < O.shift(1)) | (C.shift(1) < C.shift(2))
            T_RED = (C > O) & (C > C.shift(1))
            VOL_RATIO = V / V.rolling(5).mean().shift(1).replace(0, np.nan) > 1.1
            
            KCM = C.ewm(span=20, adjust=False).mean()
            
            SAR_VAL = self.calc_tdx_sar(H, L, 10, 2, 20)
            SAR_RED = C > SAR_VAL
            SAR_GREEN = C < SAR_VAL

            DIF = C.ewm(span=12, adjust=False).mean() - C.ewm(span=26, adjust=False).mean()
            DEA = DIF.ewm(span=9, adjust=False).mean()
            MACD = (DIF - DEA) * 2

            LLV14 = L.rolling(14).min()
            HHV14 = H.rolling(14).max()
            RSV14 = (C - LLV14) / (HHV14 - LLV14 + 0.0001) * 100
            K_VAL = self.TDX_SMA(RSV14, 3, 1)
            D_VAL = self.TDX_SMA(K_VAL, 3, 1)
            KDJ_BAR = (K_VAL - D_VAL) * 2

            # AVWAP 计算 (乘以复权因子映射为前复权价)
            VWAP3_raw = AMT.rolling(3).sum() / (V.rolling(3).sum() * 10)
            VWAP5_raw = AMT.rolling(5).sum() / (V.rolling(5).sum() * 10)
            AVWAP3 = VWAP3_raw * df_daily['adj_factor'] / base_adj
            AVWAP5 = VWAP5_raw * df_daily['adj_factor'] / base_adj


            # ==========================================
            # 【策略一】底部分型起爆
            # ==========================================
            T_COND_1 = T_RED & VOL_RATIO

            KCM_UP = KCM > KCM.shift(1)
            KCM_COND_1 = KCM_UP & (KCM_UP.rolling(3).sum() == 1)

            SAR_COND_1 = SAR_RED & (SAR_RED.rolling(3).sum() == 1)
            SAR_GREEN_COND_1 = SAR_GREEN.rolling(10).sum() >= 5

            NO_GAP_UP_1 = O <= C.shift(1) * 1.03
            PREV_5_GAIN_1 = C.shift(1) < C.shift(6) * 1.05
            
            HAS_4_RED_1 = ((C > O).rolling(4).sum() == 4).shift(1) | ((C > O).rolling(4).sum() == 4).shift(2)
            HAS_4_UP_1 = ((C > C.shift(1)).rolling(4).sum() == 4).shift(1) | ((C > C.shift(1)).rolling(4).sum() == 4).shift(2)
            NO_4_RED_UP_1 = ~HAS_4_RED_1 & ~HAS_4_UP_1
            
            ADD_COND_1 = NO_GAP_UP_1 & PREV_5_GAIN_1 & NO_4_RED_UP_1

            T_MACD_UP = MACD > 0
            MACD_FIRST_RED = T_MACD_UP & ((MACD > 0).rolling(3).sum() == 1)
            GRN_SHRINK_20 = (MACD < 0) & (MACD.shift(1) < 0) & (MACD.abs() <= MACD.shift(1).abs() * 0.8)
            MACD_COND_1 = MACD_FIRST_RED | GRN_SHRINK_20

            KDJ_UP = KDJ_BAR > 0
            KDJ_FIRST_RED = KDJ_UP & (KDJ_UP.rolling(3).sum() == 1)
            KDJ_SHRINK_20 = (KDJ_BAR < 0) & (KDJ_BAR.shift(1) < 0) & (KDJ_BAR.abs() <= KDJ_BAR.shift(1).abs() * 0.8)
            KDJ_COND_1 = KDJ_FIRST_RED | KDJ_SHRINK_20

            SIGNAL_1 = PREV_DN & T_COND_1 & KCM_COND_1 & SAR_COND_1 & SAR_GREEN_COND_1 & ADD_COND_1 & MACD_COND_1 & KDJ_COND_1


            # ==========================================
            # 【策略二】底部反包起爆
            # ==========================================
            PREV_BELOW_KCM_2 = L.shift(1) < KCM.shift(1)
            PREV_SAR_RED_2 = SAR_RED.shift(1)
            PREV_COND_2 = PREV_DN & PREV_BELOW_KCM_2 & PREV_SAR_RED_2

            PREV_5_GAIN_2 = C.shift(1) < C.shift(6) * 1.04

            RED_3 = (C > O).rolling(3).sum() == 3
            UP_3 = (C > C.shift(1)).rolling(3).sum() == 3
            HAS_3_RED_5 = RED_3.shift(1).rolling(5).sum() > 0
            HAS_3_UP_5 = UP_3.shift(1).rolling(5).sum() > 0
            NO_3_RED_UP_2 = ~HAS_3_RED_5 & ~HAS_3_UP_5

            T_COND_2 = T_RED & VOL_RATIO

            KCM_INC_2 = (C >= KCM) & (O <= KCM)

            KDJ_BULL = K_VAL > D_VAL
            MACD_COND_2 = MACD > 0

            # 新增 AVWAP 相关条件
            PREV_AW_DN = (AVWAP3.shift(1) < AVWAP3.shift(2)) | (AVWAP5.shift(1) < AVWAP5.shift(2))
            CURR_AW_UP = (AVWAP3 > AVWAP3.shift(1)) & (AVWAP5 > AVWAP5.shift(1))
            SLOPE_COND = (AVWAP3 - AVWAP3.shift(1)) > 0.20  # 斜率(变化量) > 0.20

            AWAP_COND = PREV_AW_DN & CURR_AW_UP & SLOPE_COND

            SIGNAL_2 = PREV_COND_2 & PREV_5_GAIN_2 & NO_3_RED_UP_2 & T_COND_2 & KCM_INC_2 & KDJ_BULL & MACD_COND_2 & AWAP_COND


            # ==========================================
            # 提取公共指标
            # ==========================================
            last_row = df_daily.iloc[target_idx]
            pct_chg = 0.0
            if 'pct_chg' in df_daily.columns and not pd.isna(last_row['pct_chg']):
                pct_chg = float(last_row['pct_chg'])
                
            vol_ma5_prev = V.iloc[target_idx-5:target_idx].mean()
            vol_ratio_val = round(float(V.iloc[target_idx] / vol_ma5_prev), 2) if vol_ma5_prev > 0 else 0.0
            macd_val = round(float(MACD.iloc[target_idx]), 3)
            k_val = round(float(K_VAL.iloc[target_idx]), 2)
            d_val = round(float(D_VAL.iloc[target_idx]), 2)
            kcm_val = round(float(KCM.iloc[target_idx]), 2)
            
            avwap3_val = round(float(AVWAP3.iloc[target_idx]), 2)
            avwap5_val = round(float(AVWAP5.iloc[target_idx]), 2)
            slope_val = round(float(AVWAP3.iloc[target_idx] - AVWAP3.iloc[target_idx-1]), 3) if target_idx > 0 else 0.0

            res_list = []

            if bool(SIGNAL_1.iloc[target_idx]):
                res_list.append({
                    'strategy': '策略一',
                    'ts_code': ts_code,
                    'name': name,
                    'close': round(float(last_row['close']), 2),
                    'pct_chg': round(pct_chg, 2),
                    'vol_ratio': vol_ratio_val,
                    'macd': macd_val,
                    'k': k_val,
                    'd': d_val,
                    'kcm': kcm_val,
                    'avwap3': avwap3_val,
                    'avwap5': avwap5_val,
                    'slope': slope_val
                })

            if bool(SIGNAL_2.iloc[target_idx]):
                res_list.append({
                    'strategy': '策略二',
                    'ts_code': ts_code,
                    'name': name,
                    'close': round(float(last_row['close']), 2),
                    'pct_chg': round(pct_chg, 2),
                    'vol_ratio': vol_ratio_val,
                    'macd': macd_val,
                    'k': k_val,
                    'd': d_val,
                    'kcm': kcm_val,
                    'avwap3': avwap3_val,
                    'avwap5': avwap5_val,
                    'slope': slope_val
                })

            if len(res_list) > 0:
                return res_list, 'selected'
            
            return [], 'condition_fail'

        except Exception as e:
            return [], 'exception'

    # ---------------- 主流程 ----------------
    def filter_market_stocks(self, trade_date, is_intraday=False):
        trade_date = norm_date(trade_date)
        stocks = self.get_candidate_stocks(trade_date, is_intraday)
        if len(stocks) == 0:
            return []
        if is_intraday and not self.rt_data_dict:
            self.fetch_rt_k_batch()

        stock_codes = stocks['ts_code'].tolist()
        daily_dict = self.batch_fetch_daily_history(trade_date, stock_codes)

        selected = []
        stats = {'selected': 0, 'data_short': 0, 'date_mismatch': 0,
                 'condition_fail': 0, 'exception': 0, 'no_data': 0, 'suspended': 0}

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
                    self.process_stock_from_df, info, ddf, trade_date, is_intraday)] = code

            for future in tqdm(as_completed(futures), total=len(futures), desc="选股进度"):
                res_list, reason = future.result()
                if res_list:
                    selected.extend(res_list)
                    stats['selected'] += len(res_list)
                else:
                    stats[reason] = stats.get(reason, 0) + 1

        selected = sorted(selected, key=lambda x: x.get('pct_chg', 0), reverse=True)
        print(f"\n{GREEN}筛选完成！共 {len(selected)} 条信号{RESET}")
        for k, v in stats.items():
            print(f"  {k}: {v}")
        return selected

    def save_results(self, trade_date, results, is_intraday=False):
        if not results:
            return
        mode = "_实时" if is_intraday else ""
        path = os.path.join(GlobalConfig.OUTPUT_DIR, f"双策略起爆_{trade_date}{mode}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"双策略起爆(前复权)：底部分型+底部反包 - {trade_date}\n")
            f.write(f"共选中 {len(results)} 条信号\n\n")
            for i, s in enumerate(results, 1):
                f.write(f"{i:3d}. [{s['strategy']}] {s['ts_code']} {s['name']} 价:{s['close']:.2f} 涨:{s['pct_chg']:+.2f}% "
                        f"量比:{s['vol_ratio']:.2f} MACD:{s['macd']:.3f} K:{s['k']:.2f} D:{s['d']:.2f} "
                        f"KCM:{s['kcm']:.2f} AVWAP3:{s['avwap3']:.2f} AVWAP5:{s['avwap5']:.2f} 斜率:{s['slope']:.3f}\n")
        print(f"结果已保存: {path}")


def main():
    print("=" * 110)
    print(f"{BOLD} 双策略起爆：底部分型 + 底部反包 (前复权版) {RESET}")
    print("=" * 110)
    filter_tool = BottomReversalFilter(GlobalConfig.TUSHARE_TOKEN)

    date_input = input("\n输入T日日期(YYYYMMDD)，回车使用今天: ").strip()
    validated_date = filter_tool.validate_trade_date(date_input)
    print(f"使用交易日: {validated_date}")

    now = datetime.now()
    is_today = (validated_date == now.strftime('%Y%m%d'))
    trading_time = is_today and (now.weekday() < 5) and ((9 <= now.hour < 12) or (13 <= now.hour < 15))
    
    is_intraday = False
    if trading_time:
        mode_input = input("\n1=收盘模式(单次)  2=实时盘中(每15分钟自动扫描) [默认2]: ").strip()
        is_intraday = (mode_input != '1')
    else:
        is_intraday = False
        print("非交易时段，使用历史数据 (单次运行)")

    def run_scan():
        results = filter_tool.filter_market_stocks(validated_date, is_intraday)
        filter_tool.save_results(validated_date, results, is_intraday)

        if results:
            print("\n" + "=" * 120)
            print(f"{BOLD}{CYAN}【结果】双策略起爆 - {validated_date} - 共 {len(results)} 条信号{RESET}")
            for i, s in enumerate(results, 1):
                color = RED if s['strategy'] == '策略一' else YELLOW
                print(f"\n{color}{BOLD}  {i:2d}. [{s['strategy']}] {s['ts_code']} {s['name']:<8} "
                      f"价:{s['close']:8.2f} 涨:{s['pct_chg']:+6.2f}% 量比:{s['vol_ratio']:.2f}{RESET}")
                print(f"       MACD:{s['macd']:.3f}  K:{s['k']:.2f}  D:{s['d']:.2f}  KCM:{s['kcm']:.2f}  "
                      f"AVWAP3:{s['avwap3']:.2f}  AVWAP5:{s['avwap5']:.2f}  斜率:{s['slope']:.3f}")
        else:
            print(f"\n{YELLOW}本次未选出股票，请检查条件或换日期重试。{RESET}")

    if is_intraday:
        print(f"\n{BOLD}{GREEN}>>> 已开启 15分钟自动扫描任务。请保持窗口开启，按 Ctrl+C 可强制中止。{RESET}")
        try:
            while True:
                cur_now = datetime.now()
                if not (cur_now.weekday() < 5 and ((9 <= cur_now.hour < 12) or (13 <= cur_now.hour < 15))):
                    print(f"\n{YELLOW}当前时间已超过交易时段，自动扫描任务优雅结束。{RESET}")
                    break
                    
                print(f"\n{BOLD}{CYAN}========== 开始自动扫描 [{cur_now.strftime('%Y-%m-%d %H:%M:%S')}] =========={RESET}")
                run_scan()
                
                print(f"\n{CYAN}>>> 本轮扫描结束，{BOLD}程序将休眠 15 分钟{RESET}{CYAN}，等待下一次扫描...{RESET}")
                time.sleep(15 * 60)
                
        except KeyboardInterrupt:
            print(f"\n\n{YELLOW}检测到 Ctrl+C，已手动终止自动扫描任务。{RESET}")
    else:
        run_scan()


if __name__ == '__main__':
    main()