# -*- coding: utf-8 -*-
"""
肯特纳-V 动量起爆 + 周线红柱护航 + 严格防坑 + 双轨突破版 (真实周线聚合修复版)
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
    TUSHARE_TOKEN = 'a91652800def39d213abec3af9563e4485e83940bfbbd8276f8adaa8'  # 替换为你的有效Token
    OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', '肯特纳V动量起爆_选股结果')
    CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股日线公共缓存')
    LOOKBACK_DAYS = 500
    USE_PARQUET = True

class KCVFilter:
    def __init__(self, token):
        self.pro = ts.pro_api(token)
        self.rt_data_dict = {}
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
            AMT = df_daily['amount'] * 1000 if 'amount' in df_daily.columns else C * V * 1000
            VOL_SHARES = V * 100

            # ================= 1. 基础数据与前置条件 =================
            BBI = (C.rolling(3).mean() + C.rolling(6).mean() + C.rolling(12).mean() + C.rolling(24).mean()) / 4
            KC_MID = C.ewm(span=20, adjust=False).mean()
            FAST_MA = C.ewm(span=5, adjust=False).mean()
            BAR_KC = (FAST_MA - KC_MID) / KC_MID * 100 * 2

            PREV_DOWN = (C.shift(1) < O.shift(1)) | (C.shift(1) < C.shift(2))
            PREV_LOW_BELOW = L.shift(1) < KC_MID.shift(1)
            T1_COND = PREV_DOWN & PREV_LOW_BELOW

            IS_RED_UP = (C > O) & (C > C.shift(1))
            VOL_OK = V / V.rolling(5).mean().shift(1) > 1.1
            T_BASE = IS_RED_UP & VOL_OK

            if not bool(T1_COND.iloc[-1]) or not bool(T_BASE.iloc[-1]):
                return None, 'condition_fail'

            # ================= 2. 肯特纳-V 日线动能条件 =================
            D_COND_A = (BAR_KC.shift(1) < 0) & (BAR_KC > 0)
            D_COND_B = (BAR_KC.shift(1) > 0) & (BAR_KC.shift(2) > 0) & (BAR_KC.shift(1) < BAR_KC.shift(2)) & (BAR_KC > 0)
            D_MOM_COND = D_COND_A | D_COND_B

            if not bool(D_MOM_COND.iloc[-1]): return None, 'condition_fail'

            # ================= 3. AVWAP3 拐头条件 =================
            AVWAP3 = AMT.rolling(3).sum() / VOL_SHARES.rolling(3).sum()
            A3_COND = (AVWAP3 > AVWAP3.shift(1)) & (AVWAP3.shift(1) <= AVWAP3.shift(2))

            if not bool(A3_COND.iloc[-1]): return None, 'condition_fail'

            # ================= 4. 动态聚合周线（真实周线聚合） =================
            temp = df_daily[['trade_date', 'open', 'high', 'low', 'close']].copy()
            temp['trade_date'] = pd.to_datetime(temp['trade_date'])
            temp.set_index('trade_date', inplace=True)
            
            # 生成真正的周线
            wdf = temp.resample('W-FRI').agg({'open':'first', 'high':'max', 'low':'min', 'close':'last'}).dropna().reset_index()
            
            # 确保包含今天（如果今天是周内交易日）
            if wdf.iloc[-1]['trade_date'].strftime('%Y%m%d') < trade_date:
                cur_w = temp.loc[trade_date:]
                if not cur_w.empty:
                    w_row = {
                        'trade_date': pd.to_datetime(trade_date),
                        'open': cur_w['open'].iloc[0],
                        'high': cur_w['high'].max(),
                        'low': cur_w['low'].min(),
                        'close': cur_w['close'].iloc[-1]
                    }
                    if wdf.iloc[-1]['trade_date'].strftime('%Y-%m-%d') == cur_w.index[0].strftime('%Y-%m-%d'):
                        wdf = wdf.iloc[:-1]
                    wdf = pd.concat([wdf, pd.DataFrame([w_row])], ignore_index=True)
            
            if len(wdf) < 30: return None, 'data_short'
            
            W_C = wdf['close']
            W_KC_MID = W_C.ewm(span=20, adjust=False).mean()
            W_FAST_MA = W_C.ewm(span=5, adjust=False).mean()
            W_BAR_KC = (W_FAST_MA - W_KC_MID) / W_KC_MID * 100 * 2
            
            # 现在获取真正的本周和上周的值
            W_BAR_CURR = W_BAR_KC.iloc[-1]
            W_BAR_PREV = W_BAR_KC.iloc[-2]
            
            W_COND_A = (W_BAR_PREV < 0) & (W_BAR_CURR > 0)
            W_COND_B = (W_BAR_PREV > 0) & (W_BAR_CURR > W_BAR_PREV)
            W_MOM_COND = W_COND_A or W_COND_B

            if not W_MOM_COND: return None, 'condition_fail'

            # ================= 5. 防坑与形态过滤条件 =================
            NO_HIGH_OPEN = O <= C.shift(1) * 1.03

            THREE_RED = (C > O).rolling(3).sum() == 3
            THREE_UP = (C > C.shift(1)).rolling(3).sum() == 3
            NO_TRIPLE = ((THREE_RED | THREE_UP).rolling(5).sum() == 0)

            DIF8 = C.ewm(span=8, adjust=False).mean() - C.ewm(span=20, adjust=False).mean()
            DEA6 = DIF8.ewm(span=6, adjust=False).mean()
            MACD_BAR = 2 * (DIF8 - DEA6)

            MACD_RED = (MACD_BAR > 0) & (MACD_BAR.shift(1) <= 0)
            MACD_SHRINK = (MACD_BAR < 0) & (MACD_BAR.shift(1) < 0) & (MACD_BAR > MACD_BAR.shift(1) * 0.7)
            MACD_COND = MACD_RED | MACD_SHRINK

            MID_COND = (C >= BBI) | (C >= KC_MID)

            if not (bool(NO_HIGH_OPEN.iloc[-1]) and bool(NO_TRIPLE.iloc[-1]) and bool(MACD_COND.iloc[-1]) and bool(MID_COND.iloc[-1])):
                return None, 'filter_fail'

            # ================= 6. 最终确认 =================
            t_day = df_daily.iloc[-1]
            t1 = df_daily.iloc[-2]
            pct_chg = (float(t_day['close']) / float(t1['close']) - 1) * 100

            return {
                'ts_code': ts_code,
                'name': name,
                'close': round(float(t_day['close']), 2),
                'pct_chg': round(pct_chg, 2)
            }, 'selected'

        except Exception as e:
            return None, 'exception'

    def filter_market_stocks(self, trade_date, is_intraday=False):
        print(f"\n开始【肯特纳-V 动量起爆】过滤，日期: {trade_date}")
        stocks = self.get_candidate_stocks(trade_date, is_intraday)
        if len(stocks) == 0:
            print("候选池为空")
            return []
        if is_intraday and not self.rt_data_dict:
            self.fetch_rt_k_batch()

        stock_codes = stocks['ts_code'].tolist()
        daily_dict = self.batch_fetch_daily_history(trade_date, stock_codes)

        selected = []
        stats = {'selected': 0, 'data_short': 0, 'condition_fail': 0, 'filter_fail': 0, 'exception': 0, 'no_data': 0}

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
        path = os.path.join(GlobalConfig.OUTPUT_DIR, f"肯特纳V动量起爆_{trade_date}{mode}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"肯特纳V动量起爆 - {trade_date}\n共选中 {len(results)} 只\n\n")
            for i, s in enumerate(results, 1):
                f.write(f"{i:3d}. {s['ts_code']} {s['name']} 价:{s['close']:.2f} 涨:{s['pct_chg']:+.2f}%\n")
        print(f"结果已保存: {path}")

    def print_console_results(self, results):
        print("\n" + "=" * 130)
        print(f"{BOLD}{CYAN}【结果】肯特纳V动量起爆 - 共 {len(results)} 只{RESET}")
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
    print(f"{BOLD} 肯特纳-V 动量起爆策略 【公共缓存 + 并发版】{RESET}")
    print("=" * 110)
    filter_tool = KCVFilter(GlobalConfig.TUSHARE_TOKEN)

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