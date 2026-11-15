# -*- coding: utf-8 -*-
"""
三线/两线全包探底反包 + AVWAP3突破 + 多指标转红共振 (Python 量化复刻版 - 修复对齐版)
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
    # ================= 替换为你的有效 Token =================
    TUSHARE_TOKEN = 'a91652800def39d213abec3af9563e4485e83940bfbbd8276f8adaa8'  
    OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', '三线全包探底反包_选股结果')
    CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股日线公共缓存')
    LOOKBACK_DAYS = 500
    USE_PARQUET = True

class MultiLineBreakoutFilter:
    def __init__(self, token):
        self.pro = ts.pro_api(token)
        self.rt_data_dict = {}
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
            if 'ST' in name or code.startswith('8'): continue
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
            if pd.isna(series.iloc[i]): res[i] = np.nan
            elif i == 0 or pd.isna(res[i - 1]): res[i] = series.iloc[i]
            else: res[i] = (m * series.iloc[i] + (n - m) * res[i - 1]) / n
        return pd.Series(res, index=series.index)

    def calculate_sar(self, high, low, af=0.02, amax=0.2):
        length = len(high)
        sar = np.zeros(length)
        trend = np.zeros(length)
        ep = np.zeros(length)
        af_array = np.zeros(length)
        if length < 2: return pd.Series(sar, index=high.index)
        sar[0], trend[0], ep[0], af_array[0] = low.iloc[0], 1, high.iloc[0], af
        for i in range(1, length):
            if trend[i - 1] == 1:
                sar[i] = sar[i - 1] + af_array[i - 1] * (ep[i - 1] - sar[i - 1])
                sar[i] = min(sar[i], low.iloc[i - 1], low.iloc[i - 2]) if i >= 2 else min(sar[i], low.iloc[i - 1])
                if low.iloc[i] < sar[i]:
                    trend[i], sar[i], ep[i], af_array[i] = -1, ep[i - 1], low.iloc[i], af
                else:
                    trend[i] = 1
                    if high.iloc[i] > ep[i - 1]:
                        ep[i] = high.iloc[i]
                        af_array[i] = min(af_array[i - 1] + af, amax)
                    else:
                        ep[i], af_array[i] = ep[i - 1], af_array[i - 1]
            else:
                sar[i] = sar[i - 1] + af_array[i - 1] * (ep[i - 1] - sar[i - 1])
                sar[i] = max(sar[i], high.iloc[i - 1], high.iloc[i - 2]) if i >= 2 else max(sar[i], high.iloc[i - 1])
                if high.iloc[i] > sar[i]:
                    trend[i], sar[i], ep[i], af_array[i] = 1, ep[i - 1], high.iloc[i], af
                else:
                    trend[i] = -1
                    if low.iloc[i] < ep[i - 1]:
                        ep[i] = low.iloc[i]
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
            # ================= 修复 1：严格按日期截断数据，防止引用未来数据或混入其他日线 =================
            df_daily = df_daily[df_daily['trade_date'] <= trade_date].copy()
            if len(df_daily) < 60: return None, 'data_short'

            # 实时数据修复
            if is_intraday and ts_code in self.rt_data_dict:
                rt = self.rt_data_dict[ts_code]
                last_date = str(df_daily.iloc[-1]['trade_date'])
                if last_date == trade_date:
                    idx = len(df_daily) - 1
                    df_daily.at[idx, 'close'] = float(rt['close'])
                    df_daily.at[idx, 'high'] = max(float(df_daily.at[idx, 'high']), float(rt['high']))
                    df_daily.at[idx, 'low'] = min(float(df_daily.at[idx, 'low']), float(rt['low']))
                    if float(rt.get('open', 0)) > 0: df_daily.at[idx, 'open'] = float(rt['open'])
                    df_daily.at[idx, 'vol'] = float(rt['vol'])
                    if 'amount' in df_daily.columns: df_daily.at[idx, 'amount'] = float(rt.get('amount', df_daily.at[idx, 'amount']))
                else:
                    new_row = {
                        'ts_code': ts_code, 'trade_date': trade_date,
                        'open': float(rt['open']), 'high': float(rt['high']),
                        'low': float(rt['low']), 'close': float(rt['close']),
                        'vol': float(rt['vol']), 'amount': float(rt.get('amount', 0)),
                        'pct_chg': (float(rt['close']) / float(rt.get('pre_close', rt['close'])) - 1) * 100
                    }
                    df_daily = pd.concat([df_daily, pd.DataFrame([new_row])], ignore_index=True)

            O, C, H, L, V = df_daily['open'], df_daily['close'], df_daily['high'], df_daily['low'], df_daily['vol']
            
            # ================= 修复 2：处理停牌导致的 0 值和除零错误 =================
            AMT = df_daily['amount'] * 1000 if 'amount' in df_daily.columns else C * V * 1000
            VOL_SHARES = V * 100
            VOL_SHARES = VOL_SHARES.replace(0, np.nan)
            AMT = AMT.replace(0, np.nan) if 'amount' in df_daily.columns else C * VOL_SHARES

            # ================= 1. 核心线条计算 =================
            AVWAP3  = AMT.rolling(3).sum() / VOL_SHARES.rolling(3).sum()
            AVWAP20 = AMT.rolling(20).sum() / VOL_SHARES.rolling(20).sum()
            AVWAP60 = AMT.rolling(60).sum() / VOL_SHARES.rolling(60).sum()
            KC_MID  = C.ewm(span=20, adjust=False).mean()
            
            # ================= 修复 3：填补 NaN，防止布尔运算变成 False =================
            AVWAP3 = AVWAP3.bfill().fillna(C)
            AVWAP20 = AVWAP20.bfill().fillna(C)
            AVWAP60 = AVWAP60.bfill().fillna(C)

            # ================= 2. BBIBOLL 上轨与中轨 =================
            BBI = (C.rolling(3).mean() + C.rolling(6).mean() + C.rolling(12).mean() + C.rolling(24).mean()) / 4
            # 修复 4：通达信 STD 默认是样本标准差 (ddof=1)，原代码用了 ddof=0
            UPPER = BBI + 2 * C.rolling(26).std(ddof=1) 
            UPPER_UP = UPPER >= UPPER.shift(1)

            # ================= 3. 包含条件 =================
            MAX_3 = pd.concat([AVWAP20, AVWAP60, KC_MID], axis=1).max(axis=1)
            MIN_3 = pd.concat([AVWAP20, AVWAP60, KC_MID], axis=1).min(axis=1)
            INC_3 = (C > O) & (H >= MAX_3) & (L <= MIN_3)

            MAX_2 = pd.concat([AVWAP20, KC_MID], axis=1).max(axis=1)
            MIN_2 = pd.concat([AVWAP20, KC_MID], axis=1).min(axis=1)
            INC_2 = (C > O) & (H >= MAX_2) & (L <= MIN_2)

            # ================= 4. 趋势线向上条件 =================
            L1_UP = AVWAP20 >= AVWAP20.shift(1)
            L2_UP = AVWAP60 >= AVWAP60.shift(1)
            L3_UP = KC_MID >= KC_MID.shift(1)

            ALL_3_UP = L1_UP & L2_UP & L3_UP
            ALL_2_UP = L1_UP & L3_UP

            # ================= 5. 前置过滤条件 =================
            PREV_GREEN = C.shift(1) < O.shift(1)
            PREV_DOJI = (C.shift(1) - O.shift(1)).abs() < (H.shift(1) - L.shift(1) + 0.0001) * 0.15
            PREV_COND = PREV_GREEN | PREV_DOJI
            PREV_BELOW_KC = L.shift(1) < KC_MID.shift(1)

            # ================= 6. AVWAP3与防连涨 =================
            A3_UP = AVWAP3 > AVWAP3.shift(1)
            CROSS_BBI = (AVWAP3 > BBI) & (AVWAP3.shift(1) <= BBI.shift(1))
            CROSS_KC = (AVWAP3 > KC_MID) & (AVWAP3.shift(1) <= KC_MID.shift(1))
            CROSS_A20 = (AVWAP3 > AVWAP20) & (AVWAP3.shift(1) <= AVWAP20.shift(1))
            CROSS_A60 = (AVWAP3 > AVWAP60) & (AVWAP3.shift(1) <= AVWAP60.shift(1))
            CROSS_ANY = CROSS_BBI | CROSS_KC | CROSS_A20 | CROSS_A60

            NOT_4_RED = (C > O).rolling(4).sum() < 4
            NOT_4_UP = (C > C.shift(1)).rolling(4).sum() < 4
            ANTI_CHASE = NOT_4_RED & NOT_4_UP

            # ================= 7. 动能首红过滤 (MACD-V / KDJ-V) =================
            TR1 = pd.concat([H-L, (H-C.shift(1)).abs(), (L-C.shift(1)).abs()], axis=1).max(axis=1)
            # 修复 5：防止 ATR 出现 0 导致除零
            ATR1 = TR1.rolling(20).mean().replace(0, np.nan).bfill().fillna(0.01)
            DIF_V = (C.ewm(span=8, adjust=False).mean() - C.ewm(span=20, adjust=False).mean()) / ATR1 * 100
            DEA_V = DIF_V.ewm(span=6, adjust=False).mean()
            MACD_BAR_V = 2 * (DIF_V - DEA_V)

            RSV = (C - L.rolling(14).min()) / (H.rolling(14).max() - L.rolling(14).min()) * 100
            # 修复 6：防止分母为 0 产生 inf
            RSV = RSV.replace([np.inf, -np.inf], np.nan).fillna(50) 
            K = self.TDX_SMA(RSV, 3, 1)
            D = self.TDX_SMA(K, 3, 1)
            KDJ_BAR = 2 * (K - D)

            MACD_RED_3 = (MACD_BAR_V > 0) & ((MACD_BAR_V > 0).rolling(3).sum() == 1)
            KDJ_RED_3 = (KDJ_BAR > 0) & ((KDJ_BAR > 0).rolling(3).sum() == 1)
            FIRST_RED_COND = MACD_RED_3 | KDJ_RED_3

            # ================= 8. 标准 MACD/KDJ/SAR 当日转红 =================
            DIF8 = C.ewm(span=8, adjust=False).mean() - C.ewm(span=20, adjust=False).mean()
            DEA6 = DIF8.ewm(span=6, adjust=False).mean()
            STD_MACD_BAR = 2 * (DIF8 - DEA6)
            MACD_TURN = (STD_MACD_BAR > 0) & (STD_MACD_BAR.shift(1) <= 0)
            KDJ_TURN = (KDJ_BAR > 0) & (KDJ_BAR.shift(1) <= 0)

            SAR_VAL = self.calculate_sar(H, L, af=0.02, amax=0.2)
            SAR_TURN = (C > SAR_VAL) & (C.shift(1) <= SAR_VAL.shift(1))
            MOMENTUM_TURN = MACD_TURN | KDJ_TURN | SAR_TURN

            # ================= 9. 组合逻辑 =================
            LOGIC_STRICT = INC_3 & ALL_3_UP & PREV_COND & PREV_BELOW_KC & A3_UP & CROSS_ANY & ANTI_CHASE & UPPER_UP & FIRST_RED_COND & MOMENTUM_TURN
            LOGIC_LOOSE = INC_2 & ALL_2_UP & PREV_COND & PREV_BELOW_KC & A3_UP & CROSS_ANY & ANTI_CHASE & UPPER_UP & FIRST_RED_COND & MOMENTUM_TURN

            # ================= 10. 最终判定 =================
            if bool(LOGIC_STRICT.iloc[-1]) or bool(LOGIC_LOOSE.iloc[-1]):
                last_row = df_daily.iloc[-1]
                pct_chg = (last_row['close'] / df_daily.iloc[-2]['close'] - 1) * 100 if len(df_daily) > 1 else 0
                
                sig_type = '严格版(全包三线)' if bool(LOGIC_STRICT.iloc[-1]) else '放宽版(全包两线)'
                
                return {
                    'ts_code': ts_code,
                    'name': name,
                    'close': round(last_row['close'], 2),
                    'pct_chg': round(pct_chg, 2),
                    'signal_type': sig_type,
                    'inc_cnt': 3 if sig_type.startswith('严格') else 2,
                    'bbi_up': round(UPPER.iloc[-1], 2),
                }, 'selected'
                
            return None, 'condition_fail'
            
        except Exception as e:
            # 若需排查异常，可取消下面这行的注释
            # print(f"[{ts_code}] 发生异常: {e}")
            return None, 'exception'

    def filter_market_stocks(self, trade_date, is_intraday=False):
        print(f"\n开始【三线/两线全包探底反包】过滤，日期: {trade_date}")
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
        path = os.path.join(GlobalConfig.OUTPUT_DIR, f"三线全包探底反包_{trade_date}{mode}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"三线/两线全包 探底反包 - {trade_date}\n共选中 {len(results)} 只\n\n")
            for i, s in enumerate(results, 1):
                f.write(f"{i:3d}. {s['ts_code']} {s['name']} 价:{s['close']:.2f} "
                        f"涨:{s['pct_chg']:+.2f}% 形态:{s['signal_type']} "
                        f"包线数:{s['inc_cnt']} BB上轨:{s['bbi_up']}\n")
        print(f"结果已保存: {path}")

    def print_console_results(self, results):
        print("\n" + "=" * 130)
        print(f"{BOLD}{CYAN}【结果】三线/两线全包 探底反包 - 共 {len(results)} 只{RESET}")
        print("=" * 130)
        if not results:
            print("无符合条件股票")
            return
        for i, s in enumerate(results, 1):
            color = RED if i % 2 == 1 else YELLOW
            print(f"\n{color}{BOLD}{'─' * 120}{RESET}")
            print(f"{color}{BOLD}  {i:2d}. {s['ts_code']}  {s['name']:<12}{RESET}")
            print(f"{color}      现价: {s['close']:8.2f}  涨幅: {s['pct_chg']:+7.2f}%  形态: {s['signal_type']}{RESET}")
            print(f"{color}      包线数: {s['inc_cnt']} 条      BBIBOLL上轨: {s['bbi_up']:.2f}{RESET}")
            time.sleep(0.05)
        print(f"\n{GREEN}{BOLD}{'=' * 130}{RESET}")
        print(f"{GREEN}{BOLD}完成，共 {len(results)} 只{RESET}\n")

def main():
    print("=" * 110)
    print(f"{BOLD} 三线/两线全包 探底反包 【公共缓存 + 并发修复版】{RESET}")
    print("=" * 110)
    filter_tool = MultiLineBreakoutFilter(GlobalConfig.TUSHARE_TOKEN)

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