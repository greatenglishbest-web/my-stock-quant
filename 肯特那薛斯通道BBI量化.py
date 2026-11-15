# -*- coding: utf-8 -*-
"""
多通道极值共振 + AVWAP3斜率突破版 (Python量化完美修Bug版)
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
    if d is None or (isinstance(d, float) and np.isnan(d)): return ''
    s = str(d).strip().replace('-', '').replace('/', '').replace('.0', '').replace(' ', '')
    return s[:8] if len(s) >= 8 else s

def barslast(cond):
    cond = cond.fillna(False)
    res = np.zeros(len(cond))
    count = 0
    for i in range(len(cond)):
        if cond.iloc[i]:
            count = 0
        else:
            count += 1
        res[i] = count
    return pd.Series(res, index=cond.index)

class GlobalConfig:
    TUSHARE_TOKEN = 'a91652800def39d213abec3af9563e4485e83940bfbbd8276f8adaa8'
    OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', '多通道AVWAP突破_选股结果')
    CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股日线公共缓存')
    LOOKBACK_DAYS = 500
    USE_PARQUET = True

class MultiChannelFilter:
    def __init__(self, token):
        self.pro = ts.pro_api(token)
        self.rt_data_dict = {}
        os.makedirs(GlobalConfig.CACHE_DIR, exist_ok=True)
        os.makedirs(GlobalConfig.OUTPUT_DIR, exist_ok=True)
        self.use_parquet = GlobalConfig.USE_PARQUET

    def _call_with_retry(self, api_func, **kwargs):
        retries = 0
        while retries < 5:
            try:
                return api_func(**kwargs)
            except Exception as e:
                if any(kw in str(e).lower() for kw in ["限流", "频率超限", "max retries", "connection", "timeout", "500", "502", "503", "429"]):
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
        except: pass
        date_obj = datetime.strptime(input_date, '%Y%m%d')
        for i in range(1, 40):
            prev = (date_obj - timedelta(days=i)).strftime('%Y%m%d')
            try:
                cal = self.pro.trade_cal(start_date=prev, end_date=prev)
                if not cal.empty and int(cal['is_open'].iloc[0]) == 1:
                    return prev
            except: continue
        return datetime.now().strftime('%Y%m%d')

    def get_trade_dates(self, end_date, n_days=500):
        end_date = norm_date(end_date)
        start_approx = (datetime.strptime(end_date, '%Y%m%d') - timedelta(days=int(n_days * 1.8))).strftime('%Y%m%d')
        cal = self._call_with_retry(self.pro.trade_cal, start_date=start_approx, end_date=end_date, is_open='1')
        dates = sorted([norm_date(d) for d in cal['cal_date'].tolist()])
        return dates[-n_days:] if len(dates) >= n_days else dates

    def _cache_path(self, trade_date):
        trade_date = norm_date(trade_date)
        ext = 'parquet' if self.use_parquet else 'csv'
        return os.path.join(GlobalConfig.CACHE_DIR, f"{trade_date}.{ext}")

    def save_day_to_cache(self, trade_date, df):
        if df is None or df.empty: return
        trade_date = norm_date(trade_date)
        path = self._cache_path(trade_date)
        try:
            df = df.copy()
            df['trade_date'] = trade_date
            if self.use_parquet: df.to_parquet(path, index=False)
            else: df.to_csv(path, index=False, encoding='utf-8-sig')
        except: pass

    def load_day_from_cache(self, trade_date):
        trade_date = norm_date(trade_date)
        path = self._cache_path(trade_date)
        if not os.path.exists(path): return None
        try:
            df = pd.read_parquet(path) if self.use_parquet else pd.read_csv(path)
            if df is None or df.empty: return None
            df['trade_date'] = trade_date
            for col in ['open', 'close', 'pct_chg', 'high', 'low', 'vol', 'amount']:
                if col in df.columns: df[col] = pd.to_numeric(df[col], errors='coerce')
            return df
        except: return None

    def get_cached_dates(self):
        pattern = os.path.join(GlobalConfig.CACHE_DIR, '*.parquet' if self.use_parquet else '*.csv')
        dates = set()
        for f in glob.glob(pattern):
            d = norm_date(os.path.basename(f).split('.')[0])
            if len(d) == 8 and d.isdigit(): dates.add(d)
        return dates

    def get_stock_pool(self):
        print("正在加载股票池（排除ST/北交所 + 上市≥60天）...")
        df = self.pro.stock_basic(list_status='L', fields='ts_code,name,list_date')
        today = datetime.now().strftime('%Y%m%d')
        filtered = []
        for _, row in df.iterrows():
            name, code = str(row['name']), row['ts_code']
            if 'ST' in name or code.endswith('.BJ') or code.startswith('8') or code.startswith('4') or code.startswith('92'): 
                continue
            list_date = norm_date(row['list_date'])
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
                time.sleep(0.4)
            except: pass
        if rt_dfs:
            full_df = pd.concat(rt_dfs, ignore_index=True).drop_duplicates(subset=['ts_code'])
            self.rt_data_dict = full_df.set_index('ts_code').to_dict('index')
            print(f"实时数据获取完成，共 {len(self.rt_data_dict)} 只")

    def get_candidate_stocks(self, trade_date, is_intraday=False):
        trade_date = norm_date(trade_date)
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
                    df = self._call_with_retry(self.pro.daily, trade_date=trade_date, fields='ts_code,trade_date,open,close,pct_chg,high,low,vol,amount')
                    if not df.empty: self.save_day_to_cache(trade_date, df)
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

    def batch_fetch_daily_history(self, trade_date, stock_codes):
        trade_date = norm_date(trade_date)
        print(f"\n{CYAN}准备日线（读取公共缓存）...{RESET}")
        trade_dates = self.get_trade_dates(trade_date, GlobalConfig.LOOKBACK_DAYS)
        if trade_date not in trade_dates:
            trade_dates.append(trade_date)
            trade_dates = sorted(set(trade_dates))

        cached = self.get_cached_dates()
        need = [d for d in trade_dates if d not in cached]
        if need:
            print(f"发现 {len(need)} 天缺失数据，极速补齐...")
            for d in need:
                try:
                    df = self._call_with_retry(self.pro.daily, trade_date=d, fields='ts_code,trade_date,open,high,low,close,vol,amount,pct_chg')
                    if not df.empty: self.save_day_to_cache(d, df)
                    time.sleep(0.12)
                except: pass

        all_dfs = []
        code_set = set(stock_codes)
        for d in tqdm(trade_dates, desc="加载日线"):
            df = self.load_day_from_cache(d)
            if df is not None and not df.empty:
                df = df[df['ts_code'].isin(code_set)]
                if not df.empty: all_dfs.append(df)

        if not all_dfs: return {}
        full = pd.concat(all_dfs, ignore_index=True)
        full['trade_date'] = full['trade_date'].map(norm_date)
        full = full.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
        return {code: g.reset_index(drop=True) for code, g in full.groupby('ts_code')}

    def process_stock_from_df(self, stock_info, df_daily, trade_date, is_intraday=False):
        ts_code = stock_info['ts_code']
        name = stock_info.get('name', '')
        trade_date = norm_date(trade_date)
        try:
            df_daily = df_daily.copy()
            df_daily['trade_date'] = df_daily['trade_date'].map(norm_date)
            df_daily = df_daily[df_daily['trade_date'] <= trade_date].reset_index(drop=True)
            
            if len(df_daily) < 60: return None, 'data_short'
            
            target_mask = df_daily['trade_date'] == trade_date
            if not target_mask.any(): return None, 'date_mismatch'
            
            target_idx = df_daily.index[df_daily['trade_date'] == trade_date][-1]
            
            if is_intraday and ts_code in self.rt_data_dict:
                rt = self.rt_data_dict[ts_code]
                df_daily.at[target_idx, 'close'] = float(rt['close'])
                df_daily.at[target_idx, 'high'] = max(float(df_daily.at[target_idx, 'high']), float(rt.get('high', 0)))
                df_daily.at[target_idx, 'low'] = min(float(df_daily.at[target_idx, 'low']), float(rt.get('low', 999999)))
                if float(rt.get('open', 0)) > 0: df_daily.at[target_idx, 'open'] = float(rt['open'])
                df_daily.at[target_idx, 'vol'] = float(rt.get('vol', df_daily.at[target_idx, 'vol']))
                df_daily.at[target_idx, 'amount'] = float(rt.get('amount', df_daily.at[target_idx, 'amount']))

            # ========== 动态前复权价格处理 ==========
            raw_open = pd.to_numeric(df_daily['open'], errors='coerce').ffill().bfill()
            raw_close = pd.to_numeric(df_daily['close'], errors='coerce').ffill().bfill()
            raw_high = pd.to_numeric(df_daily['high'], errors='coerce').ffill().bfill()
            raw_low = pd.to_numeric(df_daily['low'], errors='coerce').ffill().bfill()
            
            raw_pct = pd.to_numeric(df_daily['pct_chg'], errors='coerce').fillna(0) / 100
            adj_factor = np.ones(len(raw_close))
            adj_factor[-1] = 1.0
            for i in range(len(raw_close) - 2, -1, -1):
                adj_factor[i] = adj_factor[i+1] / (1 + raw_pct.iloc[i+1])
            
            O = raw_open * adj_factor
            C = raw_close * adj_factor
            H = raw_high * adj_factor
            L = raw_low * adj_factor
            
            V = pd.to_numeric(df_daily['vol'], errors='coerce').ffill().bfill()
            if 'amount' in df_daily.columns:
                AMT = pd.to_numeric(df_daily['amount'], errors='coerce').ffill().bfill() * 1000
            else:
                AMT = C * V * 100
            VOL_SHARES = V * 100

            # ========== 指标基础计算 ==========
            N = 102; M_val = 7; KCN = 20; KCM = 2
            
            # 薛斯通道
            AA = ((2*C + H + L) / 4).rolling(5).mean()
            XS1 = AA * N / 100
            XS2 = AA * (200 - N) / 100
            CC = ((2*C + H + L)/4 - C.rolling(20).mean()).abs() / C.rolling(20).mean()
            
            # 【核心修复】把 CC 的 NaN 用 0 填充，防止传染！
            CC = CC.fillna(0) 
            
            DD = np.zeros(len(C))
            DD[0] = C.iloc[0]
            CC_arr = CC.values
            C_arr = C.values
            for i in range(1, len(C)):
                DD[i] = CC_arr[i] * C_arr[i] + (1 - CC_arr[i]) * DD[i-1]
            DD = pd.Series(DD, index=C.index)
            XS3 = DD * (1 + M_val/100)
            XS4 = DD * (1 - M_val/100)
            
            # 肯特纳
            TR1 = pd.concat([H - L, (H - C.shift(1)).abs(), (L - C.shift(1)).abs()], axis=1).max(axis=1)
            ATR = TR1.rolling(KCN).mean()
            KC1 = C.rolling(KCN).mean()
            KC2 = KC1 + KCM * ATR
            KC3 = KC1 - KCM * ATR
            
            # BBI
            BBI = (C.rolling(3).mean() + C.rolling(6).mean() + C.rolling(12).mean() + C.rolling(24).mean()) / 4
            
            # ========== 通道方向 ==========
            U1 = (XS1 > XS1.shift(1)).fillna(False)
            U2 = (XS2 > XS2.shift(1)).fillna(False)
            U3 = (XS3 > XS3.shift(1)).fillna(False)
            U4 = (XS4 > XS4.shift(1)).fillna(False)
            U5 = (KC1 > KC1.shift(1)).fillna(False)
            U6 = (KC2 > KC2.shift(1)).fillna(False)
            U7 = (KC3 > KC3.shift(1)).fillna(False)
            U8 = (BBI > BBI.shift(1)).fillna(False)
            
            UPCNT = U1.astype(int) + U2.astype(int) + U3.astype(int) + U4.astype(int) + U5.astype(int) + U6.astype(int) + U7.astype(int) + U8.astype(int)
            UPOK = UPCNT >= 7
            
            # ========== BARSLAST ==========
            T1 = barslast(~U1)
            T2 = barslast(~U2)
            T3 = barslast(~U3)
            T4 = barslast(~U4)
            T5 = barslast(~U5)
            T6 = barslast(~U6)
            T7 = barslast(~U7)
            T8 = barslast(~U8)
            
            F1 = U1 & (T1 <= 3)
            F2 = U2 & (T2 <= 3)
            F3 = U3 & (T3 <= 3)
            F4 = U4 & (T4 <= 3)
            F5 = U5 & (T5 <= 3)
            F6 = U6 & (T6 <= 3)
            F7 = U7 & (T7 <= 3)
            F8 = U8 & (T8 <= 3)
            
            FCNT = F1.astype(int) + F2.astype(int) + F3.astype(int) + F4.astype(int) + F5.astype(int) + F6.astype(int) + F7.astype(int) + F8.astype(int)
            FOK = (FCNT * 10) >= (UPCNT * 8)
            
            # ========== 前置形态 ==========
            QZ = ((C.shift(1) < C.shift(2)) | (C.shift(1) < O.shift(1))).fillna(False)
            GCNT = (((C < O) | (C < C.shift(1))).rolling(5).sum() >= 2).fillna(False)
            
            N3R = (C > O) & (C.shift(1) > O.shift(1)) & (C.shift(2) > O.shift(2))
            N3U = (C > C.shift(1)) & (C.shift(1) > C.shift(2)) & (C.shift(2) > C.shift(3))
            N3 = (N3R.rolling(5).sum() == 0) & (N3U.rolling(5).sum() == 0)
            
            YX = C > O
            
            # ========== 实体包含 ==========
            I1 = (O <= XS1) & (XS1 <= C)
            I2 = (O <= XS2) & (XS2 <= C)
            I3 = (O <= XS3) & (XS3 <= C)
            I4 = (O <= XS4) & (XS4 <= C)
            I5 = (O <= KC1) & (KC1 <= C)
            I6 = (O <= KC2) & (KC2 <= C)
            I7 = (O <= KC3) & (KC3 <= C)
            I8 = (O <= BBI) & (BBI <= C)
            
            ICNT = I1.astype(int) + I2.astype(int) + I3.astype(int) + I4.astype(int) + I5.astype(int) + I6.astype(int) + I7.astype(int) + I8.astype(int)
            IOK = ICNT >= 3
            
            KCCNT = I5.astype(int) + I6.astype(int)
            KCOK = KCCNT >= 1
            
            GAO = O > C.shift(1) * 1.03
            NO_GAO = GAO.rolling(6).sum() == 0
            
            # ========== 斜率条件 ==========
            S1 = (XS1 - XS1.shift(1)) / XS1.shift(1) * 100 > 0.3
            S2 = (XS2 - XS2.shift(1)) / XS2.shift(1) * 100 > 0.3
            S3 = (XS3 - XS3.shift(1)) / XS3.shift(1) * 100 > 0.3
            S4 = (XS4 - XS4.shift(1)) / XS4.shift(1) * 100 > 0.3
            S5 = (KC1 - KC1.shift(1)) / KC1.shift(1) * 100 > 0.3
            S6 = (KC2 - KC2.shift(1)) / KC2.shift(1) * 100 > 0.3
            S7 = (KC3 - KC3.shift(1)) / KC3.shift(1) * 100 > 0.3
            S8 = (BBI - BBI.shift(1)) / BBI.shift(1) * 100 > 0.3
            
            SCNT = S1.astype(int) + S2.astype(int) + S3.astype(int) + S4.astype(int) + S5.astype(int) + S6.astype(int) + S7.astype(int) + S8.astype(int)
            SLOK = SCNT >= 2
            
            WGUP = (XS3 >= XS3.shift(1)) | (XS4 >= XS4.shift(1))
            
            XSLP1 = (XS1 - XS1.shift(1)) / XS1.shift(1) * 100
            XSLP2 = (XS2 - XS2.shift(1)) / XS2.shift(1) * 100
            XSLPOK = (XSLP1 >= 0.25) & (XSLP2 >= 0.25)
            
            QKC = (L < KC1).rolling(5).sum() >= 2
            
            # ========== 周线模拟 ==========
            RS = (C - C.shift(1)).clip(lower=0)
            ABS_RS = (C - C.shift(1)).abs()
            W_RSI = (RS.ewm(alpha=1/70, adjust=False).mean() / ABS_RS.ewm(alpha=1/70, adjust=False).mean().replace(0, 1e-10) * 100).fillna(50)
            W_RSI_UP = W_RSI > W_RSI.shift(1)
            
            W_RSV = (C - L.rolling(45).min()) / (H.rolling(45).max() - L.rolling(45).min() + 1e-10) * 100
            W_K = W_RSV.ewm(alpha=1/15, adjust=False).mean()
            W_D = W_K.ewm(alpha=1/15, adjust=False).mean()
            W_J = 3 * W_K - 2 * W_D
            W_J_UP = W_J > W_J.shift(1)
            
            WEEK_UP = W_RSI_UP | W_J_UP
            
            # ========== AVWAP 突破 ==========
            v3 = VOL_SHARES.rolling(3).sum().replace(0, np.nan)
            v20 = VOL_SHARES.rolling(20).sum().replace(0, np.nan)
            v60 = VOL_SHARES.rolling(60).sum().replace(0, np.nan)
            
            AVWAP3 = (AMT.rolling(3).sum() / v3).ffill()
            AVWAP20 = (AMT.rolling(20).sum() / v20).ffill()
            AVWAP60 = (AMT.rolling(60).sum() / v60).ffill()
            
            PREV_A3_DOWN = AVWAP3.shift(1) <= AVWAP3.shift(2)
            CURR_A3_UP = AVWAP3 > AVWAP3.shift(1)
            A3_TURN_UP = PREV_A3_DOWN & CURR_A3_UP
            
            CRS_A20 = (AVWAP3 > AVWAP20) & (AVWAP3.shift(1) <= AVWAP20.shift(1))
            CRS_A60 = (AVWAP3 > AVWAP60) & (AVWAP3.shift(1) <= AVWAP60.shift(1))
            CRS_BBI = (AVWAP3 > BBI) & (AVWAP3.shift(1) <= BBI.shift(1))
            CRS_KC1 = (AVWAP3 > KC1) & (AVWAP3.shift(1) <= KC1.shift(1))
            CRS_ANY = CRS_A20 | CRS_A60 | CRS_BBI | CRS_KC1
            
            A3_SLP = (AVWAP3 - AVWAP3.shift(1)) / AVWAP3.shift(1) * 100
            A3_SLP_OK = A3_SLP > 0.20
            
            # ========== 最终综合判定 ==========
            FINAL_SIGNAL = UPOK & FOK & QZ & GCNT & N3 & YX & IOK & KCOK & NO_GAO & SLOK & WGUP & QKC & XSLPOK & WEEK_UP & A3_TURN_UP & CRS_ANY & A3_SLP_OK

            if FINAL_SIGNAL.iloc[target_idx] == True:
                last_row = df_daily.iloc[target_idx]
                pct_chg = float(last_row['pct_chg']) if 'pct_chg' in df_daily.columns and not pd.isna(last_row['pct_chg']) else 0
                
                return {
                    'ts_code': ts_code,
                    'name': name,
                    'close': round(float(last_row['close']), 2),
                    'pct_chg': round(pct_chg, 2)
                }, 'selected'

            return None, 'condition_fail'

        except Exception as e:
            return None, 'exception'

    def filter_market_stocks(self, trade_date, is_intraday=False):
        trade_date = norm_date(trade_date)
        stocks = self.get_candidate_stocks(trade_date, is_intraday)
        if len(stocks) == 0: return []
        if is_intraday and not self.rt_data_dict:
            self.fetch_rt_k_batch()

        stock_codes = stocks['ts_code'].tolist()
        daily_dict = self.batch_fetch_daily_history(trade_date, stock_codes)

        selected = []
        stats = {'selected': 0, 'data_short': 0, 'date_mismatch': 0, 'condition_fail': 0, 'exception': 0, 'no_data': 0}

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
                futures[executor.submit(self.process_stock_from_df, info, ddf, trade_date, is_intraday)] = code

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
        if not results: return
        mode = "_实时" if is_intraday else ""
        path = os.path.join(GlobalConfig.OUTPUT_DIR, f"多通道AVWAP突破_{trade_date}{mode}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"多通道极值共振 + AVWAP3斜率突破 - {trade_date}\n共选中 {len(results)} 只\n\n")
            for i, s in enumerate(results, 1):
                f.write(f"{i:3d}. {s['ts_code']} {s['name']} 价:{s['close']:.2f} 涨:{s['pct_chg']:+.2f}%\n")
        print(f"结果已保存: {path}")

def main():
    print("=" * 110)
    print(f"{BOLD} 多通道极值共振 + AVWAP3斜率突破 选股系统 【终极修Bug版】{RESET}")
    print("=" * 110)
    filter_tool = MultiChannelFilter(GlobalConfig.TUSHARE_TOKEN)

    date_input = input("\n输入T日日期(YYYYMMDD)，回车使用今天: ").strip()
    validated_date = filter_tool.validate_trade_date(date_input)
    print(f"使用交易日: {validated_date}")

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

    if results:
        print("\n" + "=" * 120)
        print(f"{BOLD}{CYAN}【结果】多通道共振突破 - {validated_date} - 共 {len(results)} 只{RESET}")
        for i, s in enumerate(results, 1):
            color = RED if i % 2 == 1 else YELLOW
            print(f"\n{color}{BOLD}  {i:2d}. {s['ts_code']} {s['name']:<8} 价:{s['close']:8.2f} 涨:{s['pct_chg']:+6.2f}%{RESET}")
    else:
        print(f"\n{YELLOW}本次未选出股票，请检查条件或换日期重试。{RESET}")

if __name__ == '__main__':
    main()