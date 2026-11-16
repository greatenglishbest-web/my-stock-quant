import os

class GlobalConfig:
    TUSHARE_TOKEN = 'a91652800def39d213abec3af9563e4485e83940bfbbd8276f8adaa8'

    # 智能路径适配：自动识别本地(Windows)还是云端(Linux)
    if os.name == 'nt':
        # 本地 Windows 电脑：继续使用你强大的桌面缓存和输出目录
        OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', '双轨共振_选股结果')
        CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股日线公共缓存')
    else:
        # 云端 Linux 环境：自动使用项目内或云端安全的相对路径
        base_path = os.path.dirname(os.path.abspath(__file__)) if '__file__' in locals() else os.getcwd()
        CACHE_DIR = os.path.join(base_path, 'cache')
        OUTPUT_DIR = os.path.join(base_path, 'cloud_output')

    LOOKBACK_DAYS = 500
    USE_PARQUET = True
