#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mock Data Generator for Quality Report System
產生去識別化的模擬 snapshot 與 mock_snapshots.json 資料
"""

import os
import json
import random
from datetime import datetime, timezone, timedelta

TAIPEI_TZ = timezone(timedelta(hours=8))

def get_now_taipei():
    return datetime.now(timezone.utc).astimezone(TAIPEI_TZ)

def quality_score(er: float, dur: float) -> int:
    er = float(er or 0.0)
    dur = float(dur or 0.0)
    d = dur / 60.0
    if d > 1.0:
        d = 1.0
    s = (er * 0.7 + d * 0.3) * 100.0
    return int(round(max(0.0, min(100.0, s))))

PRODUCTS_CONFIG = {
    "個人護理與美妝": ["控油洗髮精", "保濕精華液", "防曬乳液", "抗老修護霜"],
    "食品與飲料": ["無糖氣泡水", "有機燕麥奶", "低卡零食", "冷萃黑咖啡"],
    "家庭與清潔用品": ["濃縮洗衣精", "植萃洗碗精", "除霉清潔劑", "地板除菌清潔劑"],
    "母嬰與保健": ["親膚濕紙巾", "高純度膠原蛋白", "綜合維他命", "專利益生菌"],
    "寵物用品與食品": ["無穀貓糧", "高肉糧狗糧", "低粉塵豆腐貓砂", "寵物關節保健粉"]
}

SOURCE_GROUPS = [
    {"group": "Google 搜尋", "source": "google / organic", "sub": "自然搜尋"},
    {"group": "Meta 廣告", "source": "facebook / cpc", "sub": "社群廣告"},
    {"group": "EDM 電子報", "source": "edm / newsletter", "sub": "會員電子報"},
    {"group": "直接流量", "source": "(direct) / (none)", "sub": "直接造訪"},
    {"group": "LINE 官方帳號", "source": "line / oa", "sub": "推播訊息"},
    {"group": "IG 網紅引流", "source": "instagram / referral", "sub": "KOL/KOC導流"}
]

PAGE_TEMPLATES = {
    "news_id": [
        {"id": "1001", "name": "2026 夏季新品上市：控油洗髮精強效持香與蓬鬆控油成分公開", "prod": "個人護理與美妝", "pdet": "控油洗髮精"},
        {"id": "1002", "name": "115 年健康飲食趨勢：有機燕麥奶與無糖氣泡水引領飲品新浪潮", "prod": "食品與飲料", "pdet": "有機燕麥奶"},
        {"id": "1003", "name": "環保潔淨新標準：濃縮洗衣精植萃防蟎抗菌配方認證公告", "prod": "家庭與清潔用品", "pdet": "濃縮洗衣精"},
        {"id": "1004", "name": "母嬰護理新震撼：親膚濕紙巾敏弱肌適用測試報告出爐", "prod": "母嬰與保健", "pdet": "親膚濕紙巾"},
        {"id": "1005", "name": "寵物健康升級：無穀貓糧主食罐高蛋白升級配方隆重登場", "prod": "寵物用品與食品", "pdet": "無穀貓糧"}
    ],
    "article_id": [
        {"id": "2001", "name": "換季肌膚抗乾敏攻略：保濕精華液與抗老修護霜完美搭配使用指南", "prod": "個人護理與美妝", "pdet": "保濕精華液"},
        {"id": "2002", "name": "辦公室上班族必看：冷萃黑咖啡搭配低卡零食熱量控制與提神技巧", "prod": "食品與飲料", "pdet": "冷萃黑咖啡"},
        {"id": "2003", "name": "居家大掃除省時祕訣：除霉清潔劑與地板除菌清潔劑正確使用步驟", "prod": "家庭與清潔用品", "pdet": "除霉清潔劑"},
        {"id": "2004", "name": "消化道日常保健指南：專利益生菌與綜合維他命挑選四大關鍵", "prod": "母嬰與保健", "pdet": "專利益生菌"},
        {"id": "2005", "name": "毛孩關節保養全攻略：高肉糧狗糧與寵物關節保健粉日常餵食法", "prod": "寵物用品與食品", "pdet": "高肉糧狗糧"}
    ],
    "comment_id": [
        {"id": "3001", "name": "夏日油頭救星！控油洗髮精一週實測控油力與頭皮蓬鬆感體驗", "prod": "個人護理與美妝", "pdet": "控油洗髮精"},
        {"id": "3002", "name": "低粉塵豆腐貓砂開箱試用：除臭力與快速結塊實測報告", "prod": "寵物用品與食品", "pdet": "低粉塵豆腐貓砂"},
        {"id": "3003", "name": "植萃洗碗精溫和不傷手真實體驗：油膩重油鍋具洗淨評測", "prod": "家庭與清潔用品", "pdet": "植萃洗碗精"},
        {"id": "3004", "name": "高純度膠原蛋白飲飲用 30 天膚況彈潤紀錄與風味口感心得", "prod": "母嬰與保健", "pdet": "高純度膠原蛋白"}
    ],
    "lecture_id": [
        {"id": "4001", "name": "2026 線上保養體驗會：防曬乳液防護力與水感質地解析", "prod": "個人護理與美妝", "pdet": "防曬乳液"},
        {"id": "4002", "name": "低卡健康飲食線上直播：無糖氣泡水調飲與特調食譜教學", "prod": "食品與飲料", "pdet": "無糖氣泡水"},
        {"id": "4003", "name": "綠色家居清潔直播研討會：植萃洗碗精無毒安全防護解析", "prod": "家庭與清潔用品", "pdet": "植萃洗碗精"},
        {"id": "4004", "name": "新手貓奴線上講座：無穀貓糧挑選與換糧常見問題指引", "prod": "寵物用品與食品", "pdet": "無穀貓糧"}
    ],
    "edm": [
        {"id": "edm101", "name": "EDM 101: 2026 新品上市！保濕精華液限時體驗組免費領取", "prod": "個人護理與美妝", "pdet": "保濕精華液"},
        {"id": "edm102", "name": "EDM 102: 夏季箱購特惠！無糖氣泡水整箱免運再享 88 折", "prod": "食品與飲料", "pdet": "無糖氣泡水"},
        {"id": "edm103", "name": "EDM 103: 狂歡寵物節！低粉塵豆腐貓砂買二送一促銷活動", "prod": "寵物用品與食品", "pdet": "低粉塵豆腐貓砂"},
        {"id": "edm104", "name": "EDM 104: 保健週年慶！專利益生菌買大送小限時搶購", "prod": "母嬰與保健", "pdet": "專利益生菌"}
    ],
    "f_subject_no": [
        {"id": "501", "name": "專題 501: 玻尿酸與神經醯胺黃金配比保濕技術解析", "prod": "個人護理與美妝", "pdet": "保濕精華液"},
        {"id": "502", "name": "專題 502: 植物萃取界面活性劑去油與溫和配方原理", "prod": "家庭與清潔用品", "pdet": "植萃洗碗精"},
        {"id": "503", "name": "專題 503: 專利益生菌三層包埋技術與定殖率實驗", "prod": "母嬰與保健", "pdet": "專利益生菌"}
    ],
    "subject_no": [
        {"id": "601", "name": "單元 601: 物理性防曬與化學性防曬清爽度成份評比", "prod": "個人護理與美妝", "pdet": "防曬乳液"},
        {"id": "602", "name": "單元 602: 冷萃工藝對咖啡因與咖啡多酚風味保留影響", "prod": "食品與飲料", "pdet": "冷萃黑咖啡"}
    ]
}

MOCK_QUERIES = [
    {"q": "2026 控油洗髮精 推薦 蓬鬆", "impr": 15800, "clicks": 2350, "ctr": 0.1487, "pos": 1.8},
    {"q": "保濕精華液 乾敏肌 評價 評測", "impr": 12400, "clicks": 1680, "ctr": 0.1355, "pos": 2.3},
    {"q": "無糖氣泡水 箱購 免運 折扣", "impr": 9800, "clicks": 1150, "ctr": 0.1173, "pos": 3.1},
    {"q": "有機燕麥奶 拿鐵 特調 搭配", "impr": 8900, "clicks": 980, "ctr": 0.1101, "pos": 4.0},
    {"q": "低粉塵 豆腐貓砂 除臭 結塊 評比", "impr": 7600, "clicks": 820, "ctr": 0.1079, "pos": 5.2},
    {"q": "濃縮洗衣精 植萃 防蟎 價格", "impr": 6500, "clicks": 620, "ctr": 0.0954, "pos": 6.1},
    {"q": "專利益生菌 腸道保健 品牌 比較", "impr": 5400, "clicks": 510, "ctr": 0.0944, "pos": 4.8},
    {"q": "防曬乳液 清爽 不黏膩 卸妝", "impr": 4300, "clicks": 390, "ctr": 0.0907, "pos": 7.5}
]

def generate_drill_rows():
    rows = []
    for cat, templates in PAGE_TEMPLATES.items():
        for t in templates:
            for src_info in SOURCE_GROUPS:
                # 隨機生成合理指標
                users = random.randint(300, 3500)
                sessions = int(users * random.uniform(1.15, 1.35))
                er = round(random.uniform(0.45, 0.88), 4)
                dur = round(random.uniform(50.0, 240.0), 1)
                
                param_key = cat if cat != "edm" else "edm"
                url = f"https://demo.example.com/page?{param_key}={t['id']}"
                
                clicks = int(users * random.uniform(0.4, 0.85)) if "organic" in src_info["source"] else random.randint(10, 200)
                impr = clicks * random.randint(8, 20)
                pos = round(random.uniform(1.8, 18.5), 1)
                
                rows.append({
                    "source_raw": src_info["source"],
                    "source": src_info["group"],
                    "source_name": src_info["group"],
                    "source_group": src_info["group"],
                    "source_sub": src_info["sub"],
                    "lp": url,
                    "display_title": t["name"],
                    "product": t["prod"],
                    "product_detail": t["pdet"],
                    "category": cat,
                    "pageId": t["id"],
                    "users": users,
                    "sessions": sessions,
                    "engagementRate": er,
                    "avgDuration": dur,
                    "avgSessionsPerUser": round(sessions / users, 2),
                    "score": quality_score(er, dur),
                    "gscClicks": clicks,
                    "gscImpressions": impr,
                    "gscPosition": pos
                })
    return rows

def generate_page_metrics_from_rows(rows):
    page_metrics = {cat: [] for cat in PAGE_TEMPLATES.keys()}
    grouped = {}
    for r in rows:
        cat = r["category"]
        pid = r["pageId"]
        key = f"{cat}_{pid}"
        if key not in grouped:
            grouped[key] = {
                "id": pid,
                "name": r["display_title"],
                "category": cat,
                "product": r["product"],
                "product_detail": r["product_detail"],
                "views": 0,
                "er_w": 0.0,
                "dur_w": 0.0,
                "gscClicks": 0,
                "gscImpressions": 0,
                "gscPosW": 0.0,
                "gscClicksForPos": 0,
                "url": r["lp"]
            }
        g = grouped[key]
        g["views"] += r["users"]
        g["er_w"] += r["users"] * r["engagementRate"]
        g["dur_w"] += r["users"] * r["avgDuration"]
        g["gscClicks"] += r["gscClicks"]
        g["gscImpressions"] += r["gscImpressions"]
        if r["gscPosition"] > 0 and r["gscClicks"] > 0:
            g["gscPosW"] += r["gscPosition"] * r["gscClicks"]
            g["gscClicksForPos"] += r["gscClicks"]

    for key, item in grouped.items():
        v = item["views"]
        pos = item["gscPosW"] / item["gscClicksForPos"] if item["gscClicksForPos"] > 0 else 0.0
        ctr = item["gscClicks"] / item["gscImpressions"] if item["gscImpressions"] > 0 else 0.0
        cat = item["category"]
        page_metrics[cat].append({
            "id": item["id"],
            "name": item["name"],
            "views": v,
            "engagementRate": round(item["er_w"] / v, 4) if v > 0 else 0.0,
            "avgSec": round(item["dur_w"] / v, 1) if v > 0 else 0.0,
            "gscClicks": item["gscClicks"],
            "gscImpressions": item["gscImpressions"],
            "gscCtr": round(ctr, 4),
            "gscPosition": round(pos, 1),
            "gscPageUrl": item["url"],
            "category": cat
        })
        
    for cat in page_metrics:
        page_metrics[cat].sort(key=lambda x: x["views"], reverse=True)
    return page_metrics

def generate_diagnostics_top5(page_metrics, page_metrics_yoy):
    scored = []
    for cat, items in page_metrics.items():
        for item in items:
            impr = item["gscImpressions"]
            clicks = item["gscClicks"]
            ctr = item["gscCtr"]
            pos = item["gscPosition"]
            
            # Find yoy
            yoy_item = next((y for y in page_metrics_yoy.get(cat, []) if y["id"] == item["id"]), None)
            yoy_clicks = yoy_item["gscClicks"] if yoy_item else int(clicks * 1.3)
            yoy_impr = yoy_item["gscImpressions"] if yoy_item else int(impr * 1.2)
            yoy_ctr = yoy_item["gscCtr"] if yoy_item else ctr * 1.2
            yoy_pos = yoy_item["gscPosition"] if yoy_item else max(1.0, pos - 2.0)
            
            click_loss = max(0, yoy_clicks - clicks)
            
            if clicks < yoy_clicks and ctr < yoy_ctr and pos > yoy_pos:
                scored.append({
                    "id": item["id"],
                    "name": item["name"],
                    "gscPageUrl": item["gscPageUrl"],
                    "category": cat,
                    "score": 95,
                    "archetype": "🚨 排名衰退 (SEO 異常)",
                    "symptom": f"點擊流失 {click_loss:,} 次",
                    "gscPhenomenon": "點擊↓ 曝光↓ 排名下降",
                    "priorityAction": "補強商品賣點、優化關鍵字佈局與KOC評測開箱連結",
                    "gscClicks": clicks,
                    "gscImpressions": impr,
                    "gscCtr": ctr,
                    "gscPosition": pos,
                    "deltaClicks": clicks - yoy_clicks,
                    "deltaImpr": impr - yoy_impr
                })
            elif impr >= yoy_impr and clicks < yoy_clicks and ctr < yoy_ctr:
                scored.append({
                    "id": item["id"],
                    "name": item["name"],
                    "gscPageUrl": item["gscPageUrl"],
                    "category": cat,
                    "score": 85,
                    "archetype": "⚠️ CTR不足 (SERP 問題)",
                    "symptom": f"點擊流失 {click_loss:,} 次",
                    "gscPhenomenon": "曝光未降 排名穩 CTR低",
                    "priorityAction": "重寫 SERP 商品主標題，強調限時優惠與滿額贈誘因",
                    "gscClicks": clicks,
                    "gscImpressions": impr,
                    "gscCtr": ctr,
                    "gscPosition": pos,
                    "deltaClicks": clicks - yoy_clicks,
                    "deltaImpr": impr - yoy_impr
                })
            elif pos >= 4.0 and pos <= 15.0:
                scored.append({
                    "id": item["id"],
                    "name": item["name"],
                    "gscPageUrl": item["gscPageUrl"],
                    "category": cat,
                    "score": 70,
                    "archetype": "🚀 快進首頁 (臨門一腳)",
                    "symptom": f"搜尋排名第 {pos:.1f} 名",
                    "gscPhenomenon": "排名 4-15 名 曝光高",
                    "priorityAction": "加強導購頁內部連結、嵌入試用開箱影片與口碑評測",
                    "gscClicks": clicks,
                    "gscImpressions": impr,
                    "gscCtr": ctr,
                    "gscPosition": pos,
                    "deltaClicks": clicks - yoy_clicks,
                    "deltaImpr": impr - yoy_impr
                })
            elif clicks < yoy_clicks and impr < yoy_impr and ctr >= yoy_ctr:
                scored.append({
                    "id": item["id"],
                    "name": item["name"],
                    "gscPageUrl": item["gscPageUrl"],
                    "category": cat,
                    "score": 60,
                    "archetype": "❄️ 搜尋需求下降 (淡季波動)",
                    "symptom": f"大環境熱度微降",
                    "gscPhenomenon": "點擊與曝光雙降 排名持平",
                    "priorityAction": "結合季節議題操作，同步啟動 LINE 官方帳號與社群推播",
                    "gscClicks": clicks,
                    "gscImpressions": impr,
                    "gscCtr": ctr,
                    "gscPosition": pos,
                    "deltaClicks": clicks - yoy_clicks,
                    "deltaImpr": impr - yoy_impr
                })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:10]

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:10]

def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    snapshots_dir = os.path.join(base_dir, "snapshots")
    os.makedirs(snapshots_dir, exist_ok=True)
    
    # 清空舊快照檔，避免舊考試資料殘留
    for fname in os.listdir(snapshots_dir):
        if fname.endswith(".json"):
            try:
                os.remove(os.path.join(snapshots_dir, fname))
            except Exception:
                pass

    tz_now = get_now_taipei()
    today_str = tz_now.strftime("%Y%m%d")
    yesterday_str = (tz_now - timedelta(days=1)).strftime("%Y%m%d")
    
    slots = [
        f"{today_str}-18", f"{today_str}-12", f"{today_str}-06", f"{today_str}-00",
        f"{yesterday_str}-18", f"{yesterday_str}-12", f"{yesterday_str}-06", f"{yesterday_str}-00",
        "20260626-06", "20260702-06", "20260615-12"
    ]
    ranges = ["7d", "14d", "28d", "90d", "month", "last_month"]
    
    filters_data = {
        "products": list(PRODUCTS_CONFIG.keys()),
        "sourceGroups": [s["group"] for s in SOURCE_GROUPS],
        "productDetailsByProduct": PRODUCTS_CONFIG
    }
    
    mock_master = {}

    for k in ranges:
        start_dt = tz_now - timedelta(days=28)
        end_dt = tz_now
        start_str = start_dt.strftime("%Y-%m-%d")
        end_str = end_dt.strftime("%Y-%m-%d")
        
        cur_rows = generate_drill_rows()
        prev_rows = generate_drill_rows()
        yoy_rows = generate_drill_rows()
        
        cur_pm = generate_page_metrics_from_rows(cur_rows)
        prev_pm = generate_page_metrics_from_rows(prev_rows)
        yoy_pm = generate_page_metrics_from_rows(yoy_rows)
        
        diagnostics = generate_diagnostics_top5(cur_pm, yoy_pm)
        
        # Calculate summary KPI
        tot_users = sum(r["users"] for r in cur_rows)
        tot_sessions = sum(r["sessions"] for r in cur_rows)
        tot_clicks = sum(r["gscClicks"] for r in cur_rows)
        tot_impr = sum(r["gscImpressions"] for r in cur_rows)
        avg_pos = round(sum(r["gscPosition"] * r["gscClicks"] for r in cur_rows if r["gscClicks"] > 0) / max(1, tot_clicks), 1)
        
        kpi_cur = {
            "users": tot_users,
            "pageViews": tot_sessions * 2,
            "sessions": tot_sessions,
            "engagementRate": 0.68,
            "avgSessionSec": 142.5,
            "gscClicks": tot_clicks,
            "gscImpressions": tot_impr,
            "gscCtr": round(tot_clicks / max(1, tot_impr), 4),
            "gscPosition": avg_pos
        }
        
        kpi_prev = {k: (v * 0.92 if isinstance(v, (int, float)) else v) for k, v in kpi_cur.items()}
        kpi_yoy = {k: (v * 0.85 if isinstance(v, (int, float)) else v) for k, v in kpi_cur.items()}
        
        for slot in slots:
            # l2_kshot
            kshot_data = {
                "_snapshot": {
                    "type": "l2_kshot",
                    "k": k,
                    "slot": slot,
                    "startDate": start_str,
                    "endDate": end_str,
                    "createdAt": tz_now.isoformat(),
                    "version": "v1"
                },
                "filters": filters_data,
                "cur": {
                    "kpi": kpi_cur,
                    "heatAgg": cur_rows[:50],
                    "heatAggByProduct": cur_rows[:50],
                    "gscRaw": {}
                },
                "prev": {
                    "kpi": kpi_prev,
                    "heatAgg": prev_rows[:50],
                    "heatAggByProduct": prev_rows[:50],
                    "gscRaw": {}
                },
                "yoy": {
                    "kpi": kpi_yoy,
                    "heatAgg": yoy_rows[:50],
                    "heatAggByProduct": yoy_rows[:50],
                    "gscRaw": {}
                }
            }
            
            kshot_filename = f"l2_kshot__{slot}__k__{k}.json"
            with open(os.path.join(snapshots_dir, kshot_filename), "w", encoding="utf-8") as f:
                json.dump(kshot_data, f, ensure_ascii=False, indent=2)
                
            # l2_drill
            drill_data = {
                "_snapshot": {
                    "type": "l2_drill",
                    "k": k,
                    "slot": slot,
                    "startDate": start_str,
                    "endDate": end_str,
                    "createdAt": tz_now.isoformat(),
                    "version": "v1",
                    "limit": 30000
                },
                "rows": cur_rows,
                "cur": {"heat": cur_rows, "page": cur_pm},
                "prev": {"heat": prev_rows, "page": prev_pm},
                "yoy": {"heat": yoy_rows, "page": yoy_pm},
                "pageMetrics": cur_pm,
                "pageMetrics_prev": prev_pm,
                "pageMetrics_yoy": yoy_pm,
                "diagnosticsTop5": diagnostics
            }
            
            drill_filename = f"l2_drill__{slot}__k__{k}.json"
            with open(os.path.join(snapshots_dir, drill_filename), "w", encoding="utf-8") as f:
                json.dump(drill_data, f, ensure_ascii=False, indent=2)
                
        if k == "last_month":
            for ym_str in ["202609", "202608", "202607", "202606", "202605"]:
                monthly_filename = f"monthly__{ym_str}__k__last_month.json"
                monthly_data = dict(drill_data)
                monthly_data["_snapshot"]["type"] = "monthly"
                monthly_data["_snapshot"]["ym"] = ym_str
                with open(os.path.join(snapshots_dir, monthly_filename), "w", encoding="utf-8") as f:
                    json.dump(monthly_data, f, ensure_ascii=False, indent=2)

        mock_master[k] = drill_data

    mock_master["_meta"] = {
        "generatedAt": tz_now.isoformat(),
        "slot": f"{today_str}-12",
        "queries": MOCK_QUERIES,
        "filters": filters_data
    }

    mock_json_path = os.path.join(base_dir, "mock_snapshots.json")
    with open(mock_json_path, "w", encoding="utf-8") as f:
        json.dump(mock_master, f, ensure_ascii=False, indent=2)
        
    print(f"[SUCCESS] Successfully generated mock_snapshots.json and snapshot files in {snapshots_dir}")

if __name__ == "__main__":
    main()

