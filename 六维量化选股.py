# -*- coding: utf-8 -*-
"""
多维共振动能通道合流选股 (五指标四共振加强版 + 周线护航 + 极速前复权版)
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
    OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', '五指标共振_选股结果')
    CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股日线公共缓存')
    ADJ_CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股复权因子缓存')
    LOOKBACK_DAYS = 500
    USE_PARQUET = True

class MultiResonanceFilter:
    def __init__(self, token):
        self.pro = ts.pro_api(token)
        self.rt_data_dict = {}
        self._rate_limited = False
        os.makedirs(GlobalConfig.CACHE_DIR, exist_ok=True)
        os.makedirs(GlobalConfig.ADJ_CACHE_DIR, exist_ok=True)
        os.makedirs(GlobalConfig.OUTPUT_DIR, exist_ok=True)
        self.use_parquet = GlobalConfig.USE_PARQUET

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

    def _adj_cache_path(self, trade_date):
        ext = 'parquet' if self.use_parquet else 'csv'
        return os.path.join(GlobalConfig.ADJ_CACHE_DIR, f"{trade_date}.{ext}")

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

    def save_adj_to_cache(self, trade_date, df):
        if df is None or df.empty: return
        path = self._adj_cache_path(trade_date)
        try:
            if self.use_parquet: df.to_parquet(path, index=False)
            else: df.to_csv(path, index=False, encoding='utf-8-sig')
        except: pass

    def load_adj_from_cache(self, trade_date):
        path = self._adj_cache_path(trade_date)
        if not os.path.exists(path): return None
        try:
            return pd.read_parquet(path) if self.use_parquet else pd.read_csv(path)
        except: return None

    def get_cached_dates(self, is_adj=False):
        d = GlobalConfig.ADJ_CACHE_DIR if is_adj else GlobalConfig.CACHE_DIR
        pattern = os.path.join(d, '*.parquet' if self.use_parquet else '*.csv')
        dates = set()
        for f in glob.glob(pattern):
            date_str = os.path.basename(f).split('.')[0]
            if len(date_str) == 8 and date_str.isdigit(): dates.add(date_str)
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
                    df['open'] = pd.to_numeric(df['open'], errors='coerce')
                    df['close'] = pd.to_numeric(df['close'], errors='coerce')
                    df['pct_chg'] = pd.to_numeric(df['pct_chg'], errors='coerce')
                    mask = (df['close'] > df['open']) & (df['pct_chg'] > 0)
                    codes = [c for c in df.loc[mask, 'ts_code'].tolist() if c in set(all_stocks['ts_code'])]
                    strong = all_stocks[all_stocks['ts_code'].isin(codes)].to_dict('records')
                else:
                    print(f"{YELLOW}警告：未获取到 {trade_date} 的任何交易数据！{RESET}")
            except Exception as e:
                print(f"预筛失败: {e}")
        strong_df = pd.DataFrame(strong)
        print(f"预筛完成：共 {len(strong_df)} 只")
        return strong_df

    def fetch_single_date(self, d, need_daily, need_adj):
        if d in need_daily:
            try:
                df = self._call_with_retry(
                    self.pro.daily, trade_date=d,
                    fields='ts_code,trade_date,open,high,low,close,vol,amount,pct_chg'
                )
                if not df.empty: self.save_day_to_cache(d, df)
            except: pass
        if d in need_adj:
            try:
                adj = self._call_with_retry(self.pro.adj_factor, trade_date=d, fields='ts_code,trade_date,adj_factor')
                if not adj.empty: self.save_adj_to_cache(d, adj)
            except: pass

    def batch_fetch_daily_history(self, trade_date, stock_codes):
        print(f"\n{CYAN}准备日线（公共缓存 + 增量 + 复权因子）...{RESET}")
        trade_dates = self.get_trade_dates(trade_date, GlobalConfig.LOOKBACK_DAYS)
        
        cached_daily = self.get_cached_dates(is_adj=False)
        cached_adj = self.get_cached_dates(is_adj=True)
        
        need_daily = [d for d in trade_dates if d not in cached_daily]
        need_adj = [d for d in trade_dates if d not in cached_adj]

        if need_daily or need_adj:
            print(f"缺失日线 {len(need_daily)} 天，缺失复权因子 {len(need_adj)} 天，启动多线程极速下载...")
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = []
                for d in trade_dates:
                    if d in need_daily or d in need_adj:
                        futures.append(executor.submit(self.fetch_single_date, d, set(need_daily), set(need_adj)))
                
                for future in tqdm(as_completed(futures), total=len(futures), desc="并发下载"):
                    pass

        all_dfs = []
        code_set = set(stock_codes)
        for d in tqdm(trade_dates, desc="加载本地缓存"):
            df = self.load_day_from_cache(d)
            adj = self.load_adj_from_cache(d)
            
            if df is not None and not df.empty:
                df = df[df['ts_code'].isin(code_set)]
                if not df.empty:
                    if adj is not None and not adj.empty:
                        df = pd.merge(df, adj, on=['ts_code', 'trade_date'], how='left')
                    else:
                        df['adj_factor'] = 1.0
                    all_dfs.append(df)
                    
        if not all_dfs: return {}
        full = pd.concat(all_dfs, ignore_index=True).sort_values(['ts_code', 'trade_date'])
        full['adj_factor'] = full.groupby('ts_code')['adj_factor'].ffill().bfill().fillna(1.0)
        return {code: g.reset_index(drop=True) for code, g in full.groupby('ts_code')}

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

    def process_stock_from_df(self, stock_info, df_daily, trade_date, is_intraday=False):
        ts_code = stock_info['ts_code']
        name = stock_info.get('name', '')
        try:
            df_daily = df_daily[df_daily['trade_date'] <= trade_date].copy()
            if len(df_daily) < 30: return None, 'data_short'
            if str(df_daily.iloc[-1]['trade_date']) != trade_date:
                return None, 'date_mismatch'
            
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
                    df_daily.at[idx, 'vol'] = float(rt['vol']) / 100.0  
                else:
                    new_row = {
                        'ts_code': ts_code, 'trade_date': trade_date,
                        'open': float(rt['open']), 'high': float(rt['high']),
                        'low': float(rt['low']), 'close': float(rt['close']),
                        'vol': float(rt['vol']) / 100.0,
                        'pct_chg': (float(rt['close']) / float(rt.get('pre_close', rt['close'])) - 1) * 100,
                        'adj_factor': df_daily['adj_factor'].iloc[-1]
                    }
                    df_daily = pd.concat([df_daily, pd.DataFrame([new_row])], ignore_index=True)

            # 【核心修复】前复权转换：当日因子 / 最新因子
            latest_af = df_daily['adj_factor'].iloc[-1]
            af_ratio = df_daily['adj_factor'] / latest_af
            
            O_raw = pd.to_numeric(df_daily['open'], errors='coerce').ffill().bfill()
            C_raw = pd.to_numeric(df_daily['close'], errors='coerce').ffill().bfill()
            H_raw = pd.to_numeric(df_daily['high'], errors='coerce').ffill().bfill()
            L_raw = pd.to_numeric(df_daily['low'], errors='coerce').ffill().bfill()
            V = pd.to_numeric(df_daily['vol'], errors='coerce').ffill().bfill()

            O = O_raw * af_ratio
            C = C_raw * af_ratio
            H = H_raw * af_ratio
            L = L_raw * af_ratio

            # 1. 基础均线与多空布林带
            BBI = (C.rolling(3).mean() + C.rolling(6).mean() + C.rolling(12).mean() + C.rolling(24).mean()) / 4

            # 2. 五大副图指标
            BIAS5 = (C - C.rolling(5).mean()) / C.rolling(5).mean() * 100
            BIAS14 = (C - C.rolling(14).mean()) / C.rolling(14).mean() * 100
            EMA_BIAS5_5 = BIAS5.ewm(span=5, adjust=False).mean()
            EMA_BIAS5_21 = BIAS5.ewm(span=21, adjust=False).mean()
            DIF_B = EMA_BIAS5_5 - EMA_BIAS5_21
            DEA_B = DIF_B.ewm(span=10, adjust=False).mean()
            B_BAR1 = (DIF_B - DEA_B) * 2
            IND1 = ((BIAS14 > BIAS14.shift(1)) | (B_BAR1 > 0)).fillna(False)

            LLV14 = L.rolling(14).min()
            HHV14 = H.rolling(14).max()
            RSV = (C - LLV14) / (HHV14 - LLV14 + 1e-10) * 100
            K = self.TDX_SMA(RSV, 3, 1)
            D = self.TDX_SMA(K, 3, 1)
            KDJ_BAR = (K - D) * 2
            IND2 = ((K > K.shift(1)) | (KDJ_BAR > 0)).fillna(False)

            BOLL_MID = C.rolling(23).mean()
            BOLL_STD = C.rolling(23).std(ddof=0)
            BOLL_UPR = BOLL_MID + 2 * BOLL_STD
            BOLL_DWN = BOLL_MID - 2 * BOLL_STD
            PCT_B = (C - BOLL_DWN) / (BOLL_UPR - BOLL_DWN + 1e-10) * 100
            B_BAR3 = (PCT_B.ewm(span=3, adjust=False).mean() - PCT_B.ewm(span=10, adjust=False).mean()) * 2
            IND3 = ((PCT_B > PCT_B.shift(1)) | (B_BAR3 > 0)).fillna(False)

            OMNI_MID = C.ewm(span=18, adjust=False).mean()
            VOL_RATIO = V / V.rolling(14).mean()
            FAST_P = C.ewm(span=4, adjust=False).mean()
            SLOW_P = C.ewm(span=18, adjust=False).mean()
            BASE_BAR = (FAST_P - SLOW_P) / OMNI_MID * 100 * 2
            BAR_OMNI = BASE_BAR * (1 + 0.4 * (np.sqrt(np.maximum(VOL_RATIO, 0.2)) - 1))
            IND4 = ((OMNI_MID >= OMNI_MID.shift(1)) | (BAR_OMNI > 0)).fillna(False)

            TR1 = pd.concat([H - L, (H - C.shift(1)).abs(), (L - C.shift(1)).abs()], axis=1).max(axis=1)
            ATR1 = TR1.rolling(20).mean().replace(0, np.nan).ffill()
            DIF_V = (C.ewm(span=8, adjust=False).mean() - C.ewm(span=20, adjust=False).mean()) / ATR1 * 100
            DEA_V = DIF_V.ewm(span=6, adjust=False).mean()
            MACD_BAR_V = 2 * (DIF_V - DEA_V)
            IND5 = ((DIF_V > DIF_V.shift(1)) | (MACD_BAR_V > 0)).fillna(False)

            # 3. 核心执行条件
            PREV_DN = ((C.shift(1) < O.shift(1)) | (C.shift(1) < C.shift(2))).fillna(False)
            PREV_BLW_BBI = (L.shift(1) < BBI.shift(1)).fillna(False)
            
            IS_GRN_DN = (C < O) | (C < C.shift(1))
            PREV_3_GRN = IS_GRN_DN.shift(1).rolling(3).sum() >= 1

            IS_3_RED = (C > O) & (C.shift(1) > O.shift(1)) & (C.shift(2) > O.shift(2))
            IS_3_UP = (C > C.shift(1)) & (C.shift(1) > C.shift(2)) & (C.shift(2) > C.shift(3))
            CON_RECENT5 = (IS_3_RED | IS_3_UP).rolling(5).sum() == 0

            CON_HIST10 = (L < BBI).rolling(10).sum() >= 2
            CON_CURR = ((C > O) & (V / V.rolling(5).mean().replace(0, np.nan) > 1.1)).fillna(False)
            CON_INDS = (IND1.astype(int) + IND2.astype(int) + IND3.astype(int) + IND4.astype(int) + IND5.astype(int)) >= 4
            CON_GAP = (O / C.shift(1)) < 1.03
            CON_BBI_UP = BBI > BBI.shift(1)

            W_DIF = C.ewm(span=60, adjust=False).mean() - C.ewm(span=130, adjust=False).mean()
            W_DEA = W_DIF.ewm(span=45, adjust=False).mean()
            W_MACD = 2 * (W_DIF - W_DEA)
            W_RED_1 = (W_MACD > 0) & (W_MACD.shift(5) <= 0)
            W_RED_2 = (W_MACD > 0) & (W_MACD.shift(5) > 0) & (W_MACD >= W_MACD.shift(5))
            W_GRN_2 = (W_MACD < 0) & (W_MACD.shift(5) < 0) & (W_MACD > W_MACD.shift(5))
            CON_W_MACD = (W_RED_1 | W_RED_2 | W_GRN_2).fillna(False)

            # 4. 最终信号判定
            FINAL_SIGNAL = PREV_DN & PREV_BLW_BBI & PREV_3_GRN & CON_RECENT5 & CON_HIST10 & CON_CURR & CON_INDS & CON_GAP & CON_BBI_UP & CON_W_MACD

            if FINAL_SIGNAL.iloc[-1] == True:
                last_row = df_daily.iloc[-1]
                pct_chg = (float(C.iloc[-1]) / float(C.iloc[-2]) - 1) * 100
                vol_ratio = float(V.iloc[-1]) / float(V.iloc[-1:-6:-1].mean())
                
                hit_inds = []
                if IND1.iloc[-1]: hit_inds.append("BIAS")
                if IND2.iloc[-1]: hit_inds.append("KDJ")
                if IND3.iloc[-1]: hit_inds.append("%B")
                if IND4.iloc[-1]: hit_inds.append("OMNI")
                if IND5.iloc[-1]: hit_inds.append("MACD")
                
                return {
                    'ts_code': ts_code,
                    'name': name,
                    'close': round(float(C.iloc[-1]), 2),
                    'pct_chg': round(pct_chg, 2),
                    'vol_ratio': round(vol_ratio, 2),
                    'hit_inds': "、".join(hit_inds),
                    'prev_date': str(df_daily.iloc[-2]['trade_date']),
                    'prev_open': round(float(O.iloc[-2]), 2),
                    'prev_close': round(float(C.iloc[-2]), 2),
                    'prev_pct': round(float((C.iloc[-2] / C.iloc[-3] - 1) * 100), 2)
                }, 'selected'

            return None, 'condition_fail'
            
        except Exception as e:
            return None, 'exception'

    def filter_market_stocks(self, trade_date, is_intraday=False):
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
        path = os.path.join(GlobalConfig.OUTPUT_DIR, f"五指标共振_前复权_{trade_date}{mode}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"五指标四共振 + 周线护航 (前复权) - {trade_date}\n共选中 {len(results)} 只\n\n")
            for i, s in enumerate(results, 1):
                f.write(f"{i:3d}. {s['ts_code']} {s['name']} 价:{s['close']:.2f} 涨:{s['pct_chg']:+.2f}% 量比:{s['vol_ratio']} 命中:{s['hit_inds']}\n")
                f.write(f"      [前日验证] {s['prev_date']} 开:{s['prev_open']:.2f} 收:{s['prev_close']:.2f} 涨:{s['prev_pct']:+.2f}% (条件要求:跌/绿K)\n\n")
        print(f"结果已保存: {path}")

def main():
    print("=" * 110)
    print(f"{BOLD} 五指标四共振 + 周线护航 选股系统 【极速前复权版】{RESET}")
    print("=" * 110)
    filter_tool = MultiResonanceFilter(GlobalConfig.TUSHARE_TOKEN)

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
        print(f"{BOLD}{CYAN}【结果】五指标四共振(极速前复权) - {validated_date} - 共 {len(results)} 只{RESET}")
        for i, s in enumerate(results, 1):
            color = RED if i % 2 == 1 else YELLOW
            print(f"\n{color}{BOLD}  {i:2d}. {s['ts_code']} {s['name']:<8} 价:{s['close']:8.2f} 涨:{s['pct_chg']:+6.2f}% 量比:{s['vol_ratio']:.2f}{RESET}")
            print(f"{color}      [前日验证] {s['prev_date']} 开:{s['prev_open']:.2f} 收:{s['prev_close']:.2f} 涨:{s['prev_pct']:+.2f}% (须<0或开>收){RESET}")
            print(f"{color}      [共振指标] {s['hit_inds']}{RESET}")

if __name__ == '__main__':
    main()