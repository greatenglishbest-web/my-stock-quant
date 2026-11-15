# -*- coding: utf-8 -*-
"""
多周期 AVWAP 趋势共振选股
逻辑：日线刚拐头，周线/月线只需保持向上趋势即可
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
    # ================= 替换为你的有效 Token =================
    TUSHARE_TOKEN = 'a91652800def39d213abec3af9563e4485e83940bfbbd8276f8adaa8'
    OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'AVWAP多周期_选股结果')
    CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股日线公共缓存')
    LOOKBACK_DAYS = 800  # 加载长周期数据以确保月线指标收敛
    USE_PARQUET = True

class AVWAPMultiPeriodFilter:
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
        print("正在加载股票池（底层已排除 ST/北交所 + 上市≥60天）...")
        df = self.pro.stock_basic(list_status='L', fields='ts_code,name,list_date')
        today = datetime.now().strftime('%Y%m%d')
        filtered = []
        for _, row in df.iterrows():
            name, code = str(row['name']), row['ts_code']
            # 这里已经强制排除了 ST 和 北交所（8/4/92开头）
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
        print("正在预筛：当日红盘上涨K线 ...")  # 【修改处】改为红盘预筛
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
                    df = self._call_with_retry(self.pro.daily, trade_date=trade_date, fields='ts_code,open,close,pct_chg,high,low,vol,amount')
                    if not df.empty: self.save_day_to_cache(trade_date, df)
                if not df.empty:
                    # 【修改处】必须收阳线(C>O)且上涨(pct_chg>0)
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

            O = pd.to_numeric(df_daily['open'], errors='coerce').ffill().bfill()
            C = pd.to_numeric(df_daily['close'], errors='coerce').ffill().bfill()
            V = pd.to_numeric(df_daily['vol'], errors='coerce').ffill().bfill()
            
            if 'amount' in df_daily.columns:
                AMT = pd.to_numeric(df_daily['amount'], errors='coerce').ffill().bfill() * 1000
            else:
                AMT = C * V * 100
            VOL_SHARES = V * 100

            df_daily['O'] = O
            df_daily['C'] = C
            df_daily['AMT'] = AMT
            df_daily['VOL_SHARES'] = VOL_SHARES
            
            df_idx = df_daily.set_index('trade_date')
            df_idx.index = pd.to_datetime(df_idx.index)

            # ========== 1. 日线级别 (要求刚刚拐头) ==========
            v3 = df_idx['VOL_SHARES'].rolling(3).sum().replace(0, np.nan)
            v5 = df_idx['VOL_SHARES'].rolling(5).sum().replace(0, np.nan)
            AVWAP3_D = (df_idx['AMT'].rolling(3).sum() / v3).ffill()
            AVWAP5_D = (df_idx['AMT'].rolling(5).sum() / v5).ffill()
            
            # 日线双线向上
            D_UP = (AVWAP3_D > AVWAP3_D.shift(1)) & (AVWAP5_D > AVWAP5_D.shift(1))
            
            # 前一日下跌，且前一日AVWAP3向下
            PREV_DN = (df_idx['C'].shift(1) < df_idx['O'].shift(1)) | (df_idx['C'].shift(1) < df_idx['C'].shift(2))
            PREV_AV3_DN = AVWAP3_D.shift(1) < AVWAP3_D.shift(2)
            
            # ========== 2. 周线级别 (只需当前向上即可，不要求刚拐头) ==========
            df_w = df_idx.resample('W-FRI').agg({'O': 'first', 'C': 'last', 'AMT': 'sum', 'VOL_SHARES': 'sum'}).dropna(subset=['C'])
            v3_w = df_w['VOL_SHARES'].rolling(3).sum().replace(0, np.nan)
            v5_w = df_w['VOL_SHARES'].rolling(5).sum().replace(0, np.nan)
            AVWAP3_W = (df_w['AMT'].rolling(3).sum() / v3_w).ffill()
            AVWAP5_W = (df_w['AMT'].rolling(5).sum() / v5_w).ffill()
            
            # 周线双线向上 (本周值 > 上周值)
            W_UP = (AVWAP3_W > AVWAP3_W.shift(1)) & (AVWAP5_W > AVWAP5_W.shift(1))
            
            # ========== 3. 月线级别 (只需当前向上即可，不要求刚拐头) ==========
            df_m = df_idx.resample('ME').agg({'O': 'first', 'C': 'last', 'AMT': 'sum', 'VOL_SHARES': 'sum'}).dropna(subset=['C'])
            v3_m = df_m['VOL_SHARES'].rolling(3).sum().replace(0, np.nan)
            v5_m = df_m['VOL_SHARES'].rolling(5).sum().replace(0, np.nan)
            AVWAP3_M = (df_m['AMT'].rolling(3).sum() / v3_m).ffill()
            AVWAP5_M = (df_m['AMT'].rolling(5).sum() / v5_m).ffill()
            
            # 月线双线向上 (本月值 > 上月值)
            M_UP = (AVWAP3_M > AVWAP3_M.shift(1)) & (AVWAP5_M > AVWAP5_M.shift(1))

            # ========== 4. 多周期信号映射回日线 ==========
            W_SIG = pd.DataFrame({'W_UP': W_UP}).resample('D').ffill().reindex(df_idx.index).fillna(False)
            M_SIG = pd.DataFrame({'M_UP': M_UP}).resample('D').ffill().reindex(df_idx.index).fillna(False)

            # ========== 5. 综合判定 ==========
            FINAL_SIGNAL = D_UP & W_SIG['W_UP'].values & M_SIG['M_UP'].values & PREV_DN & PREV_AV3_DN

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
        path = os.path.join(GlobalConfig.OUTPUT_DIR, f"AVWAP多周期_{trade_date}{mode}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"日周月 AVWAP 趋势选股 - {trade_date}\n共选中 {len(results)} 只\n\n")
            for i, s in enumerate(results, 1):
                f.write(f"{i:3d}. {s['ts_code']} {s['name']} 价:{s['close']:.2f} 涨:{s['pct_chg']:+.2f}%\n")
        print(f"结果已保存: {path}")

def main():
    print("=" * 110)
    print(f"{BOLD} 日/周/月 AVWAP 趋势选股系统 (日线拐头 + 周/月向上) {RESET}")
    print("=" * 110)
    filter_tool = AVWAPMultiPeriodFilter(GlobalConfig.TUSHARE_TOKEN)

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
        print(f"{BOLD}{CYAN}【结果】多周期选股 - {validated_date} - 共 {len(results)} 只{RESET}")
        for i, s in enumerate(results, 1):
            color = RED if i % 2 == 1 else YELLOW
            print(f"\n{color}{BOLD}  {i:2d}. {s['ts_code']} {s['name']:<8} 价:{s['close']:8.2f} 涨:{s['pct_chg']:+6.2f}%{RESET}")
    else:
        print(f"\n{YELLOW}本次未选出股票，请检查条件或换日期重试。{RESET}")

if __name__ == '__main__':
    main()