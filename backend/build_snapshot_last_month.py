import os
import sys
import logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

from app import build_monthly_snapshot, get_now_taipei

def run_last_month_snapshot():
    logging.info("==================================================")
    logging.info("開始執行【上個月 (last_month)】手動快照生成程式")
    logging.info("==================================================")
    
    try:
        now = get_now_taipei()
        # 取得上個月份的 YYYYMM
        first_of_this_month = now.replace(day=1)
        last_month_dt = first_of_this_month - timedelta(days=1)
        ym = last_month_dt.strftime("%Y%m")
        
        logging.info(f"\n[1/1] 正在處理上個月區間：{ym} ...")
        logging.info(f"   -> 產生上個月總覽與明細快照 (Monthly Snapshot)")
        
        build_monthly_snapshot(ym)
        
        logging.info(f"[1/1] 上個月區間處理完成！")
        
        logging.info("\n==================================================")
        logging.info(f"上個月快照建立完成！時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logging.info("==================================================")
    except Exception as e:
        logging.error(f"上個月快照處理失敗: {repr(e)}")

if __name__ == "__main__":
    run_last_month_snapshot()
