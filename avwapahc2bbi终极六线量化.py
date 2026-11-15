# -*- coding: utf-8 -*-
"""
终极共振选股 + AVWAP3双穿爆发版（新增形态三）
最终稳健版：
- 统一 daily / rt_k 单位（手→股，千元→元）
- 实时模式正确重算涨跌幅
- 兼容旧缓存（无 amount 列时自动近似）
- 单位转换更安全，大幅降低 exception
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

def norm_date(d):
    if d is None or (isinstance(d, float) and np.isnan(d)):
        return ''
    s = str(d).strip().replace('-', '').replace('/', '').replace('.0', '').replace(' ', '')
    return s[:8] if len(s) >= 8 else s

class GlobalConfig:
    # ================= 替换为你的有效 Token =================
    TUSHARE_TOKEN = 'a91652800def39d213abec3af9563e4485e83940bfbbd8276f8adaa8'
    OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', '终极共振_选股结果')
    CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股日线公共缓存')
    LOOKBACK_DAYS = 500
    USE_PARQUET = True

class UltimateResonanceFilter:
    def __init__(self, token):
        self.pro = ts.pro_api(token)
        self.rt_data_dict = {}
        self._ex_count = 0          # 用于限制异常打印次数
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
            df['trade_date'] = trade_date
            for col in ['open', 'close', 'pct_chg', 'high', 'low', 'vol', 'amount']:
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

    def get_stock_pool(self):
        print("正在加载股票池（保留主板/创业板/科创板，排除ST/北交所）...")
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
                if days > 60:
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
                time.sleep(0.4)
            except:
                pass
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
                    df = self._call_with_retry(self.pro.daily, trade_date=trade_date,
                                               fields='ts_code,trade_date,open,close,pct_chg,high,low,vol,amount')
                    if not df.empty:
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
                    df = self._call_with_retry(self.pro.daily, trade_date=d,
                                               fields='ts_code,trade_date,open,high,low,close,vol,amount,pct_chg')
                    if not df.empty:
                        self.save_day_to_cache(d, df)
                    time.sleep(0.12)
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

    def process_stock_from_df(self, stock_info, df_daily, trade_date, is_intraday=False):
        ts_code = stock_info['ts_code']
        name = stock_info.get('name', '')
        trade_date = norm_date(trade_date)
        try:
            df_daily = df_daily.copy()
            df_daily['trade_date'] = df_daily['trade_date'].map(norm_date)
            df_daily = df_daily[df_daily['trade_date'] <= trade_date].reset_index(drop=True)

            if len(df_daily) < 60:
                return None, 'data_short'

            target_mask = df_daily['trade_date'] == trade_date
            if not target_mask.any():
                return None, 'date_mismatch'

            target_idx = df_daily.index[target_mask][-1]

            # ========== 实时数据替换 ==========
            is_rt_today = False
            if is_intraday and ts_code in self.rt_data_dict:
                rt = self.rt_data_dict[ts_code]
                is_rt_today = True
                df_daily.at[target_idx, 'close'] = float(rt['close'])
                df_daily.at[target_idx, 'high'] = max(float(df_daily.at[target_idx, 'high']), float(rt.get('high', 0)))
                df_daily.at[target_idx, 'low'] = min(float(df_daily.at[target_idx, 'low']), float(rt.get('low', 999999)))
                if float(rt.get('open', 0)) > 0:
                    df_daily.at[target_idx, 'open'] = float(rt['open'])
                # rt_k 单位：vol=股，amount=元
                df_daily.at[target_idx, 'vol'] = float(rt.get('vol', 0))
                df_daily.at[target_idx, 'amount'] = float(rt.get('amount', 0))
                pre_close = float(rt.get('pre_close', 0))
                if pre_close > 0:
                    df_daily.at[target_idx, 'pct_chg'] = (float(rt['close']) / pre_close - 1) * 100

            O = pd.to_numeric(df_daily['open'], errors='coerce').ffill().bfill()
            C = pd.to_numeric(df_daily['close'], errors='coerce').ffill().bfill()
            H = pd.to_numeric(df_daily['high'], errors='coerce').ffill().bfill()
            L = pd.to_numeric(df_daily['low'], errors='coerce').ffill().bfill()
            V_raw = pd.to_numeric(df_daily['vol'], errors='coerce').fillna(0)

            # ========== 单位统一（更安全写法） ==========
            # 1. 先假设全部是 daily 数据（手 / 千元）
            vol_shares = V_raw * 100.0
            if 'amount' in df_daily.columns:
                amount_raw = pd.to_numeric(df_daily['amount'], errors='coerce')
                amount_yuan = amount_raw * 1000.0
            else:
                # 旧缓存没有 amount，用价格近似
                amount_yuan = C * vol_shares

            # 2. 如果当日是实时数据，只覆盖 target 行（已经是股 / 元）
            if is_rt_today:
                vol_shares.iloc[target_idx] = V_raw.iloc[target_idx]
                if 'amount' in df_daily.columns:
                    amount_yuan.iloc[target_idx] = pd.to_numeric(df_daily['amount'], errors='coerce').iloc[target_idx]
                else:
                    amount_yuan.iloc[target_idx] = C.iloc[target_idx] * vol_shares.iloc[target_idx]

            # 填充可能的 NaN
            vol_shares = vol_shares.ffill().bfill().fillna(0)
            amount_yuan = amount_yuan.ffill().bfill().fillna(0)
            V = vol_shares

            # ========== 1. 肯特纳通道中轨 ==========
            TYP = (H + L + C) / 3
            TR1 = pd.concat([H - L, (H - C.shift(1)).abs(), (L - C.shift(1)).abs()], axis=1).max(axis=1)
            ATR = TR1.rolling(20).mean()
            KCM = TYP.ewm(span=20, adjust=False).mean()

            # ========== 2. 多周期均线与成本线 ==========
            BBI_MID = (C.rolling(3).mean() + C.rolling(6).mean() + C.rolling(12).mean() + C.rolling(24).mean()) / 4

            B1 = (C.rolling(3).mean() + C.rolling(5).mean() + C.rolling(8).mean() + C.rolling(13).mean()) / 4
            B2 = (C.rolling(5).mean() + C.rolling(10).mean() + C.rolling(15).mean() + C.rolling(22).mean()) / 4
            B3 = (C.rolling(6).mean() + C.rolling(12).mean() + C.rolling(24).mean() + C.rolling(48).mean()) / 4
            B4 = (C.rolling(12).mean() + C.rolling(24).mean() + C.rolling(48).mean() + C.rolling(96).mean()) / 4
            B5 = (C.rolling(24).mean() + C.rolling(48).mean() + C.rolling(96).mean() + C.rolling(192).mean()) / 4

            # AHC2 趋势线
            AHC1 = C * V
            AHC2 = (
                (AHC1.ewm(span=3, adjust=False).mean() / V.ewm(span=3, adjust=False).mean().replace(0, np.nan) +
                 AHC1.ewm(span=6, adjust=False).mean() / V.ewm(span=6, adjust=False).mean().replace(0, np.nan) +
                 AHC1.ewm(span=12, adjust=False).mean() / V.ewm(span=12, adjust=False).mean().replace(0, np.nan) +
                 AHC1.ewm(span=24, adjust=False).mean() / V.ewm(span=24, adjust=False).mean().replace(0, np.nan)) / 4
            ).ewm(span=13, adjust=False).mean()

            # 五条 AVWAP + 长线（统一用 元 / 股）
            v3 = V.rolling(3).sum().replace(0, np.nan)
            v5 = V.rolling(5).sum().replace(0, np.nan)
            v15 = V.rolling(15).sum().replace(0, np.nan)
            v22 = V.rolling(22).sum().replace(0, np.nan)
            v66 = V.rolling(66).sum().replace(0, np.nan)
            v20 = V.rolling(20).sum().replace(0, np.nan)
            v60 = V.rolling(60).sum().replace(0, np.nan)

            A1 = (amount_yuan.rolling(3).sum() / v3).ffill()
            A2 = (amount_yuan.rolling(5).sum() / v5).ffill()
            A3 = (amount_yuan.rolling(15).sum() / v15).ffill()
            A4 = (amount_yuan.rolling(22).sum() / v22).ffill()
            A5 = (amount_yuan.rolling(66).sum() / v66).ffill()
            AVWAP20 = (amount_yuan.rolling(20).sum() / v20).ffill()
            AVWAP60 = (amount_yuan.rolling(60).sum() / v60).ffill()

            # ========== 3. 基础条件 ==========
            C1 = ((C.shift(1) < O.shift(1)) | (C.shift(1) < C.shift(2))).fillna(False).astype(bool)
            C2 = (L.shift(1) < KCM.shift(1)).fillna(False).astype(bool)
            C3 = ((C > O) & (C > C.shift(1))).fillna(False).astype(bool)
            LB = (V / V.rolling(5).mean().shift(1).replace(0, np.nan) > 1.2).fillna(False).astype(bool)
            C4 = (KCM > KCM.shift(1)).fillna(False).astype(bool)

            # ========== 4. 形态条件 ==========
            IK = ((O <= KCM) & (KCM <= C)).fillna(False).astype(bool)
            IB1 = ((O <= B1) & (B1 <= C)).fillna(False).astype(bool)
            IB2 = ((O <= B2) & (B2 <= C)).fillna(False).astype(bool)
            IB3 = ((O <= B3) & (B3 <= C)).fillna(False).astype(bool)
            IB4 = ((O <= B4) & (B4 <= C)).fillna(False).astype(bool)
            IB5 = ((O <= B5) & (B5 <= C)).fillna(False).astype(bool)

            CNTB = IK.astype(int) + IB1.astype(int) + IB2.astype(int) + IB3.astype(int) + IB4.astype(int) + IB5.astype(int)
            MODEB = CNTB >= 6

            IA1 = ((O <= A1) & (A1 <= C)).fillna(False).astype(bool)
            IA2 = ((O <= A2) & (A2 <= C)).fillna(False).astype(bool)
            IA3 = ((O <= A3) & (A3 <= C)).fillna(False).astype(bool)
            IA4 = ((O <= A4) & (A4 <= C)).fillna(False).astype(bool)
            IA5 = ((O <= A5) & (A5 <= C)).fillna(False).astype(bool)

            CNTA = IK.astype(int) + IA1.astype(int) + IA2.astype(int) + IA3.astype(int) + IA4.astype(int) + IA5.astype(int)
            MODEA = CNTA >= 6

            INC_AHC2 = ((O <= AHC2) & (AHC2 <= C)).fillna(False).astype(bool)
            INC_CNT = IB1.astype(int) + IB2.astype(int) + IB3.astype(int) + IB4.astype(int) + IB5.astype(int) + INC_AHC2.astype(int)
            PART_INC = INC_CNT >= 4
            SHORT_UP = ((B1 > B1.shift(1)) & (B2 > B2.shift(1))).fillna(False).astype(bool)
            MODEC = PART_INC & SHORT_UP

            # ========== 5. 过滤与动能条件 ==========
            GAP3 = (O > C.shift(1) * 1.03).fillna(False).astype(bool)
            C5 = GAP3.rolling(3).sum() == 0

            DIF = C.ewm(span=12, adjust=False).mean() - C.ewm(span=26, adjust=False).mean()
            DEA = DIF.ewm(span=9, adjust=False).mean()
            MACDJC = ((DIF > DEA) & (DIF.shift(1) <= DEA.shift(1))).fillna(False).astype(bool)

            RSV = (C - L.rolling(14).min()) / (H.rolling(14).max() - L.rolling(14).min() + 1e-10) * 100
            KK = RSV.ewm(alpha=1/3, adjust=False).mean()
            DD = KK.ewm(alpha=1/3, adjust=False).mean()
            KDJJC = ((KK > DD) & (KK.shift(1) <= DD.shift(1))).fillna(False).astype(bool)

            C6 = MACDJC | KDJJC

            # ========== 6. AVWAP3上穿与斜率 ==========
            CR1 = ((A1 > KCM) & (A1.shift(1) <= KCM.shift(1))).fillna(False)
            CR2 = ((A1 > BBI_MID) & (A1.shift(1) <= BBI_MID.shift(1))).fillna(False)
            CR3 = ((A1 > AVWAP20) & (A1.shift(1) <= AVWAP20.shift(1))).fillna(False)
            CR4 = ((A1 > AVWAP60) & (A1.shift(1) <= AVWAP60.shift(1))).fillna(False)

            CR_TOTAL = CR1.astype(int) + CR2.astype(int) + CR3.astype(int) + CR4.astype(int)
            C7 = CR_TOTAL >= 2

            A1_SLOPE = (A1 - A1.shift(1)) / A1.shift(1).replace(0, np.nan) * 100
            C8 = (A1_SLOPE > 0.20).fillna(False).astype(bool)

            # ========== 7. 最终选股 ==========
            FINAL_SIGNAL = (
                C1 & C2 & C3 & LB & (MODEB | MODEA | MODEC) & C4 & C5 & C6 & C7 & C8
            )

            if bool(FINAL_SIGNAL.iloc[target_idx]):
                last_row = df_daily.iloc[target_idx]
                pct_chg = float(last_row['pct_chg']) if 'pct_chg' in df_daily.columns and not pd.isna(last_row['pct_chg']) else 0.0

                return {
                    'ts_code': ts_code,
                    'name': name,
                    'close': round(float(last_row['close']), 2),
                    'pct_chg': round(pct_chg, 2)
                }, 'selected'

            return None, 'condition_fail'

        except Exception as e:
            self._ex_count += 1
            if self._ex_count <= 5:          # 只打印前 5 个，方便排查
                print(f"\n{RED}[EXCEPTION] {ts_code} {name}{RESET}")
                print(traceback.format_exc())
            return None, 'exception'

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
        if not results:
            return
        mode = "_实时" if is_intraday else ""
        path = os.path.join(GlobalConfig.OUTPUT_DIR, f"终极共振_{trade_date}{mode}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"终极共振 + AVWAP3双穿爆发 - {trade_date}\n共选中 {len(results)} 只\n\n")
            for i, s in enumerate(results, 1):
                f.write(f"{i:3d}. {s['ts_code']} {s['name']} 价:{s['close']:.2f} 涨:{s['pct_chg']:+.2f}%\n")
        print(f"结果已保存: {path}")

def main():
    print("=" * 110)
    print(f"{BOLD} 终极共振 + AVWAP3双穿爆发 选股系统（最终稳健版） {RESET}")
    print("=" * 110)
    filter_tool = UltimateResonanceFilter(GlobalConfig.TUSHARE_TOKEN)

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
        print(f"{BOLD}{CYAN}【结果】终极共振 - {validated_date} - 共 {len(results)} 只{RESET}")
        for i, s in enumerate(results, 1):
            color = RED if i % 2 == 1 else YELLOW
            print(f"\n{color}{BOLD}  {i:2d}. {s['ts_code']} {s['name']:<8} 价:{s['close']:8.2f} 涨:{s['pct_chg']:+6.2f}%{RESET}")
    else:
        print(f"\n{YELLOW}本次未选出股票，请检查条件或换日期重试。{RESET}")

if __name__ == '__main__':
    main()