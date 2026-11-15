# -*- coding: utf-8 -*-
"""
双动能必选 + 通道/薛斯起爆共振 (实时盘中终极修复版)
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

class GlobalConfig:
    TUSHARE_TOKEN = 'a91652800def39d213abec3af9563e4485e83940bfbbd8276f8adaa8'
    OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', '双动能通道共振_选股结果')
    CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股日线公共缓存')
    LOOKBACK_DAYS = 500
    USE_PARQUET = True

class DualMomentumFilter:
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
        trade_date = norm_date(trade_date)
        try:
            df_daily = df_daily.copy()
            df_daily['trade_date'] = df_daily['trade_date'].map(norm_date)
            df_daily = df_daily[df_daily['trade_date'] <= trade_date].reset_index(drop=True)
            
            if len(df_daily) < 30: return None, 'data_short'
            
            # ================= 【核心修复】处理实时行情拼接 =================
            if is_intraday and ts_code in self.rt_data_dict:
                rt = self.rt_data_dict[ts_code]
                last_date = str(df_daily.iloc[-1]['trade_date'])
                
                # 构造今日实时数据，并严格把 rt_k 单位转换为与 pro.daily 一致（手、千元）
                new_row = {
                    'ts_code': ts_code, 'trade_date': trade_date,
                    'open': float(rt['open']), 'high': float(rt['high']),
                    'low': float(rt['low']), 'close': float(rt['close']),
                    'vol': float(rt.get('vol', 0)) / 100.0,        # 股 -> 手
                    'amount': float(rt.get('amount', 0)) / 1000.0, # 元 -> 千元
                    'pct_chg': (float(rt['close']) / float(rt.get('pre_close', rt['close'])) - 1) * 100
                }
                
                if last_date == trade_date:
                    # 如果缓存最后一天已经是今天，直接覆盖
                    idx = len(df_daily) - 1
                    for k, v in new_row.items():
                        df_daily.at[idx, k] = v
                else:
                    # 如果缓存最后一天是昨天，拼接今天的数据
                    df_daily = pd.concat([df_daily, pd.DataFrame([new_row])], ignore_index=True)
            # ===============================================================

            target_mask = df_daily['trade_date'] == trade_date
            if not target_mask.any(): return None, 'date_mismatch'
            
            target_idx = df_daily.index[df_daily['trade_date'] == trade_date][-1]

            O = pd.to_numeric(df_daily['open'], errors='coerce').ffill().bfill()
            C = pd.to_numeric(df_daily['close'], errors='coerce').ffill().bfill()
            H = pd.to_numeric(df_daily['high'], errors='coerce').ffill().bfill()
            L = pd.to_numeric(df_daily['low'], errors='coerce').ffill().bfill()
            V = pd.to_numeric(df_daily['vol'], errors='coerce').ffill().bfill()
            
            if 'amount' in df_daily.columns:
                AMT = pd.to_numeric(df_daily['amount'], errors='coerce').ffill().bfill() * 1000
            else:
                AMT = C * V * 100

            # ========== 1. 全局基础条件 ==========
            PREV_DN = ((C.shift(1) < O.shift(1)) | (C.shift(1) < C.shift(2))).fillna(False).astype(bool)
            
            vol_ma5_prev = V.rolling(5).mean().shift(1).replace(0, np.nan)
            CURR_UP = ((C > O) & (C > C.shift(1)) & (V / vol_ma5_prev > 1.2)).fillna(False).astype(bool)

            # ========== 2. 必选条件一：MACD(8,20,6) 近3日首次多头 ==========
            DIF8 = C.ewm(span=8, adjust=False).mean() - C.ewm(span=20, adjust=False).mean()
            DEA6 = DIF8.ewm(span=6, adjust=False).mean()
            
            MACD_IS_BULL = (DIF8 > DEA6).fillna(False).astype(bool)
            COND_MACD = MACD_IS_BULL & (MACD_IS_BULL.rolling(3).sum() == 1)

            # ========== 3. 必选条件二：SKDJ(14,3) 多头且 K 向上 ==========
            RSV_14 = (C - L.rolling(14).min()) / (H.rolling(14).max() - L.rolling(14).min() + 1e-10) * 100
            K_14 = RSV_14.ewm(alpha=1/3, adjust=False).mean()
            D_14 = K_14.ewm(alpha=1/3, adjust=False).mean()
            COND_SKDJ = ((K_14 > D_14) & (K_14 > K_14.shift(1))).fillna(False).astype(bool)

            # ========== 4. 可选条件：通道/薛斯 ==========
            BBI = (C.rolling(3).mean() + C.rolling(6).mean() + C.rolling(12).mean() + C.rolling(24).mean()) / 4
            BBI_UP = (BBI > BBI.shift(1)).fillna(False).astype(bool)
            BBI_PREV_DN = (BBI.shift(1) <= BBI.shift(2)).fillna(False).astype(bool)
            BBI_TURN = BBI_UP & BBI_PREV_DN

            XS_AA = ((2*C + H + L) / 4).rolling(5).mean()
            XS2 = XS_AA * (200 - 102) / 100
            XS_CC = ((2*C + H + L)/4 - C.rolling(20).mean()).abs() / C.rolling(20).mean()
            
            CC_arr = XS_CC.fillna(0).values
            C_arr = C.values
            DD = np.zeros(len(C))
            DD[0] = C_arr[0]
            for i in range(1, len(C)):
                DD[i] = CC_arr[i] * C_arr[i] + (1 - CC_arr[i]) * DD[i-1]
            XS_DD = pd.Series(DD, index=C.index)
            XS3 = (1 + 7/100) * XS_DD

            XS2_UP = (XS2 > XS2.shift(1)).fillna(False).astype(bool)
            XS3_UP = (XS3 > XS3.shift(1)).fillna(False).astype(bool)
            XS_UP = XS2_UP & XS3_UP
            XS_FIRST = XS_UP & (XS_UP.rolling(3).sum() == 1)

            CHANNEL_COND = BBI_TURN | XS_FIRST

            # ========== 5. 新增过滤条件 ==========
            IS_3_RED = ((C > O) & (C.shift(1) > O.shift(1)) & (C.shift(2) > O.shift(2))).fillna(False).astype(bool)
            IS_3_UP = ((C > C.shift(1)) & (C.shift(1) > C.shift(2)) & (C.shift(2) > C.shift(3))).fillna(False).astype(bool)
            NO_3RD_5 = (IS_3_RED | IS_3_UP).rolling(5).sum() == 0

            NO_GAP = (O <= C.shift(1) * 1.03).fillna(False).astype(bool)

            v3 = V.rolling(3).sum().replace(0, np.nan)
            AVWAP3 = (AMT.rolling(3).sum() / (v3 * 100)).ffill()
            AVWAP3_UP = (AVWAP3 > AVWAP3.shift(1)).fillna(False).astype(bool)

            # ========== 6. 最终综合 ==========
            FINAL_SIGNAL = (
                PREV_DN & CURR_UP & COND_MACD & COND_SKDJ & CHANNEL_COND &
                NO_3RD_5 & NO_GAP & AVWAP3_UP
            )

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
        path = os.path.join(GlobalConfig.OUTPUT_DIR, f"双动能通道_{trade_date}{mode}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"双动能必选 + 通道/薛斯起爆 - {trade_date}\n共选中 {len(results)} 只\n\n")
            for i, s in enumerate(results, 1):
                f.write(f"{i:3d}. {s['ts_code']} {s['name']} 价:{s['close']:.2f} 涨:{s['pct_chg']:+.2f}%\n")
        print(f"结果已保存: {path}")

def main():
    print("=" * 110)
    print(f"{BOLD} 双动能必选 + 通道/薛斯起爆 选股系统 {RESET}")
    print("=" * 110)
    filter_tool = DualMomentumFilter(GlobalConfig.TUSHARE_TOKEN)

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
        print(f"{BOLD}{CYAN}【结果】双动能通道共振 - {validated_date} - 共 {len(results)} 只{RESET}")
        for i, s in enumerate(results, 1):
            color = RED if i % 2 == 1 else YELLOW
            print(f"\n{color}{BOLD}  {i:2d}. {s['ts_code']} {s['name']:<8} 价:{s['close']:8.2f} 涨:{s['pct_chg']:+6.2f}%{RESET}")
    else:
        print(f"\n{YELLOW}本次未选出股票，请检查条件或换日期重试。{RESET}")

if __name__ == '__main__':
    main()