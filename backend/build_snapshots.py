import os
import sys
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

from app import build_l2_snapshot_for_k, build_l2_drill_for_k, get_current_slot_id

def run_routine_snapshots():
    logging.info("==================================================")
    logging.info("開始執行【例行期間】手動快照生成程式")
    logging.info("包含期間：7d, 14d, 28d, 90d, month")
    logging.info("==================================================")
    
    slot = get_current_slot_id()
    ranges = ["7d", "14d", "28d", "90d", "month"]
    total = len(ranges)
    
    for idx, k in enumerate(ranges, 1):
        logging.info(f"\n[{idx}/{total}] 正在處理區間：{k} ...")
        try:
            logging.info(f"   -> 產生 {k} 總覽快照 (L2 Snapshot)")
            build_l2_snapshot_for_k(k, slot)
            
            logging.info(f"   -> 產生 {k} 明細快照 (L2 Drill)")
            build_l2_drill_for_k(k, slot, limit=30000)
            
            logging.info(f"[{idx}/{total}] 區間 {k} 處理完成！")
        except Exception as e:
            logging.error(f"[{idx}/{total}] 區間 {k} 處理失敗: {repr(e)}")
            
    logging.info("\n==================================================")
    logging.info(f"所有例行期間快照建立完成！時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logging.info("==================================================")

if __name__ == "__main__":
    run_routine_snapshots()
