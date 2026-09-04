import os
import re
import json
import csv
import logging
import urllib.parse
import requests
import time
from datetime import datetime, timedelta, timezone
import math
from typing import Optional, List, Dict, Any, Tuple

from fastapi import FastAPI, Query, BackgroundTasks, HTTPException, Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import pandas as pd

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    RunReportRequest,
    DateRange,
    Dimension,
    Metric,
)
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from apscheduler.schedulers.background import BackgroundScheduler

# 設定 Log
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("QualityReport")

app = FastAPI(title="Quality Report Local Backend")

# 允許 CORS 方便偵錯
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 常數設定
GA4_PROPERTY_ID = "257689285"  # 移除 properties/ 前綴，API 需要純數字或含前綴，程式內會處理
MAPPING_SHEET_ID = "1smzqQJiWndifWLUMgTVRMTfWjQe71DVzldNtgBYt26g"

import sys
def get_base_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

SNAPSHOT_DIR = os.path.join(get_base_dir(), "snapshots")
MAPPING_DIR = os.path.join(get_base_dir(), "mappings")

os.makedirs(SNAPSHOT_DIR, exist_ok=True)
os.makedirs(MAPPING_DIR, exist_ok=True)

# 台北時區 (UTC+8)
TAIPEI_TZ = timezone(timedelta(hours=8))

# 全域變數快取對照表，避免每次查詢都去下載
_cached_page_map = None
_cached_maps = None

# =====================================================================
# 1. 台北時間與 Slot 小工具
# =====================================================================
def get_now_taipei() -> datetime:
    return datetime.now(timezone.utc).astimezone(TAIPEI_TZ)

def get_current_slot_id() -> str:
    now = get_now_taipei()
    hh = now.hour
    if hh < 6:
        slot_hh = "00"
    elif hh < 12:
        slot_hh = "06"
    elif hh < 18:
        slot_hh = "12"
    else:
        slot_hh = "18"
    return now.strftime("%Y%m%d") + "-" + slot_hh

def date_range_for_k(k: str) -> Dict[str, str]:
    now = get_now_taipei()
    end_date = now.strftime("%Y-%m-%d")
    if k == "month":
        start_date = now.replace(day=1).strftime("%Y-%m-%d")
    else:
        days_map = {"7d": 7, "14d": 14, "28d": 28, "90d": 90}
        days = days_map.get(k, 28)
        start_date = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return {"startDate": start_date, "endDate": end_date}

def compute_prev_and_yoy_ranges(start_str: str, end_str: str) -> Dict[str, Dict[str, str]]:
    s = datetime.strptime(start_str, "%Y-%m-%d")
    e = datetime.strptime(end_str, "%Y-%m-%d")
    len_days = (e - s).days + 1
    
    prev_end = s - timedelta(days=1)
    prev_start = prev_end - timedelta(days=len_days - 1)
    
    def shift_year(dt: datetime) -> datetime:
        try:
            return dt.replace(year=dt.year - 1)
        except ValueError:
            return dt - timedelta(days=365)
            
    yoy_start = shift_year(s)
    yoy_end = shift_year(e)
    
    return {
        "prev": {"start": prev_start.strftime("%Y-%m-%d"), "end": prev_end.strftime("%Y-%m-%d")},
        "yoy": {"start": yoy_start.strftime("%Y-%m-%d"), "end": yoy_end.strftime("%Y-%m-%d")}
    }

# =====================================================================
# 2. GA4 及 GSC API Client 初始化
# =====================================================================
SCOPES = [
    'https://www.googleapis.com/auth/analytics.readonly',
    'https://www.googleapis.com/auth/webmasters.readonly',
]

def is_mock_mode() -> bool:
    env_mock = os.environ.get("MOCK_MODE", "").lower() in ("1", "true", "yes")
    if env_mock:
        return True
    token_path = os.path.join(get_base_dir(), "token.json")
    creds_path = os.path.join(get_base_dir(), "credentials.json")
    secrets_path = os.path.join(get_base_dir(), "client_secrets.json")
    has_creds = os.path.exists(token_path) or os.path.exists(creds_path) or os.path.exists(secrets_path)
    return not has_creds

def load_mock_snapshots_master() -> Optional[Dict[str, Any]]:
    mock_path = os.path.join(get_base_dir(), "mock_snapshots.json")
    if os.path.exists(mock_path):
        try:
            with open(mock_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"載入 mock_snapshots.json 失敗: {repr(e)}")
    return None

_cached_creds = None

def _get_creds():
    """取得共用的 OAuth credentials（GA4 和 GSC 共用）"""
    if is_mock_mode():
        return None
    global _cached_creds
    if _cached_creds and _cached_creds.valid:
        return _cached_creds
    
    token_path = os.path.join(get_base_dir(), "token.json")
    secrets_path = os.path.join(get_base_dir(), "client_secrets.json")
    creds = None
    
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception as e:
            logger.error(f"從 token.json 載入憑證失敗: {repr(e)}")
            
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(token_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
                logger.info("自動刷新 Token 成功")
            except Exception as e:
                logger.error(f"刷新 Token 失敗: {repr(e)}，需要重新授權")
                creds = None
                
        if not creds:
            if os.path.exists(secrets_path):
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(secrets_path, SCOPES)
                    creds = flow.run_local_server(port=0)
                    with open(token_path, "w", encoding="utf-8") as f:
                        f.write(creds.to_json())
                    logger.info("本地 OAuth 授權成功，生成 token.json")
                except Exception as e:
                    logger.error(f"本地 OAuth 授權失敗: {repr(e)}")
                    return None
            else:
                logger.error("找不到 client_secrets.json 檔案，且無 token.json，無法驗證身分。")
                return None
    
    _cached_creds = creds
    return creds

def get_ga4_client() -> Optional[BetaAnalyticsDataClient]:
    if is_mock_mode():
        return None
    creds = _get_creds()
    if creds:
        try:
            return BetaAnalyticsDataClient(credentials=creds)
        except Exception as e:
            logger.error(f"建立 GA4 Client 失敗: {repr(e)}")
    return None

_cached_gsc_service = None
_cached_gsc_site_url = None

import threading
_gsc_local = threading.local()

def get_gsc_service():
    """取得 Google Search Console API 服務 (Thread-Safe)"""
    if is_mock_mode():
        return None
    if hasattr(_gsc_local, 'service') and _gsc_local.service:
        return _gsc_local.service
        
    creds = _get_creds()
    if not creds:
        return None
    try:
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
        from googleapiclient.discovery import build
        
        # 每個 Thread 獨立建立 Http instance 避免 SSL WRONG_VERSION_NUMBER 錯誤
        authorized_http = AuthorizedHttp(creds, http=httplib2.Http())
        
        # cache_discovery=False 避免多執行緒寫入 cache 發生衝突
        service = build('searchconsole', 'v1', http=authorized_http, cache_discovery=False)
        _gsc_local.service = service
        logger.info(f"Search Console API 服務建立成功 (Thread: {threading.get_ident()})")
        return service
    except Exception as e:
        logger.error(f"建立 GSC Service 失敗: {repr(e)}")
        return None

def get_gsc_site_url() -> Optional[str]:
    """Auto-detect the Search Console site URL"""
    if is_mock_mode():
        return "https://demo.example.com/"
    global _cached_gsc_site_url
    if _cached_gsc_site_url:
        return _cached_gsc_site_url
    service = get_gsc_service()
    if not service:
        return None
    try:
        sites = service.sites().list().execute()
        site_list = sites.get('siteEntry', [])
        if site_list:
            # 優先選擇驗證網站
            for s in site_list:
                if 'demo' in s['siteUrl'].lower() or 'example' in s['siteUrl'].lower():
                    _cached_gsc_site_url = s['siteUrl']
                    logger.info(f"GSC 網站: {_cached_gsc_site_url}")
                    return _cached_gsc_site_url
            _cached_gsc_site_url = site_list[0]['siteUrl']
            logger.info(f"GSC 網站 (預設): {_cached_gsc_site_url}")
            return _cached_gsc_site_url
    except Exception as e:
        logger.error(f"取得 GSC 網站列表失敗: {repr(e)}")
    return None

def gsc_query_analytics(site_url: str, start_date: str, end_date: str,
                        dimensions: List[str] = None, page_filter: str = None,
                        page_regex_filter: str = None,
                        row_limit: int = 1000) -> List[Dict[str, Any]]:
    """通用 GSC searchanalytics.query wrapper"""
    service = get_gsc_service()
    if not service:
        return []
    body = {
        'startDate': start_date,
        'endDate': end_date,
        'rowLimit': row_limit
    }
    if dimensions:
        body['dimensions'] = dimensions
    if page_filter:
        body['dimensionFilterGroups'] = [{
            'filters': [{
                'dimension': 'page',
                'operator': 'contains',
                'expression': page_filter
            }]
        }]
    if page_regex_filter:
        body['dimensionFilterGroups'] = [{
            'filters': [{
                'dimension': 'page',
                'operator': 'includingRegex',
                'expression': page_regex_filter
            }]
        }]
    for attempt in range(3):
        try:
            result = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
            return result.get('rows', [])
        except Exception as e:
            logger.error(f"GSC searchanalytics.query 失敗 (attempt {attempt+1}): {repr(e)}")
            time.sleep(1 + attempt)
    return []

def gsc_query_property_level(site_url: str, start_date: str, end_date: str, page_regex_filter: str = None) -> Dict[str, Any]:
    """執行不帶 page 維度的 GSC API 查詢，取得資源層級 (Property-level) 去重後的絕對加總。"""
    rows = gsc_query_analytics(site_url, start_date, end_date, dimensions=["date"], page_regex_filter=page_regex_filter, row_limit=10000)
    
    total_clicks = 0
    total_impressions = 0
    total_pos_w = 0.0
    total_pos_clicks = 0
    
    for row in rows:
        clicks = int(row.get('clicks', 0))
        impr = int(row.get('impressions', 0))
        pos = float(row.get('position', 0.0))
        
        total_clicks += clicks
        total_impressions += impr
        if pos > 0 and clicks > 0:
            total_pos_w += pos * clicks
            total_pos_clicks += clicks
            
    ctr = total_clicks / total_impressions if total_impressions > 0 else 0.0
    pos_avg = total_pos_w / total_pos_clicks if total_pos_clicks > 0 else 0.0
    
    return {
        "gscClicks": total_clicks,
        "gscImpressions": total_impressions,
        "gscCtr": ctr,
        "gscPosition": pos_avg
    }

# =====================================================================
# 3. URL 與對照解析小工具
# =====================================================================
def safe_decode(s: str) -> str:
    if not s:
        return ""
    try:
        return urllib.parse.unquote(s.replace("+", " "))
    except Exception:
        return s

def _pick_param_(url: str, name: str) -> str:
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        val = params.get(name)
        return val[0] if val else ""
    except Exception:
        return ""

def _extract_edm_key_from_url_(url: str, row: Dict[str, Any]) -> str:
    url = str(url or "")
    # 1) ?edm=721
    v = _pick_param_(url, "edm")
    if v:
        m1 = re.search(r"edm?(\d{1,6})", v, re.IGNORECASE)
        if m1:
            return "edm" + m1.group(1).lower()
        return "edm" + re.sub(r"[^\d]", "", v)
        
    # 2) 參數名本身就是 edm721
    try:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        for k in params.keys():
            m2 = re.match(r"^edm\d{1,6}$", k, re.IGNORECASE)
            if m2:
                return k.lower()
    except Exception:
        pass
        
    # 3) 路徑 /go_edm/edm721
    m3 = re.search(r"\/(?:go_edm\/)?(edm\d{1,6})(?=\/|[?#]|$)", url, re.IGNORECASE)
    if m3:
        return m3.group(1).lower()
        
    # 4) utm/source/campaign 中包含 edm721
    source = _pick_param_(url, "utm_source")
    medium = _pick_param_(url, "utm_medium")
    campaign = _pick_param_(url, "utm_campaign")
    row_src = row.get("source") or row.get("source_name") or ""
    row_camp = row.get("campaign") or ""
    
    whole = "_".join([source, medium, campaign, str(row_src), str(row_camp)])
    m4 = re.search(r"edm(\d{1,6})", whole, re.IGNORECASE)
    if m4:
        return "edm" + m4.group(1)
    return ""

def quality_score(er: float, dur: float) -> int:
    er = float(er or 0.0)
    dur = float(dur or 0.0)
    d = dur / 60.0
    if d > 1.0:
        d = 1.0
    s = (er * 0.7 + d * 0.3) * 100.0
    return int(round(max(0.0, min(100.0, s))))

# =====================================================================
# 4. 對照表同步與讀取邏輯 (Google Sheets CSV 串接)
# =====================================================================
def fetch_sheet_csv_online(sheet_name: str) -> str:
    # 5 個分頁名稱對應的 GID，使用直導 CSV 避開 gviz/tq 自動類型推斷的抹除值 Bug
    gid_map = {
        "adno": "52788684",
        "print_id": "74251217",
        "utm_source": "2074789182",
        "default": "1663714181",
        "page_map": "1823123412"
    }
    
    # 進行不區分大小寫的匹配
    sh_lower = str(sheet_name or "").strip().lower()
    if sh_lower in gid_map:
        gid = gid_map[sh_lower]
        url = f"https://docs.google.com/spreadsheets/d/{MAPPING_SHEET_ID}/export?format=csv&gid={gid}"
        logger.info(f"[Mapping] 線上同步 {sheet_name} 使用直導 CSV (GID: {gid})")
    else:
        url = f"https://docs.google.com/spreadsheets/d/{MAPPING_SHEET_ID}/gviz/tq?tqx=out:csv&sheet={sheet_name}"
        logger.info(f"[Mapping] 線上同步 {sheet_name} 使用 gviz API")
        
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    # 強制使用 utf-8 解碼，避開 requests 對無指定 charset 的 CSV 自動推導為 latin-1 的 Bug
    resp.encoding = "utf-8"
    return resp.text

def find_col_idx(header: List[str], candidates: List[str]) -> int:
    # 進行大小寫、空格、底線不敏感的子字串比對
    # 1. 先嘗試精確匹配（包含 strip 與小寫）
    header_clean = [str(h).strip().lower() for h in header]
    for cand in candidates:
        cand_clean = cand.strip().lower()
        if cand_clean in header_clean:
            return header_clean.index(cand_clean)
            
    # 2. 如果沒有精確匹配，進行子字串匹配（忽略空格與底線）
    for cand in candidates:
        cand_norm = cand.strip().lower().replace(" ", "").replace("_", "")
        if not cand_norm:
            continue
        for idx, h in enumerate(header_clean):
            h_norm = h.replace(" ", "").replace("_", "")
            # 若候選關鍵字存在於欄位名稱的開頭或包含在內
            if cand_norm in h_norm:
                return idx
    return -1

def canon(s: str) -> str:
    return str(s or "").strip().lower().replace(" ", "").replace("\t", "")

def gv(row: List[Any], idx: int) -> str:
    return str(row[idx]).strip() if idx >= 0 and idx < len(row) else ""

def sync_page_map() -> Dict[str, Any]:
    global _cached_page_map
    local_path = os.path.join(MAPPING_DIR, "page_map.csv")
    csv_text = ""
    if os.path.exists(local_path):
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                csv_text = f.read()
            logger.info("[Mapping] 優先讀取本地 page_map.csv 檔案")
        except Exception as e:
            logger.warning(f"[Mapping] 讀取本地 page_map 失敗: {repr(e)}")

    if not csv_text:
        try:
            csv_text = fetch_sheet_csv_online("page_map")
            with open(local_path, "w", encoding="utf-8") as f:
                f.write(csv_text)
            logger.info("[Mapping] 線上同步 page_map 成功")
        except Exception as e:
            logger.error(f"[Mapping] 線上同步 page_map 失敗 ({repr(e)})，且本地亦無備份")
            return {"rows": [], "byKey": {}}

    try:
        lines = [line for line in csv.reader(csv_text.splitlines())]
    except Exception as e:
        logger.error(f"[Mapping] 解析 page_map CSV 失敗: {repr(e)}")
        return {"rows": [], "byKey": {}}
        
    if not lines or len(lines) < 2:
        return {"rows": [], "byKey": {}}
        
    header = lines[0]
    cId = find_col_idx(header, ['id', 'page_id', 'news_id', 'article_id', 'comment_id', 'lecture_id', 'f_subject_no', 'subject_no'])
    cName = find_col_idx(header, ['name', 'title'])
    cProd = find_col_idx(header, ['product', 'prod'])
    cPdet = find_col_idx(header, ['product_detail', 'productdetail', 'prod_detail', 'd'])
    cCat = find_col_idx(header, ['category', '類別', '分類', 'e', 'cat'])

    def norm_category(cat: str) -> str:
        c = str(cat or "").strip().lower().replace(" ", "").replace("-", "_")
        if c in ["newsid", "news_id"]: return "news_id"
        if c in ["articleid", "article_id"]: return "article_id"
        if c in ["commentid", "comment_id"]: return "comment_id"
        if c in ["lectureid", "lecture_id"]: return "lecture_id"
        if c == "edm": return "edm"
        if c in ["fsubjectno", "f_subjectno", "f_subject_no"]: return "f_subject_no"
        if c in ["subjectno", "subject_no"]: return "subject_no"
        if c in ["exam_type_id", "examid"]: return "exam_type_id"
        return ""

    allow_cats = {"news_id", "article_id", "comment_id", "lecture_id", "edm", "f_subject_no", "subject_no", "exam_type_id"}
    rows = []
    byKey = {k: [] for k in allow_cats}

    for r in range(1, len(lines)):
        row_vals = lines[r]
        val_id = gv(row_vals, cId)
        val_name = gv(row_vals, cName)
        val_prod = gv(row_vals, cProd)
        val_pdet = gv(row_vals, cPdet)
        val_cat = norm_category(gv(row_vals, cCat)) if cCat >= 0 else ""
        
        if not val_id or not val_cat or val_cat not in allow_cats:
            continue
            
        row_obj = {
            "id": val_id,
            "name": val_name,
            "product": val_prod,
            "product_detail": val_pdet,
            "category": val_cat
        }
        rows.append(row_obj)
        byKey[val_cat].append(row_obj)

    _cached_page_map = {"rows": rows, "byKey": byKey}
    return _cached_page_map

def sync_source_mappings() -> Dict[str, Any]:
    global _cached_maps
    local_path = os.path.join(MAPPING_DIR, "source_mappings.json")
    res = {"adno": {}, "print_id": {}, "utm_source": {}, "defaults": []}
    
    # 嘗試從線上同步所有可能的頁籤
    sheets = ["source_map", "source", "mapping", "adNo", "print_id", "utm_source", "default"]
    downloaded_data = {}
    
    online_success = False
    for sh_name in sheets:
        try:
            csv_text = fetch_sheet_csv_online(sh_name)
            downloaded_data[sh_name] = csv_text
            online_success = True
        except Exception:
            pass # 找不到該頁籤是正常的
            
    if online_success:
        try:
            # 寫入本地備份
            with open(local_path, "w", encoding="utf-8") as f:
                json.dump(downloaded_data, f, ensure_ascii=False, indent=2)
            logger.info("[Mapping] 線上同步 source_mappings 成功")
        except Exception as e:
            logger.warning(f"儲存本地 source_mappings 備份失敗: {repr(e)}")
    else:
        logger.warning("[Mapping] 線上同步 source_mappings 失敗，讀取本地備份")
        if os.path.exists(local_path):
            try:
                with open(local_path, "r", encoding="utf-8") as f:
                    downloaded_data = json.load(f)
            except Exception as e:
                logger.error(f"讀取本地 source_mappings 備份失敗: {repr(e)}")
                
    import csv
    # 1. 處理 source_map / source / mapping (單一表模式)
    for sh_name in ["source_map", "source", "mapping"]:
        if sh_name in downloaded_data:
            csv_text = downloaded_data[sh_name]
            lines = list(csv.reader(csv_text.splitlines()))
            if not lines or len(lines) < 2: continue
            head = lines[0]
            idx_type = find_col_idx(head, ['type', '類別'])
            idx_name = find_col_idx(head, ['name', 'label', '顯示名稱', 'source_name', 'custom_source_name'])
            idx_grp = find_col_idx(head, ['group', '群組', 'group_name', 'source_group'])
            idx_sub = find_col_idx(head, ['sub', '子群組', 'source_sub', '細項'])
            idx_key = find_col_idx(head, ['key', 'key_value', '值', 'id'])
            
            for r in range(1, len(lines)):
                v = lines[r]
                t = canon(gv(v, idx_type))
                nm = gv(v, idx_name)
                grp = gv(v, idx_grp)
                sub = gv(v, idx_sub)
                key = gv(v, idx_key)
                if not t: continue
                if t == "default":
                    if nm: res["defaults"].append({"label": nm, "name": nm, "group": grp, "sub": ""})
                    continue
                if not key: continue
                val_obj = {"label": nm or key, "name": nm or key, "group": grp, "sub": sub}
                key_norm = key.strip().lower()
                if t == "adno": res["adno"][key_norm] = val_obj
                elif t == "print_id": res["print_id"][key_norm] = val_obj
                elif t == "utm_source": res["utm_source"][key_norm] = val_obj

    # 2. 處理獨立表
    if "adNo" in downloaded_data:
        lines = list(csv.reader(downloaded_data["adNo"].splitlines()))
        if len(lines) >= 2:
            head = lines[0]
            idx_key = find_col_idx(head, ['key_value', 'key', '值'])
            idx_name = find_col_idx(head, ['custom_source_name', 'source_name', 'label', '顯示名稱', 'name'])
            idx_grp = find_col_idx(head, ['group', 'source_group', '群組', 'group_name'])
            for r in range(1, len(lines)):
                v = lines[r]
                key = gv(v, idx_key)
                if not key: continue
                key_norm = key.strip().lower()
                res["adno"][key_norm] = {"label": gv(v, idx_name) or key, "name": gv(v, idx_name) or key, "group": gv(v, idx_grp), "sub": ""}

    if "print_id" in downloaded_data:
        lines = list(csv.reader(downloaded_data["print_id"].splitlines()))
        if len(lines) >= 2:
            head = lines[0]
            idx_key = find_col_idx(head, ['key_value', 'key', '值'])
            idx_name = find_col_idx(head, ['source_name', 'custom_source_name', 'label', '顯示名稱', 'name'])
            idx_sub = find_col_idx(head, ['source_sub', 'sub', '細項'])
            idx_grp = find_col_idx(head, ['group', 'source_group', '群組', 'group_name'])
            for r in range(1, len(lines)):
                v = lines[r]
                key = gv(v, idx_key)
                if not key: continue
                key_norm = key.strip().lower()
                res["print_id"][key_norm] = {"label": gv(v, idx_name) or key, "name": gv(v, idx_name) or key, "group": gv(v, idx_grp), "sub": gv(v, idx_sub)}

    if "utm_source" in downloaded_data:
        lines = list(csv.reader(downloaded_data["utm_source"].splitlines()))
        if len(lines) >= 2:
            head = lines[0]
            idx_key = find_col_idx(head, ['key_value', 'key', '值'])
            idx_name = find_col_idx(head, ['custom_source_name', 'label', 'source_name', '顯示名稱', 'name'])
            idx_grp = find_col_idx(head, ['group', 'source_group', '群組', 'group_name'])
            for r in range(1, len(lines)):
                v = lines[r]
                key = gv(v, idx_key)
                if not key: continue
                key_norm = key.strip().lower()
                res["utm_source"][key_norm] = {"label": gv(v, idx_name) or key, "name": gv(v, idx_name) or key, "group": gv(v, idx_grp), "sub": ""}

    if "default" in downloaded_data:
        lines = list(csv.reader(downloaded_data["default"].splitlines()))
        if len(lines) >= 2:
            head = lines[0]
            idx_type = find_col_idx(head, ['key_type', 'type', '類別'])
            idx_name = find_col_idx(head, ['custom_source_name', 'source_name', 'label', '顯示名稱', 'name'])
            idx_grp = find_col_idx(head, ['group', 'source_group', '群組', 'group_name'])
            for r in range(1, len(lines)):
                v = lines[r]
                if canon(gv(v, idx_type)) != "default": continue
                nm = gv(v, idx_name)
                if nm:
                    res["defaults"].append({"label": nm, "name": nm, "group": gv(v, idx_grp), "sub": ""})

    _cached_maps = res
    return _cached_maps

def load_page_map() -> Dict[str, Any]:
    global _cached_page_map
    if _cached_page_map is None:
        sync_page_map()
    return _cached_page_map

def load_mappings() -> Dict[str, Any]:
    global _cached_maps
    if _cached_maps is None:
        sync_source_mappings()
    return _cached_maps

# =====================================================================
# 5. Mappings 解析規則 (對齊 code.js)
# =====================================================================
def resolve_source_name(src_raw: str, lp_raw: str, maps: Dict[str, Any]) -> Dict[str, str]:
    src_raw = str(src_raw or "")
    lp_raw = str(lp_raw or "")
    params = {}
    if lp_raw and "?" in lp_raw:
        try:
            qs = lp_raw.split("?", 1)[1]
            params = {k.lower(): v[0] for k, v in urllib.parse.parse_qs(qs).items() if v}
        except Exception:
            pass
            
    def_val = {"label": src_raw, "name": src_raw, "group": "", "sub": ""}
    if not maps:
        return def_val
        
    adno = params.get("adno") or params.get("adNo") or ""
    adno_norm = adno.strip().lower()
    if adno_norm and maps.get("adno") and adno_norm in maps["adno"]:
        a = maps["adno"][adno_norm]
        return {
            "label": a.get("label") or a.get("name") or src_raw,
            "name": a.get("name") or src_raw,
            "group": a.get("group") or "",
            "sub": a.get("sub") or ""
        }
        
    pid = params.get("print_id") or params.get("printid") or params.get("printId") or ""
    pid_norm = pid.strip().lower()
    if pid_norm and maps.get("print_id") and pid_norm in maps["print_id"]:
        p = maps["print_id"][pid_norm]
        return {
            "label": p.get("label") or p.get("name") or src_raw,
            "name": p.get("name") or src_raw,
            "group": p.get("group") or "",
            "sub": p.get("sub") or ""
        }
        
    us = params.get("utm_source") or ""
    us_norm = us.strip().lower()
    if us_norm and maps.get("utm_source") and us_norm in maps["utm_source"]:
        u = maps["utm_source"][us_norm]
        return {
            "label": u.get("label") or u.get("name") or src_raw,
            "name": u.get("name") or src_raw,
            "group": u.get("group") or "",
            "sub": ""
        }
        
    if maps.get("defaults") and len(maps["defaults"]) > 0:
        d = maps["defaults"][0]
        return {
            "label": d.get("label") or d.get("name") or src_raw,
            "name": d.get("name") or src_raw,
            "group": d.get("group") or "",
            "sub": ""
        }
        
    return def_val

def clean_url_path(url: str) -> str:
    """Extract path without query string, lowercase."""
    if not url:
        return ""
    try:
        if url.startswith("http://") or url.startswith("https://"):
            parsed = urllib.parse.urlparse(url)
            path = parsed.path
        else:
            path = url.split("?", 1)[0]
        p = path.strip().rstrip('/')
        if not p.startswith('/'):
            p = '/' + p
        return p.lower()
    except Exception:
        return url.strip().lower()

def clean_gsc_page_url(url: str) -> str:
    """Strip marketing tracking parameters (like utm_*, gclid, fbclid) from URLs for cleaner GSC matching."""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        qd = urllib.parse.parse_qs(parsed.query)
        clean_qd = {}
        for k, v in qd.items():
            k_lower = k.lower()
            if k_lower.startswith("utm_") or k_lower in ("gclid", "fbclid", "cohort"):
                continue
            clean_qd[k] = v
        new_query = urllib.parse.urlencode(clean_qd, doseq=True)
        new_parts = parsed._replace(query=new_query)
        return urllib.parse.urlunparse(new_parts)
    except Exception:
        return url

def url_path_with_query(url: str) -> str:
    """Extract path WITH query string, lowercase. Used for GSC matching."""
    if not url:
        return ""
    try:
        if url.startswith("http://") or url.startswith("https://"):
            parsed = urllib.parse.urlparse(url)
            path = parsed.path
            if parsed.query:
                path = path + "?" + parsed.query
        else:
            path = url
        p = path.strip().rstrip('/')
        if not p.startswith('/'):
            p = '/' + p
        return p.lower()
    except Exception:
        return url.strip().lower()

def lookup_gsc(gsc_dict: Dict[str, Dict[str, Any]], lp_raw: str) -> Dict[str, Any]:
    """Try matching GSC data: first try full path+query, then fallback to path-only."""
    empty = {"gscClicks": 0, "gscImpressions": 0, "gscPosition": 0.0}
    full_key = url_path_with_query(lp_raw)
    if full_key and full_key in gsc_dict:
        return gsc_dict[full_key]
    path_key = clean_url_path(lp_raw)
    if path_key and path_key in gsc_dict:
        return gsc_dict[path_key]
    return empty

def get_gsc_data_dict(client: BetaAnalyticsDataClient, start_date: str, end_date: str) -> Dict[str, Dict[str, Any]]:
    """透過 Search Console API 取得每頁 GSC 指標（以 page 維度查詢）"""
    site_url = get_gsc_site_url()
    if not site_url:
        logger.warning("get_gsc_data_dict: 無法取得 GSC 網站 URL，返回空資料")
        return {}
    
    rows = gsc_query_analytics(site_url, start_date, end_date, dimensions=['page'], row_limit=10000)
    logger.info(f"GSC data_dict: 從 Search Console API 取得 {len(rows)} 筆 page 資料")
    
    gsc_map = {}
    for row in rows:
        page_url = row['keys'][0]  # e.g. https://demo.example.com/news/toDetail?news_id=1001
        clicks = int(row.get('clicks', 0))
        impr = int(row.get('impressions', 0))
        pos = float(row.get('position', 0.0))
        ctr = float(row.get('ctr', 0.0))
        
        # 只將指標累加到單一唯一鍵，避免重複計算
        full_key = url_path_with_query(page_url)
        key = full_key or page_url
        if key:
            if key not in gsc_map:
                gsc_map[key] = {"gscClicks": 0, "gscImpressions": 0, "gscPosW": 0.0, "gscClicksForPos": 0}
            gsc_map[key]["gscClicks"] += clicks
            gsc_map[key]["gscImpressions"] += impr
            if pos > 0 and clicks > 0:
                gsc_map[key]["gscPosW"] += pos * clicks
                gsc_map[key]["gscClicksForPos"] += clicks
    
    final_map = {}
    for k, v in gsc_map.items():
        p = 0.0
        if v["gscClicksForPos"] > 0:
            p = v["gscPosW"] / v["gscClicksForPos"]
        final_map[k] = {
            "gscClicks": v["gscClicks"],
            "gscImpressions": v["gscImpressions"],
            "gscPosition": p
        }
    logger.info(f"GSC data_dict: 最終 map 共 {len(final_map)} 個 key")
    return final_map

def get_gsc_kpi(client: BetaAnalyticsDataClient, start_date: str, end_date: str) -> Dict[str, Any]:
    """透過 Search Console API 取得全站 GSC KPI（不分維度查詢即為全站總量）"""
    site_url = get_gsc_site_url()
    if not site_url:
        logger.warning("get_gsc_kpi: 無法取得 GSC 網站 URL，返回空資料")
        return {"gscClicks": 0, "gscImpressions": 0, "gscCtr": 0.0, "gscPosition": 0.0}
    
    # 無 dimensions 時回傳全站聚合
    rows = gsc_query_analytics(site_url, start_date, end_date, dimensions=None, row_limit=1)
    if rows:
        row = rows[0]
        clicks = int(row.get('clicks', 0))
        impr = int(row.get('impressions', 0))
        ctr_val = float(row.get('ctr', 0.0))
        pos = float(row.get('position', 0.0))
        logger.info(f"GSC KPI (from SC API): clicks={clicks}, impressions={impr}, ctr={ctr_val:.4f}, pos={pos:.1f}")
        return {
            "gscClicks": clicks,
            "gscImpressions": impr,
            "gscCtr": ctr_val,
            "gscPosition": pos
        }
    logger.warning("get_gsc_kpi: Search Console API 無資料")
    return {"gscClicks": 0, "gscImpressions": 0, "gscCtr": 0.0, "gscPosition": 0.0}


def resolve_page_by_params(input_url: str, page_map: Dict[str, Any]) -> Dict[str, str]:
    cats = ['news_id', 'article_id', 'comment_id', 'lecture_id', 'edm', 'f_subject_no', 'subject_no']
    by_key = page_map.get("byKey") or {}
    
    lp = str(input_url or "")
    decoded = safe_decode(lp)
    
    params = {}
    if "?" in decoded:
        try:
            qs = decoded.split("?", 1)[1]
            if "#" in qs:
                qs = qs.split("#", 1)[0]
            params = {k.lower(): v[0] for k, v in urllib.parse.parse_qs(qs).items() if v}
        except Exception:
            pass
            
    # 1) key=value
    params_clean = {k.replace("_", ""): v for k, v in params.items()}
    for k in cats:
        k_clean = k.lower().replace("_", "")
        val = params_clean.get(k_clean)
        if val is not None and str(val).strip() != "":
            id_val = str(val).strip()
            # 清除所有非英數字與底線/橫線的後續雜訊 (例如 ? / & 等等)
            id_val = re.sub(r"[^a-zA-Z0-9_-].*", "", id_val)
            row = None
            if k in by_key:
                for r in by_key[k]:
                    if str(r.get("id")).lower() == id_val.lower():
                        row = r
                        break
            return {
                "category": k,
                "id": id_val,
                "name": row.get("name") if row else (f"EDM {id_val}" if k == "edm" else id_val),
                "product": (row.get("product") or "未分類") if row else "未分類",
                "product_detail": row.get("product_detail") if row else ""
            }

    # 2) EDM Regex
    if decoded:
        s_low = decoded.lower()
        
        def pick_row(cat: str, raw_id: str):
            lst = by_key.get(cat) or []
            candidates = [raw_id]
            if cat == "edm":
                if re.match(r"^edm[_-]?\w+", raw_id, re.IGNORECASE):
                    candidates.append(re.sub(r"^edm[_-]?", "", raw_id, flags=re.IGNORECASE))
                else:
                    candidates.extend([f"edm{raw_id}", f"edm_{raw_id}", f"edm-{raw_id}"])
            for cand in candidates:
                for r in lst:
                    if str(r.get("id")).lower() == str(cand).lower():
                        return {"id": cand, "row": r}
            return None

        m_edm = re.search(r"(?:^|[?&#\/])edm[_-]?([a-z0-9]+)", decoded, re.IGNORECASE)
        if m_edm and m_edm.group(1):
            raw = m_edm.group(1)
            try1 = pick_row('edm', raw) or pick_row('edm', 'edm' + raw)
            id_final = try1["id"] if try1 else raw
            row_e = try1["row"] if try1 else None
            return {
                "category": "edm",
                "id": id_final,
                "name": row_e.get("name") if row_e else f"EDM {id_final}",
                "product": (row_e.get("product") or "未分類") if row_e else "未分類",
                "product_detail": row_e.get("product_detail") if row_e else ""
            }

        # 其他匹配
        re_list = [
            {"cat": "news_id", "re": r"(?:^|[?&#\/])news[_-]?(\d{1,12})"},
            {"cat": "article_id", "re": r"(?:^|[?&#\/])article[_-]?(\d{1,12})"},
            {"cat": "comment_id", "re": r"(?:^|[?&#\/])comment[_-]?(\d{1,12})"},
            {"cat": "lecture_id", "re": r"(?:^|[?&#\/])lecture[_-]?(\d{1,12})"}
        ]
        for item in re_list:
            mm = re.search(item["re"], decoded, re.IGNORECASE)
            if mm and mm.group(1):
                cat = item["cat"]
                val_id = str(mm.group(1))
                row2 = None
                if cat in by_key:
                    for x in by_key[cat]:
                        if str(x.get("id")) == val_id:
                            row2 = x
                            break
                return {
                    "category": cat,
                    "id": val_id,
                    "name": row2.get("name") if row2 else val_id,
                    "product": (row2.get("product") or "未分類") if row2 else "未分類",
                    "product_detail": row2.get("product_detail") if row2 else ""
                }

    return {"category": "", "id": "", "name": "", "product": "", "product_detail": ""}

# =====================================================================
# 6. 本機 JSON 快照讀寫小工具 (替代 Google Drive)
# =====================================================================
def get_snapshot_path(name: str) -> str:
    return os.path.join(SNAPSHOT_DIR, f"{name}.json")

def write_snapshot_json(name: str, obj: Any):
    path = get_snapshot_path(name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    logger.info(f"[Snapshot] 寫入快照: {name}.json")

def read_snapshot_json(name: str) -> Optional[Any]:
    path = get_snapshot_path(name)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[Snapshot] 讀取快照 {name} 失敗: {repr(e)}")
            return None
    return None

def candidate_slots() -> List[str]:
    tz_now = get_now_taipei()
    today_str = tz_now.strftime("%Y%m%d")
    
    hh = tz_now.hour
    c = []
    if hh >= 18: c.append(f"{today_str}-18")
    if hh >= 12: c.append(f"{today_str}-12")
    if hh >= 6:  c.append(f"{today_str}-06")
    c.append(f"{today_str}-00")
    
    # 前一天的 slot 候補
    yesterday = tz_now - timedelta(days=1)
    y_str = yesterday.strftime("%Y%m%d")
    c.extend([f"{y_str}-18", f"{y_str}-12", f"{y_str}-06", f"{y_str}-00"])
    return c

# =====================================================================
# 7. GA4 API 獲取與計算 (原先在 code.js)
# =====================================================================
def get_heat_data(client: BetaAnalyticsDataClient, start_date: str, end_date: str, limit: int = 30000, gsc_dict: Dict[str, Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    # 抓取 GA4
    req = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimensions=[Dimension(name="sessionSourceMedium"), Dimension(name="pageLocation")],
        metrics=[
            Metric(name="totalUsers"),
            Metric(name="sessions"),
            Metric(name="engagementRate"),
            Metric(name="averageSessionDuration")
        ],
        limit=limit
    )
    
    try:
        response = client.run_report(req)
    except Exception as e:
        logger.error(f"GA4 runReport 失敗: {repr(e)}")
        return []

    maps = load_mappings()
    page_map = load_page_map()
    
    # 獲取真實的 GSC 資料
    if gsc_dict is None:
        gsc_dict = get_gsc_data_dict(client, start_date, end_date)

    out = []
    for row in response.rows:
        src_raw = row.dimension_values[0].value
        lp_raw = row.dimension_values[1].value
        
        users = int(row.metric_values[0].value or 0)
        sessions = int(row.metric_values[1].value or 0)
        er = float(row.metric_values[2].value or 0.0)
        dur = float(row.metric_values[3].value or 0.0)
        
        src_obj = resolve_source_name(src_raw, lp_raw, maps)
        pinfo = resolve_page_by_params(lp_raw, page_map)
        
        # 過濾掉完全沒有參數（首頁/列表頁等非產品頁）的資料
        if not pinfo.get("category"):
            continue
        
        # 配對 GSC 指標 (先嘗試含 query string 的完整路徑，再嘗試不含 query string 的)
        gsc_item = lookup_gsc(gsc_dict, lp_raw)
        
        out.append({
            "source_raw": src_raw,
            "source": src_obj["label"],
            "source_name": src_obj["name"],
            "source_group": src_obj["group"],
            "source_sub": src_obj["sub"],
            "lp": lp_raw,
            "display_title": pinfo["name"],
            "product": pinfo["product"],
            "product_detail": pinfo["product_detail"],
            "category": pinfo["category"],
            "pageId": pinfo["id"],
            "users": users,
            "sessions": sessions,
            "engagementRate": er,
            "avgDuration": dur,
            "avgSessionsPerUser": (sessions / users) if users > 0 else 0.0,
            "score": quality_score(er, dur),
            "gscClicks": gsc_item["gscClicks"],
            "gscImpressions": gsc_item["gscImpressions"],
            "gscPosition": gsc_item["gscPosition"]
        })
    return out

def get_kpi_base(client: BetaAnalyticsDataClient, start_date: str, end_date: str) -> Dict[str, Any]:
    req = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        metrics=[
            Metric(name="totalUsers"),
            Metric(name="screenPageViews"),
            Metric(name="sessions"),
            Metric(name="engagementRate"),
            Metric(name="averageSessionDuration")
        ]
    )
    kpi_res = {"users": 0, "pageViews": 0, "sessions": 0, "engagementRate": 0.0, "avgSessionSec": 0.0}
    try:
        response = client.run_report(req)
        if response.rows:
            row = response.rows[0]
            kpi_res = {
                "users": int(row.metric_values[0].value or 0),
                "pageViews": int(row.metric_values[1].value or 0),
                "sessions": int(row.metric_values[2].value or 0),
                "engagementRate": float(row.metric_values[3].value or 0.0),
                "avgSessionSec": float(row.metric_values[4].value or 0.0)
            }
    except Exception as e:
        logger.error(f"get_kpi_base 失敗: {repr(e)}")
        
    gsc_kpi = get_gsc_kpi(client, start_date, end_date)
    kpi_res.update(gsc_kpi)
    return kpi_res

def aggregate_heat_for_summary(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not rows: return []
    df = pd.DataFrame(rows)
    # 按 group, name 進行 groupby
    grouped = df.groupby(["source_group", "source", "source_name", "source_sub"], dropna=False)
    
    agg_rows = []
    for keys, gp in grouped:
        sessions_sum = gp["sessions"].sum()
        users_sum = gp["users"].sum()
        
        # 加權 ER 與 Duration
        weighted_er = (gp["engagementRate"] * gp["sessions"]).sum() / sessions_sum if sessions_sum > 0 else 0.0
        weighted_dur = (gp["avgDuration"] * gp["sessions"]).sum() / sessions_sum if sessions_sum > 0 else 0.0
        
        # 累加 GSC 指標（依 lp 去重避免重複累加）
        gsc_clicks = 0
        gsc_impr = 0
        gsc_pos_sum = 0.0
        gsc_pos_clicks = 0
        
        gsc_counted_lps = set()
        for _, row_item in gp.iterrows():
            lp_raw = str(row_item.get("lp") or "").strip().lower()
            if not lp_raw:
                continue
            if lp_raw not in gsc_counted_lps:
                gsc_counted_lps.add(lp_raw)
                clicks = int(row_item.get("gscClicks") or 0)
                impr = int(row_item.get("gscImpressions") or 0)
                pos = float(row_item.get("gscPosition") or 0.0)
                
                gsc_clicks += clicks
                gsc_impr += impr
                if pos > 0:
                    gsc_pos_sum += pos * clicks
                    gsc_pos_clicks += clicks
                    
        gsc_pos_w = gsc_pos_sum / gsc_pos_clicks if gsc_pos_clicks > 0 else 0.0

        agg_rows.append({
            "source_group": keys[0],
            "source": keys[1],
            "source_name": keys[2],
            "source_sub": keys[3],
            "users": int(users_sum),
            "sessions": int(sessions_sum),
            "engagementRate": float(weighted_er),
            "avgDuration": float(weighted_dur),
            "avgSessionSec": float(weighted_dur),
            "avgSessionsPerUser": float(sessions_sum / users_sum) if users_sum > 0 else 0.0,
            "score": quality_score(weighted_er, weighted_dur),
            "product": "",
            "product_detail": "",
            "lp": "",
            "display_title": "",
            "category": "",
            "pageId": "",
            "gscClicks": int(gsc_clicks),
            "gscImpressions": int(gsc_impr),
            "gscPosition": float(gsc_pos_w)
        })
        
    return sorted(agg_rows, key=lambda x: x["users"], reverse=True)

def aggregate_heat_for_summary_by_product(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not rows: return []
    df = pd.DataFrame(rows)
    grouped = df.groupby(["product", "product_detail", "source_group", "source", "source_name", "source_sub"], dropna=False)
    
    agg_rows = []
    for keys, gp in grouped:
        sessions_sum = gp["sessions"].sum()
        users_sum = gp["users"].sum()
        
        weighted_er = (gp["engagementRate"] * gp["sessions"]).sum() / sessions_sum if sessions_sum > 0 else 0.0
        weighted_dur = (gp["avgDuration"] * gp["sessions"]).sum() / sessions_sum if sessions_sum > 0 else 0.0
        
        # 累加 GSC 指標（依 lp 去重避免重複累加）
        gsc_clicks = 0
        gsc_impr = 0
        gsc_pos_w = 0.0
        
        if "lp" in gp.columns and "gscClicks" in gp.columns:
            gsc_unique = gp.drop_duplicates(subset=["lp"])
            gsc_clicks = gsc_unique["gscClicks"].sum()
            gsc_impr = gsc_unique["gscImpressions"].sum()
            
            valid_pos = gsc_unique[gsc_unique["gscPosition"] > 0]
            clicks_for_pos = valid_pos["gscClicks"].sum()
            if clicks_for_pos > 0:
                gsc_pos_w = (valid_pos["gscPosition"] * valid_pos["gscClicks"]).sum() / clicks_for_pos
            elif len(valid_pos) > 0:
                gsc_pos_w = valid_pos["gscPosition"].mean()

        agg_rows.append({
            "product": keys[0],
            "product_detail": keys[1],
            "source_group": keys[2],
            "source": keys[3],
            "source_name": keys[4],
            "source_sub": keys[5],
            "users": int(users_sum),
            "sessions": int(sessions_sum),
            "engagementRate": float(weighted_er),
            "avgDuration": float(weighted_dur),
            "avgSessionSec": float(weighted_dur),
            "avgSessionsPerUser": float(sessions_sum / users_sum) if users_sum > 0 else 0.0,
            "score": quality_score(weighted_er, weighted_dur),
            "lp": "",
            "display_title": "",
            "category": "",
            "pageId": "",
            "gscClicks": int(gsc_clicks),
            "gscImpressions": int(gsc_impr),
            "gscPosition": float(gsc_pos_w)
        })
    return sorted(agg_rows, key=lambda x: x["users"], reverse=True)

def summarize_filters_for_ui(heat_rows: List[Dict[str, Any]], page_map: Dict[str, Any]) -> Dict[str, Any]:
    products = set()
    pdet_map = {}
    
    # 從 page_map 提取產品與小分類 (保持與 GAS summarizeFiltersForUI 一致)
    pm_rows = page_map.get("rows") or []
    for r in pm_rows:
        prod = str(r.get("product") or "").strip()
        pdet = str(r.get("product_detail") or "").strip()
        
        prod_key = prod if prod else "未分類"
        products.add(prod_key)
            
        if prod_key not in pdet_map:
            pdet_map[prod_key] = set()
        if pdet:
            pdet_map[prod_key].add(pdet)
                
    # 來源群組
    groups = set()
    for r in heat_rows:
        g = str(r.get("source_group") or "").strip()
        groups.add(g if g else "未分類")
        
    return {
        "products": sorted(list(products)),
        "sourceGroups": sorted(list(groups)),
        "productDetailsByProduct": {k: sorted(list(v)) for k, v in pdet_map.items()}
    }

def build_product_drilldown_data(page_map: Dict[str, Any], selected_product: str) -> Dict[str, List[Dict[str, str]]]:
    pm_rows = page_map.get("rows") or []
    out = {cat: [] for cat in ['news_id', 'article_id', 'comment_id', 'lecture_id', 'edm', 'f_subject_no', 'subject_no']}
    
    for r in pm_rows:
        cat = r.get("category")
        prod = r.get("product")
        if cat in out:
            if selected_product and str(prod) != selected_product:
                continue
            out[cat].append({
                "id": r["id"],
                "name": r["name"],
                "product": prod,
                "category": cat
            })
    return out

def get_page_type_metrics(client: BetaAnalyticsDataClient, start_date: str, end_date: str, limit: int = 100000) -> Dict[str, List[Dict[str, Any]]]:
    req = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimensions=[Dimension(name="sessionSourceMedium"), Dimension(name="pageLocation")],
        metrics=[
            Metric(name="screenPageViews"),
            Metric(name="engagementRate"),
            Metric(name="averageSessionDuration")
        ],
        limit=limit
    )
    
    try:
        response = client.run_report(req)
    except Exception as e:
        logger.error(f"get_page_type_metrics 失敗: {repr(e)}")
        return {cat: [] for cat in ['news_id', 'article_id', 'comment_id', 'lecture_id', 'edm', 'f_subject_no', 'subject_no']}

    page_map = load_page_map()
    maps = load_mappings()
    
    # 獲取真實的 GSC 資料
    gsc_dict = get_gsc_data_dict(client, start_date, end_date)
    
    buckets = {cat: {} for cat in ['news_id', 'article_id', 'comment_id', 'lecture_id', 'edm', 'f_subject_no', 'subject_no']}
    
    for row in response.rows:
        src_raw = row.dimension_values[0].value
        lp_raw = row.dimension_values[1].value
        
        views = int(row.metric_values[0].value or 0)
        er = float(row.metric_values[1].value or 0.0)
        dur = float(row.metric_values[2].value or 0.0)
        
        pinfo = resolve_page_by_params(lp_raw, page_map)
        cat = pinfo["category"]
        val_id = pinfo["id"]
        
        if not cat or not val_id or cat not in buckets:
            continue
            
        name = pinfo["name"] or (f"EDM {val_id}" if cat == "edm" else val_id)
        
        bkt = buckets[cat]
        if val_id not in bkt:
            bkt[val_id] = {
                "id": val_id,
                "name": name,
                "views": 0,
                "er_weighted_sum": 0.0,
                "dur_weighted_sum": 0.0,
                "product": pinfo["product"],
                "product_detail": pinfo["product_detail"],
                "gscClicks": 0,
                "gscImpressions": 0,
                "gscPosW": 0.0,
                "gscClicksForPos": 0
            }
            
        item = bkt[val_id]
        item["views"] += views
        item["er_weighted_sum"] += views * er
        item["dur_weighted_sum"] += views * dur

    # 處理 GSC 資料
    for url, g_item in gsc_dict.items():
        pinfo = resolve_page_by_params(url, page_map)
        cat = pinfo["category"]
        val_id = pinfo["id"]
        
        if not cat or not val_id or cat not in buckets:
            continue
            
        name = pinfo["name"] or (f"EDM {val_id}" if cat == "edm" else val_id)
        bkt = buckets[cat]
        
        if val_id not in bkt:
            bkt[val_id] = {
                "id": val_id,
                "name": name,
                "views": 0,
                "er_weighted_sum": 0.0,
                "dur_weighted_sum": 0.0,
                "product": pinfo["product"],
                "product_detail": pinfo["product_detail"],
                "gscClicks": 0,
                "gscImpressions": 0,
                "gscPosW": 0.0,
                "gscClicksForPos": 0
            }
            
        item = bkt[val_id]
        clicks = int(g_item.get("gscClicks") or 0)
        impr = int(g_item.get("gscImpressions") or 0)
        pos = float(g_item.get("gscPosition") or 0.0)
        
        item["gscClicks"] += clicks
        item["gscImpressions"] += impr
        if pos > 0 and clicks > 0:
            item["gscPosW"] += pos * clicks
            item["gscClicksForPos"] += clicks

    out = {}
    for cat, items in buckets.items():
        packed = []
        for val_id, item in items.items():
            v = item["views"]
            pos = 0.0
            if item["gscClicksForPos"] > 0:
                pos = item["gscPosW"] / item["gscClicksForPos"]
            packed.append({
                "id": item["id"],
                "name": item["name"],
                "views": int(v),
                "engagementRate": float(item["er_weighted_sum"] / v) if v > 0 else 0.0,
                "avgSec": float(item["dur_weighted_sum"] / v) if v > 0 else 0.0,
                "product": item["product"],
                "product_detail": item["product_detail"],
                "gscClicks": item["gscClicks"],
                "gscImpressions": item["gscImpressions"],
                "gscPosition": pos
            })
        out[cat] = sorted(packed, key=lambda x: x["views"], reverse=True)
        
    return out

# =====================================================================
# 8. 快照背景產生核心任務 (同步 L2 snapshot 與 monthly 任務)
# =====================================================================
def build_l2_snapshot_for_k(k: str, slot: str):
    client = get_ga4_client()
    if not client:
        logger.error("無法產生 L2 快照: 找不到憑證檔案 credentials.json")
        return
        
    logger.info(f"開始執行 L2 總覽快照計算, k={k}, slot={slot}")
    range_info = date_range_for_k(k)
    start_date = range_info["startDate"]
    end_date = range_info["endDate"]
    
    cmp = compute_prev_and_yoy_ranges(start_date, end_date)
    
    # 獲取當期、前期、去年同期
    gsc_raw_cur = get_gsc_data_dict(client, start_date, end_date)
    heat_cur = get_heat_data(client, start_date, end_date, gsc_dict=gsc_raw_cur)
    kpi_cur = get_kpi_base(client, start_date, end_date)
    filters = summarize_filters_for_ui(heat_cur, load_page_map())
    
    heat_agg_cur = aggregate_heat_for_summary(heat_cur)
    heat_agg_by_product_cur = aggregate_heat_for_summary_by_product(heat_cur)
    
    # 前期
    gsc_raw_prev = get_gsc_data_dict(client, cmp["prev"]["start"], cmp["prev"]["end"])
    heat_prev = get_heat_data(client, cmp["prev"]["start"], cmp["prev"]["end"], gsc_dict=gsc_raw_prev)
    kpi_prev = get_kpi_base(client, cmp["prev"]["start"], cmp["prev"]["end"])
    heat_agg_prev = aggregate_heat_for_summary(heat_prev)
    heat_agg_by_product_prev = aggregate_heat_for_summary_by_product(heat_prev)
    
    # 去年
    gsc_raw_yoy = get_gsc_data_dict(client, cmp["yoy"]["start"], cmp["yoy"]["end"])
    heat_yoy = get_heat_data(client, cmp["yoy"]["start"], cmp["yoy"]["end"], gsc_dict=gsc_raw_yoy)
    kpi_yoy = get_kpi_base(client, cmp["yoy"]["start"], cmp["yoy"]["end"])
    heat_agg_yoy = aggregate_heat_for_summary(heat_yoy)
    heat_agg_by_product_yoy = aggregate_heat_for_summary_by_product(heat_yoy)
    
    out = {
        "_snapshot": {
            "type": "l2_kshot",
            "k": k,
            "slot": slot,
            "startDate": start_date,
            "endDate": end_date,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "version": "v1"
        },
        "filters": filters,
        "cur": {
            "kpi": kpi_cur,
            "heatAgg": heat_agg_cur,
            "heatAggByProduct": heat_agg_by_product_cur,
            "gscRaw": gsc_raw_cur
        },
        "prev": {
            "kpi": kpi_prev,
            "heatAgg": heat_agg_prev,
            "heatAggByProduct": heat_agg_by_product_prev,
            "gscRaw": gsc_raw_prev
        },
        "yoy": {
            "kpi": kpi_yoy,
            "heatAgg": heat_agg_yoy,
            "heatAggByProduct": heat_agg_by_product_yoy,
            "gscRaw": gsc_raw_yoy
        }
    }
    
    write_snapshot_json(f"l2_kshot__{slot}__k__{k}", out)

def build_l2_drill_for_k(k: str, slot: str, limit: int = 30000):
    client = get_ga4_client()
    if not client:
        return
        
    logger.info(f"開始執行 L2 Drill 明細快照計算, k={k}, slot={slot}")
    range_info = date_range_for_k(k)
    start_date = range_info["startDate"]
    end_date = range_info["endDate"]
    
    cmp = compute_prev_and_yoy_ranges(start_date, end_date)
    
    gsc_raw = get_gsc_data_dict(client, start_date, end_date)
    heat_rows = get_heat_data(client, start_date, end_date, limit=limit, gsc_dict=gsc_raw)
    
    # 前期
    gsc_prev = get_gsc_data_dict(client, cmp["prev"]["start"], cmp["prev"]["end"])
    heat_prev = get_heat_data(client, cmp["prev"]["start"], cmp["prev"]["end"], limit=limit, gsc_dict=gsc_prev)
    
    # 去年同期
    gsc_yoy = get_gsc_data_dict(client, cmp["yoy"]["start"], cmp["yoy"]["end"])
    heat_yoy = get_heat_data(client, cmp["yoy"]["start"], cmp["yoy"]["end"], limit=limit, gsc_dict=gsc_yoy)
    
    out = {
        "_snapshot": {
            "type": "l2_drill",
            "k": k,
            "slot": slot,
            "startDate": start_date,
            "endDate": end_date,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "version": "v1",
            "limit": limit
        },
        "rows": heat_rows,
        "gscRaw": gsc_raw,
        "cur": {
            "heat": heat_rows,
            "gscRaw": gsc_raw
        },
        "prev": {
            "heat": heat_prev,
            "gscRaw": gsc_prev
        },
        "yoy": {
            "heat": heat_yoy,
            "gscRaw": gsc_yoy
        }
    }
    write_snapshot_json(f"l2_drill__{slot}__k__{k}", out)

def build_monthly_snapshot(ym: str):
    client = get_ga4_client()
    if not client:
        return
        
    # 計算上個月的起訖日期
    # ym 格式 "202409"
    y = int(ym[:4])
    m = int(ym[4:6])
    
    first = datetime(y, m, 1)
    if m == 12:
        last = datetime(y + 1, 1, 1) - timedelta(days=1)
    else:
        last = datetime(y, m + 1, 1) - timedelta(days=1)
        
    start_date = first.strftime("%Y-%m-%d")
    end_date = last.strftime("%Y-%m-%d")
    
    logger.info(f"開始執行月度快照計算, ym={ym} ({start_date} ~ {end_date})")
    
    cmp = compute_prev_and_yoy_ranges(start_date, end_date)
    
    gsc_raw_cur = get_gsc_data_dict(client, start_date, end_date)
    heat_cur = get_heat_data(client, start_date, end_date, gsc_dict=gsc_raw_cur)
    kpi_cur = get_kpi_base(client, start_date, end_date)
    page_map = load_page_map()
    filters = summarize_filters_for_ui(heat_cur, page_map)
    
    heat_agg_cur = aggregate_heat_for_summary(heat_cur)
    heat_agg_by_product_cur = aggregate_heat_for_summary_by_product(heat_cur)
    
    # 取得圓餅圖所需的整體來源
    df_cur = pd.DataFrame(heat_cur)
    pie_rows = []
    if not df_cur.empty:
        pie_gp = df_cur.groupby("source_group")["users"].sum().reset_index()
        pie_rows = [{"group": r["source_group"] or "未分類", "users": int(r["users"])} for _, r in pie_gp.iterrows()]
        pie_rows = sorted(pie_rows, key=lambda x: x["users"], reverse=True)
    pie_cur = {"totalUsersApprox": sum(r["users"] for r in pie_rows), "rows": pie_rows}

    # 頁面明細 - 改用同源明細資料建構，避免 GA4 100,000 筆限制與資料差異
    page_metrics_cur = build_page_metrics_from_rows(heat_cur, page_map, gsc_dict=gsc_raw_cur)
    
    # 對照組
    gsc_raw_prev = get_gsc_data_dict(client, cmp["prev"]["start"], cmp["prev"]["end"])
    heat_prev = get_heat_data(client, cmp["prev"]["start"], cmp["prev"]["end"], gsc_dict=gsc_raw_prev)
    kpi_prev = get_kpi_base(client, cmp["prev"]["start"], cmp["prev"]["end"])
    heat_agg_prev = aggregate_heat_for_summary(heat_prev)
    heat_agg_by_product_prev = aggregate_heat_for_summary_by_product(heat_prev)
    page_metrics_prev = build_page_metrics_from_rows(heat_prev, page_map, gsc_dict=gsc_raw_prev)
    
    gsc_raw_yoy = get_gsc_data_dict(client, cmp["yoy"]["start"], cmp["yoy"]["end"])
    heat_yoy = get_heat_data(client, cmp["yoy"]["start"], cmp["yoy"]["end"], gsc_dict=gsc_raw_yoy)
    kpi_yoy = get_kpi_base(client, cmp["yoy"]["start"], cmp["yoy"]["end"])
    heat_agg_yoy = aggregate_heat_for_summary(heat_yoy)
    heat_agg_by_product_yoy = aggregate_heat_for_summary_by_product(heat_yoy)
    page_metrics_yoy = build_page_metrics_from_rows(heat_yoy, page_map, gsc_dict=gsc_raw_yoy)

    out = {
        "_snapshot": {
            "type": "monthly",
            "k": "last_month",
            "ym": ym,
            "startDate": start_date,
            "endDate": end_date,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "version": "v1"
        },
        "filters": filters,
        "cur": {
            "kpi": kpi_cur,
            "heat": heat_cur,
            "heatAgg": heat_agg_cur,
            "heatAggByProduct": heat_agg_by_product_cur,
            "pie": pie_cur,
            "page": page_metrics_cur,
            "gscRaw": gsc_raw_cur
        },
        "prev": {
            "kpi": kpi_prev,
            "heat": heat_prev,
            "heatAgg": heat_agg_prev,
            "heatAggByProduct": heat_agg_by_product_prev,
            "page": page_metrics_prev,
            "gscRaw": gsc_raw_prev
        },
        "yoy": {
            "kpi": kpi_yoy,
            "heat": heat_yoy,
            "heatAgg": heat_agg_yoy,
            "heatAggByProduct": heat_agg_by_product_yoy,
            "page": page_metrics_yoy,
            "gscRaw": gsc_raw_yoy
        }
    }
    
    write_snapshot_json(f"monthly__{ym}__k__last_month", out)
    
    # 同步寫一份 l2_drill 做回退
    slot_id = get_current_slot_id()
    drill_out = {
        "_snapshot": {
            "type": "l2_drill",
            "k": "last_month",
            "slot": slot_id,
            "startDate": start_date,
            "endDate": end_date,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "version": "v1",
            "limit": 100000
        },
        "rows": heat_cur
    }
    write_snapshot_json(f"l2_drill__{slot_id}__k__last_month", drill_out)

def build_all_l2_snapshots_job():
    logger.info("[Job] 開始自動執行全量快照背景任務")
    slot = get_current_slot_id()
    ranges = ["7d", "14d", "28d", "90d", "month"]
    for k in ranges:
        try:
            build_l2_snapshot_for_k(k, slot)
            build_l2_drill_for_k(k, slot, limit=30000)
        except Exception as e:
            logger.error(f"[Job] 產生 {k} 快照失敗: {repr(e)}")
    logger.info("[Job] 自動快照背景任務完成")

def build_monthly_snapshots_job():
    now = get_now_taipei()
    # 取得上個月份的 YYYYMM
    # 例如今天 2026/06/08，上個月就是 202605
    first_of_this_month = now.replace(day=1)
    last_month_dt = first_of_this_month - timedelta(days=1)
    ym = last_month_dt.strftime("%Y%m")
    try:
        build_monthly_snapshot(ym)
    except Exception as e:
        logger.error(f"[Job] 產生月度快照 {ym} 失敗: {repr(e)}")

# =====================================================================
# 9. 快照推導邏輯 (對齊 code.js 內的 filter 與 L2 派生邏輯)
# =====================================================================
def calc_pure_gsc_kpi(gsc_dict: Dict[str, Dict[str, Any]], product: str, product_detail: str, page_map: Dict[str, Any]) -> Dict[str, Any]:
    if not gsc_dict:
        return {"gscClicks": 0, "gscImpressions": 0, "gscCtr": 0.0, "gscPosition": 0.0}
    
    gsc_clicks = 0
    gsc_impr = 0
    gsc_pos_sum = 0.0
    gsc_pos_clicks = 0
    
    for url, item in gsc_dict.items():
        if product or product_detail:
            pinfo = resolve_page_by_params(url, page_map)
            if product and pinfo.get("product") != product:
                continue
            if product_detail and pinfo.get("product_detail") != product_detail:
                continue
                
        clicks = int(item.get("gscClicks") or 0)
        impr = int(item.get("gscImpressions") or 0)
        pos = float(item.get("gscPosition") or 0.0)
        
        gsc_clicks += clicks
        gsc_impr += impr
        if pos > 0 and clicks > 0:
            gsc_pos_sum += pos * clicks
            gsc_pos_clicks += clicks
            
    gsc_pos_w = gsc_pos_sum / gsc_pos_clicks if gsc_pos_clicks > 0 else 0.0
    gsc_ctr = gsc_clicks / gsc_impr if gsc_impr > 0 else 0.0
    
    return {
        "gscClicks": gsc_clicks,
        "gscImpressions": gsc_impr,
        "gscCtr": gsc_ctr,
        "gscPosition": gsc_pos_w
    }

def build_gsc_regexes_for_product(page_map: Dict[str, Any], product: str, product_detail: str = "") -> List[str]:
    """根據產品分類，從 page_map 中反查所有對應的 ID 並組合為 GSC 可用的 Regex 篩選條件列表（依 6 大頁面分類分批，以符合 2048 字元限制）。"""
    by_key = page_map.get("byKey") or {}
    
    cat_param_map = {
        "news_id": "news_id",
        "article_id": "article_id",
        "comment_id": "comment_id",
        "lecture_id": "lecture_id",
        "edm": "edm_id",
        "f_subject_no": "f_subject_no",
        "subject_no": "subject_no"
    }
    
    regexes = []
    has_any_id = False
    
    # 根據 6 大頁面分類切分 Regex
    for cat, items in by_key.items():
        param_name = cat_param_map.get(cat)
        if not param_name:
            continue
            
        ids_for_cat = []
        for item in items:
            if product and item.get("product") != product:
                continue
            if product_detail and item.get("product_detail") != product_detail:
                continue
            item_id = item.get("id")
            if item_id:
                ids_for_cat.append(str(item_id))
                    
        if not ids_for_cat:
            continue
            
        has_any_id = True
        
        # 針對該分類進行自動分批邏輯，並採用 param=(A|B|C) 的極致壓縮語法
        batch_ids = []
        batch_len = 0
        
        for mid in ids_for_cat:
            item_len = len(mid)
            added_len = item_len if not batch_ids else item_len + 1
            
            if cat == "edm":
                # ".*(edm_id=(...)|edm(...)).*" 
                proj_len = 22 + (batch_len + added_len) * 2
            else:
                # ".*param=(...).*"
                wrapper_len = 6 + len(param_name) 
                proj_len = wrapper_len + batch_len + added_len
                
            # 將限制抓得更緊，預留一些安全空間 (網頁版 GSC 限制 1024，為了讓使用者能在網頁版驗證，如果可能我們以 1000 為界限)
            if proj_len > 1000:
                if cat == "edm":
                    regexes.append(f"(edm_id=|edm[_-]?)({'|'.join(batch_ids)})([^a-zA-Z0-9]|$)")
                else:
                    regexes.append(f"{param_name}=({'|'.join(batch_ids)})(&|$)")
                batch_ids = [mid]
                batch_len = item_len
            else:
                batch_ids.append(mid)
                batch_len += added_len
                
        if batch_ids:
            if cat == "edm":
                regexes.append(f"(edm_id=|edm[_-]?)({'|'.join(batch_ids)})([^a-zA-Z0-9]|$)")
            else:
                regexes.append(f"{param_name}=({'|'.join(batch_ids)})(&|$)")
            
    if not has_any_id:
        return ["MATCH_NOTHING_XXX_999"]
        
    return regexes

def calc_pure_gsc_kpi_live(start_date: str, end_date: str, product: str, product_detail: str, page_map: Dict[str, Any]) -> Dict[str, Any]:
    """若有產品篩選，直接打 GSC API 取得精確且去重的產品層級加總指標（支援依照 6 大分類分批 Regex 發送）"""
    site_url = get_gsc_site_url()
    if not site_url:
        return {"gscClicks": 0, "gscImpressions": 0, "gscCtr": 0.0, "gscPosition": 0.0}
        
    regexes = build_gsc_regexes_for_product(page_map, product, product_detail)
    if not regexes or regexes[0] == "MATCH_NOTHING_XXX_999":
        return {"gscClicks": 0, "gscImpressions": 0, "gscCtr": 0.0, "gscPosition": 0.0}
        
    total_clicks = 0
    total_impr = 0
    total_pos_clicks = 0
    total_pos_sum = 0.0
    
    for regex_filter in regexes:
        try:
            res = gsc_query_property_level(site_url, start_date, end_date, page_regex_filter=regex_filter)
            c = int(res.get("gscClicks", 0))
            i = int(res.get("gscImpressions", 0))
            p = float(res.get("gscPosition", 0.0))
            
            total_clicks += c
            total_impr += i
            if p > 0 and c > 0:
                total_pos_sum += p * c
                total_pos_clicks += c
                
        except Exception as e:
            logger.error(f"calc_pure_gsc_kpi_live 批次查詢失敗: {repr(e)}")
            
    gsc_pos_w = total_pos_sum / total_pos_clicks if total_pos_clicks > 0 else 0.0
    gsc_ctr = total_clicks / total_impr if total_impr > 0 else 0.0
    
    return {
        "gscClicks": total_clicks,
        "gscImpressions": total_impr,
        "gscCtr": gsc_ctr,
        "gscPosition": gsc_pos_w
    }


def filter_heat(rows: List[Dict[str, Any]], product: str, group: str, product_detail: str) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        if not r: continue
        row_prod = str(r.get("product") or "").strip()
        if not row_prod: row_prod = "未分類"
        
        if product and row_prod != product: continue
        if product_detail and str(r.get("product_detail") or "") != product_detail: continue
        if group and str(r.get("source_group") or "") != group: continue
        out.append(r)
    return out

def derive_all_from_l2(l2: Dict[str, Any], product: str, group: str, limit: int, product_detail: str) -> Dict[str, Any]:
    def filt_agg(arr):
        return filter_heat(arr or [], product, group, product_detail)
        
    cur_node = l2.get("cur") or {}
    prev_node = l2.get("prev") or {}
    yoy_node = l2.get("yoy") or {}
    
    heat_agg_cur = filt_agg(cur_node.get("heatAgg"))
    heat_agg_prev = filt_agg(prev_node.get("heatAgg"))
    heat_agg_yoy = filt_agg(yoy_node.get("heatAgg"))
    
    heat_agg_by_prod_cur = filt_agg(cur_node.get("heatAggByProduct"))
    heat_agg_by_prod_prev = filt_agg(prev_node.get("heatAggByProduct"))
    heat_agg_by_prod_yoy = filt_agg(yoy_node.get("heatAggByProduct"))
    
    # 合成相容的 heatmap 結構 (前端期望同構)
    def synth_heatmap(agg_list):
        out = []
        for r in agg_list:
            users = r.get("users") or 0
            sessions = r.get("sessions") or 0
            er = r.get("engagementRate") or 0.0
            dur = r.get("avgDuration") or r.get("avgSessionSec") or 0.0
            out.append({
                "source_group": r.get("source_group") or "",
                "source_name": r.get("source_name") or r.get("source") or "",
                "source": r.get("source") or r.get("source_name") or "",
                "source_sub": r.get("source_sub") or "",
                "users": users,
                "sessions": sessions,
                "engagementRate": er,
                "avgDuration": dur,
                "avgSessionSec": dur,
                "avgSessionsPerUser": (sessions / users) if users > 0 else 0.0,
                "score": quality_score(er, dur),
                "lp": "",
                "display_title": "",
                "product": r.get("product") or "",
                "product_detail": r.get("product_detail") or "",
                "category": "",
                "pageId": "",
                "gscClicks": int(r.get("gscClicks") or 0),
                "gscImpressions": int(r.get("gscImpressions") or 0),
                "gscPosition": float(r.get("gscPosition") or 0.0)
            })
        return out[:limit]
        
    heatmap_cur = synth_heatmap(heat_agg_cur)
    heatmap_prev = synth_heatmap(heat_agg_prev)
    heatmap_yoy = synth_heatmap(heat_agg_yoy)
    
    # 圓餅圖
    pie_map = {}
    total_users = 0
    for r in heat_agg_cur:
        g = r.get("source_group") or "未分類"
        u = r.get("users") or 0
        pie_map[g] = pie_map.get(g, 0) + u
        total_users += u
        
    pie_rows = [{"group": k, "users": v} for k, v in pie_map.items()]
    pie_rows = sorted(pie_rows, key=lambda x: x["users"], reverse=True)
    pie = {"totalUsersApprox": total_users, "rows": pie_rows}
    
    # KPI 計算
    kpi_cur = cur_node.get("kpi") or {"users": 0, "sessions": 0, "engagementRate": 0.0, "avgSessionSec": 0.0}
    kpi_prev = prev_node.get("kpi") or {"users": 0, "sessions": 0, "engagementRate": 0.0, "avgSessionSec": 0.0}
    kpi_yoy = yoy_node.get("kpi") or {"users": 0, "sessions": 0, "engagementRate": 0.0, "avgSessionSec": 0.0}
    
    # 如果有篩選，利用加權重新估計 KPI
    has_filter = bool(product or group or product_detail)
    
    # 補足快照中缺少的 GSC 全站 KPI 數據（當沒有篩選且快照缺少欄位時）
    if not has_filter:
        start_date = l2.get("_snapshot", {}).get("startDate")
        end_date = l2.get("_snapshot", {}).get("endDate")
        if start_date and end_date:
            cmp = compute_prev_and_yoy_ranges(start_date, end_date)
            if "gscClicks" not in kpi_cur:
                try:
                    real_gsc = get_gsc_kpi(None, start_date, end_date)
                    kpi_cur.update(real_gsc)
                except Exception as e:
                    logger.error(f"即時補足當期 GSC KPI 失敗: {repr(e)}")
            if "gscClicks" not in kpi_prev:
                try:
                    real_gsc_prev = get_gsc_kpi(None, cmp["prev"]["start"], cmp["prev"]["end"])
                    kpi_prev.update(real_gsc_prev)
                except Exception as e:
                    logger.error(f"即時補足前期 GSC KPI 失敗: {repr(e)}")
            if "gscClicks" not in kpi_yoy:
                try:
                    real_gsc_yoy = get_gsc_kpi(None, cmp["yoy"]["start"], cmp["yoy"]["end"])
                    kpi_yoy.update(real_gsc_yoy)
                except Exception as e:
                    logger.error(f"即時補足去年 GSC KPI 失敗: {repr(e)}")
    def calc_kpi_from_heat(h_list, raw_heat_list=None):
        if not h_list:
            return {
                "users": 0, "sessions": 0, "engagementRate": 0.0, "avgSessionSec": 0.0
            }
        total_s = sum(r.get("sessions", 0) for r in h_list)
        total_u = sum(r.get("users", 0) for r in h_list)
        weighted_er = sum(r.get("engagementRate", 0.0) * r.get("sessions", 0) for r in h_list) / total_s if total_s > 0 else 0.0
        weighted_dur = sum(r.get("avgDuration", 0.0) * r.get("sessions", 0) for r in h_list) / total_s if total_s > 0 else 0.0
        
        return {
            "users": total_u,
            "sessions": total_s,
            "engagementRate": weighted_er,
            "avgSessionSec": weighted_dur,
            "_approxUsers": True
        }
        
    page_map = load_page_map()
    
    if has_filter:
        raw_heat_cur = [] # Snapshot does not store 'heat'
        raw_heat_prev = []
        raw_heat_yoy = []
        
        if product or product_detail:
            heatmap_cur = synth_heatmap(heat_agg_by_prod_cur)
            heatmap_prev = synth_heatmap(heat_agg_by_prod_prev)
            heatmap_yoy = synth_heatmap(heat_agg_by_prod_yoy)
        else:
            heatmap_cur = synth_heatmap(heat_agg_cur)
            heatmap_prev = synth_heatmap(heat_agg_prev)
            heatmap_yoy = synth_heatmap(heat_agg_yoy)
        
        kpi_cur.update(calc_kpi_from_heat(heatmap_cur, raw_heat_cur))
        kpi_prev.update(calc_kpi_from_heat(heatmap_prev, raw_heat_prev))
        kpi_yoy.update(calc_kpi_from_heat(heatmap_yoy, raw_heat_yoy))
        
        start_date = l2.get("_snapshot", {}).get("startDate")
        end_date = l2.get("_snapshot", {}).get("endDate")
        
        if start_date and end_date:
            cmp = compute_prev_and_yoy_ranges(start_date, end_date)
            kpi_cur.update(calc_pure_gsc_kpi_live(start_date, end_date, product, product_detail, page_map))
            kpi_prev.update(calc_pure_gsc_kpi_live(cmp["prev"]["start"], cmp["prev"]["end"], product, product_detail, page_map))
            kpi_yoy.update(calc_pure_gsc_kpi_live(cmp["yoy"]["start"], cmp["yoy"]["end"], product, product_detail, page_map))
        else:
            kpi_cur.update(calc_pure_gsc_kpi(cur_node.get("gscRaw"), product, product_detail, page_map))
            kpi_prev.update(calc_pure_gsc_kpi(prev_node.get("gscRaw"), product, product_detail, page_map))
            kpi_yoy.update(calc_pure_gsc_kpi(yoy_node.get("gscRaw"), product, product_detail, page_map))
        
    drilldown = build_product_drilldown_data(page_map, product)
    
    return {
        "ok": True,
        "_from": "l2_snapshot",
        "_snapshot": l2.get("_snapshot") or {},
        "filters": l2.get("filters") or {},
        "kpi": kpi_cur,
        "kpi_prev": kpi_prev,
        "kpi_yoy": kpi_yoy,
        "heatmap": heatmap_cur,
        "heatmap_prev": heatmap_prev,
        "heatmap_yoy": heatmap_yoy,
        "heatAgg": heat_agg_cur[:limit],
        "heatAgg_prev": heat_agg_prev[:limit],
        "heatAgg_yoy": heat_agg_yoy[:limit],
        "heatAggByProduct": heat_agg_by_prod_cur[:limit],
        "heatAggByProduct_prev": heat_agg_by_prod_prev[:limit],
        "heatAggByProduct_yoy": heat_agg_by_prod_yoy[:limit],
        "pie": pie,
        "drilldown": drilldown
    }

def build_page_metrics_from_rows(rows: List[Dict[str, Any]], page_map: Dict[str, Any], gsc_dict: Dict[str, Dict[str, Any]] = None, product: str = "", product_detail: str = "") -> Dict[str, List[Dict[str, Any]]]:
    CATS = ['news_id', 'article_id', 'comment_id', 'lecture_id', 'edm', 'f_subject_no', 'subject_no']
    
    def extract_key(r: Dict[str, Any], cat: str) -> str:
        k = ""
        if r.get("category") == cat and r.get("pageId"):
            k = str(r.get("pageId")).strip()
        elif r.get(cat) is not None and str(r.get(cat)).strip() != "":
            k = str(r.get(cat)).strip()
        else:
            url = str(r.get("lp") or "")
            if url:
                if cat == "edm":
                    k = _extract_edm_key_from_url_(url, r)
                else:
                    cat_pat = cat.replace("_", "[_-]?")
                    m = re.search(r"(?:^|[?&])" + cat_pat + r"=([^&#]+)", url, re.IGNORECASE)
                    if m:
                        k = urllib.parse.unquote(m.group(1))
                        
        if cat == "edm" and k.lower().startswith("edm"):
            k = re.sub(r"^edm[_-]?", "", k, flags=re.IGNORECASE)
            
        return k
        
    # O(1) Lookup cache
    key_hash = {}
    for c, arr in (page_map.get("byKey") or {}).items():
        key_hash[c] = {}
        for r in arr:
            key_hash[c][str(r.get("id")).lower()] = r

    def lookup_title(cat: str, key: str) -> str:
        candidates = [key]
        if cat == "edm":
            if key.startswith("edm"): candidates.append(re.sub(r"^edm[_-]?", "", key, flags=re.IGNORECASE))
            else: candidates.extend([f"edm{key}", f"edm_{key}", f"edm-{key}"])
        dct = key_hash.get(cat, {})
        for cand in candidates:
            c = str(cand).lower()
            if c in dct: return dct[c].get("name") or key
        return f"EDM {key}" if cat == "edm" else key

    def check_product(cat: str, key: str) -> bool:
        if not product and not product_detail: return True
        candidates = [key]
        if cat == "edm":
            if key.startswith("edm"): candidates.append(re.sub(r"^edm[_-]?", "", key, flags=re.IGNORECASE))
            else: candidates.extend([f"edm{key}", f"edm_{key}", f"edm-{key}"])
        dct = key_hash.get(cat, {})
        for cand in candidates:
            c = str(cand).lower()
            if c in dct:
                r = dct[c]
                if product and str(r.get("product") or "") != product: return False
                if product_detail and str(r.get("product_detail") or "") != product_detail: return False
                return True
        return False

    out = {}
    for cat in CATS:
        acc = {}
        # 1. 處理 GA4 流量與列內建 GSC 數據 (如 Mock Data)
        for r in rows:
            key = extract_key(r, cat)
            if not key: continue
            
            views = int(r.get("users") or r.get("sessions") or r.get("views") or 0)
            er = float(r.get("engagementRate") or 0.0)
            dur = float(r.get("avgDuration") or r.get("avgSessionSec") or 0.0)
            
            clicks = int(r.get("gscClicks") or 0)
            impr = int(r.get("gscImpressions") or 0)
            pos = float(r.get("gscPosition") or 0.0)
            
            if key not in acc:
                acc[key] = {
                    "views": 0, "erW": 0.0, "durW": 0.0,
                    "gscClicks": 0, "gscImpressions": 0, "gscPosW": 0.0, "gscClicksForPos": 0,
                    "gscPageUrl": ""
                }
            item = acc[key]
            item["views"] += views
            item["erW"] += views * er
            item["durW"] += views * dur
            item["gscClicks"] += clicks
            item["gscImpressions"] += impr
            if pos > 0 and clicks > 0:
                item["gscPosW"] += pos * clicks
                item["gscClicksForPos"] += clicks
            if not item["gscPageUrl"] and r.get("lp"):
                item["gscPageUrl"] = r.get("lp")
                
        # 2. 處理 GSC 資料 (獨立累加)
        if gsc_dict:
            matched = 0
            for url, g_item in gsc_dict.items():
                r_mock = {"lp": url}
                key = extract_key(r_mock, cat)
                if not key: continue
                if not check_product(cat, key): continue
                matched += 1
                
                if key not in acc:
                    acc[key] = {
                        "views": 0, "erW": 0.0, "durW": 0.0,
                        "gscClicks": 0, "gscImpressions": 0, "gscPosW": 0.0, "gscClicksForPos": 0,
                        "gscPageUrl": url
                    }
                item = acc[key]
                clicks = int(g_item.get("gscClicks") or 0)
                impr = int(g_item.get("gscImpressions") or 0)
                pos = float(g_item.get("gscPosition") or 0.0)
                
                item["gscClicks"] += clicks
                item["gscImpressions"] += impr
                if pos > 0 and clicks > 0:
                    item["gscPosW"] += pos * clicks
                    item["gscClicksForPos"] += clicks
                # 永遠優先使用 GSC 提供的標準網址，避免 GA4 帶有追蹤參數的網址導致 API 查無資料
                item["gscPageUrl"] = url
            
        packed = []
        for k, v in acc.items():
            views_sum = v["views"]
            name = lookup_title(cat, k)
            
            pos_avg = 0.0
            if v["gscClicksForPos"] > 0:
                pos_avg = v["gscPosW"] / v["gscClicksForPos"]
            
            packed.append({
                "id": k,
                "name": name,
                "views": int(views_sum),
                "engagementRate": float(v["erW"] / views_sum) if views_sum > 0 else 0.0,
                "avgSec": float(v["durW"] / views_sum) if views_sum > 0 else 0.0,
                "gscClicks": v["gscClicks"],
                "gscImpressions": v["gscImpressions"],
                "gscCtr": v["gscClicks"] / v["gscImpressions"] if v["gscImpressions"] > 0 else 0.0,
                "gscPosition": pos_avg,
                "gscPageUrl": v["gscPageUrl"],
                "category": cat
            })
        if gsc_dict:
            logger.info(f"[GSC match] cat={cat}: {matched}/{len(gsc_dict)} URLs matched -> {len(packed)} items")
        out[cat] = sorted(packed, key=lambda x: x["views"], reverse=True)
    return out

def enhance_page_metrics_with_live_gsc(page_metrics: Dict[str, List[Dict[str, Any]]], start_date: str, end_date: str, top_n: int = 30) -> Dict[str, List[Dict[str, Any]]]:
    """針對 6 大頁面明細表，對每個 GA4 流量排名前 N 的 ID 發送獨立的去重 GSC API 查詢。"""
    site_url = get_gsc_site_url()
    if not site_url or not start_date or not end_date:
        return page_metrics
        
    cat_param_map = {
        "news_id": "news_id",
        "article_id": "article_id",
        "comment_id": "comment_id",
        "lecture_id": "lecture_id",
        "edm": "edm_id",
        "f_subject_no": "f_subject_no",
        "subject_no": "subject_no"
    }
    
    total_calls = 0
    max_calls = 100 # 設定安全上限
    
    for cat, items in page_metrics.items():
        param_name = cat_param_map.get(cat)
        if not param_name: continue
        
        # 僅對前 top_n 筆執行
        targets = items[:top_n]
        for item in targets:
            if total_calls >= max_calls:
                break
            item_id = item.get("id")
            if not item_id: continue
            
            expr = f".*{param_name}={item_id}.*"
            if cat == "edm":
                expr = f".*({param_name}={item_id}|edm{item_id}).*"
                
            try:
                gsc_data = gsc_query_property_level(site_url, start_date, end_date, page_regex_filter=expr)
                item["gscClicks"] = gsc_data.get("gscClicks", 0)
                item["gscImpressions"] = gsc_data.get("gscImpressions", 0)
                item["gscCtr"] = gsc_data.get("gscCtr", 0.0)
                item["gscPosition"] = gsc_data.get("gscPosition", 0.0)
                total_calls += 1
            except Exception as e:
                logger.error(f"enhance_page_metrics_with_live_gsc API 失敗 (cat={cat}, id={item_id}): {repr(e)}")
                
        if total_calls >= max_calls:
            logger.warning("達到 enhance_page_metrics_with_live_gsc 安全 API 呼叫上限")
            break
            
    return page_metrics

def calculate_seo_diagnostics_top5(page_metrics: Dict[str, List[Dict[str, Any]]], page_metrics_yoy: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    # 建立 yoy 的 lookup map
    yoy_map = {}
    for cat, items in page_metrics_yoy.items():
        for item in items:
            yoy_map[str(item.get("id"))] = item

    # 計算各頁型的 P75 曝光門檻
    cat_p75_map = {}
    for cat, items in page_metrics.items():
        imprs = sorted([item.get("gscImpressions", 0) for item in items])
        if imprs:
            # P75 index
            idx = int(len(imprs) * 0.75)
            cat_p75_map[cat] = imprs[idx]
        else:
            cat_p75_map[cat] = 0

    scored_items = []
    
    for cat, items in page_metrics.items():
        p75_impr = cat_p75_map.get(cat, 0)
        
        for item in items:
            name = item.get("name", "")
            
            # 排除 標題年分非今年(2026 / 115)
            year_match = re.search(r'20\d{2}', name)
            if year_match and year_match.group(0) != '2026':
                continue
                
            roc_match = re.search(r'(?<!\d)(10\d|11\d)(?!\d)', name)
            if roc_match and roc_match.group(1) != '115':
                continue
                
            impr = item.get("gscImpressions", 0)
            clicks = item.get("gscClicks", 0)
            ctr = item.get("gscCtr", 0.0)
            cur_pos = item.get("gscPosition", 0.0)
            item_id = str(item.get("id"))
            
            yoy_pos = 0.0
            yoy_clicks = 0
            yoy_impr = 0
            yoy_ctr = 0.0
            if item_id in yoy_map:
                yoy_pos = yoy_map[item_id].get("gscPosition", 0.0)
                yoy_clicks = yoy_map[item_id].get("gscClicks", 0)
                yoy_impr = yoy_map[item_id].get("gscImpressions", 0)
                yoy_ctr = yoy_map[item_id].get("gscCtr", 0.0)
                
            # 最低樣本門檻
            if impr + yoy_impr < 100:
                continue
                
            archetype = ""
            symptom = ""
            suggestion = ""
            score = 0.0

            delta_impr = impr - yoy_impr
            delta_clicks = clicks - yoy_clicks

            click_loss = yoy_clicks - clicks
            
            # 判斷邏輯
            # 確保曝光量有一定基礎 (避免 p75_impr = 0 導致 0 曝光被判定為高曝光)
            p75_safe = max(p75_impr, 50)
            
            is_seo_anomaly = (clicks < yoy_clicks) and (ctr < yoy_ctr) and (cur_pos > yoy_pos) and (impr >= p75_safe)
            is_normal_drop = (clicks < yoy_clicks) and (impr < yoy_impr) and (ctr >= yoy_ctr) and (cur_pos <= yoy_pos)
            is_serp_ctr_issue = (impr >= yoy_impr) and (clicks < yoy_clicks) and (ctr < yoy_ctr) and (cur_pos <= yoy_pos)
            is_high_impr_low_ctr = (impr >= p75_safe) and (cur_pos > 0 and cur_pos <= 10) and (ctr < 0.01)
            is_one_step_away = (impr >= p75_safe) and (cur_pos >= 4) and (cur_pos <= 15)

            gsc_phenomenon = ""
            priority_action = ""

            if is_seo_anomaly:
                archetype = "🚨 排名衰退 (SEO 異常)"
                symptom = f"點擊損 {click_loss:,}"
                gsc_phenomenon = "點擊↓ 曝光↓ 排名↓"
                priority_action = "更新內容、補內鏈"
                score = 100000 + click_loss
            elif is_serp_ctr_issue:
                archetype = "⚠️ CTR不足 (SERP 問題)"
                symptom = f"點擊損 {click_loss:,}"
                gsc_phenomenon = "曝光未降 排名穩 CTR低"
                priority_action = "改標題與描述"
                score = 10000 + click_loss
            elif is_normal_drop:
                archetype = "❄️ 搜尋需求下降 (正常波動)"
                symptom = f"點擊損 {click_loss:,}"
                gsc_phenomenon = "曝光↓ 排名穩"
                priority_action = "檢查考程/季節性"
                score = 1000 + click_loss
            elif is_high_impr_low_ctr:
                archetype = "👀 高曝光低 CTR"
                symptom = f"曝光 {impr:,}"
                gsc_phenomenon = "曝光高 排名佳 CTR低"
                priority_action = "檢查SERP意圖"
                score = 500 + impr / 1000
            elif is_one_step_away:
                archetype = "🚀 快進首頁 (臨門一腳)"
                symptom = f"排名第 {cur_pos:.1f} 名"
                gsc_phenomenon = "排名4-15 曝光高"
                priority_action = "補內容深度"
                score = 100 + impr / 1000

            # GA4 高價值判斷
            if (clicks < yoy_clicks) and (item.get("views", 0) >= 100):
                if not archetype:
                    archetype = "💎 高價值流量流失"
                    symptom = f"點擊損 {click_loss:,}"
                    gsc_phenomenon = "點擊↓ 但GA4流量高"
                    priority_action = "優先維護"
                    score = 200000 + click_loss
                else:
                    priority_action = "⭐[高價值] " + priority_action
                
            if archetype:
                scored_items.append({
                    "id": item_id,
                    "name": name,
                    "gscPageUrl": item.get("gscPageUrl", ""),
                    "category": cat,
                    "score": round(score, 1),
                    "archetype": archetype,
                    "symptom": symptom,
                    "gscPhenomenon": gsc_phenomenon,
                    "priorityAction": priority_action,
                    "gscClicks": clicks,
                    "gscImpressions": impr,
                    "gscCtr": ctr,
                    "gscPosition": cur_pos,
                    "deltaClicks": delta_clicks,
                    "deltaImpr": delta_impr
                })
                
    scored_items.sort(key=lambda x: x["score"], reverse=True)
    
    # 重新給予固定分數以符合前端進度條顯示 (100 為全滿)
    for item in scored_items:
        if "高價值流量流失" in item["archetype"]:
            item["score"] = 100
        elif "SEO 異常" in item["archetype"]:
            item["score"] = 90
        elif "SERP" in item["archetype"]:
            item["score"] = 75
        elif "正常" in item["archetype"]:
            item["score"] = 40
        elif "高曝光低" in item["archetype"]:
            item["score"] = 60
        elif "臨門" in item["archetype"]:
            item["score"] = 30
        else:
            item["score"] = 50

    return scored_items
def get_gsc_data_dict_by_regexes(start_date: str, end_date: str, regexes: List[str]) -> Dict[str, Dict[str, Any]]:
    import concurrent.futures
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    import requests
    from google.auth.transport.requests import Request as AuthRequest
    
    site_url = get_gsc_site_url()
    if not site_url or not regexes:
        return {}
        
    creds = _get_creds()
    if not creds:
        return {}
        
    # 確保 token 有效 (單一執行緒預先 Refresh，避免多執行緒競爭)
    if not creds.valid:
        creds.refresh(AuthRequest())
    token = creds.token
        
    def fetch_regex(reg):
        url = f"https://searchconsole.googleapis.com/webmasters/v3/sites/{urllib.parse.quote(site_url, safe='')}/searchAnalytics/query"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        body = {
            'startDate': start_date,
            'endDate': end_date,
            'rowLimit': 10000,
            'dimensions': ['page'],
            'dimensionFilterGroups': [{
                'filters': [{'dimension': 'page', 'operator': 'includingRegex', 'expression': reg}]
            }]
        }
        for attempt in range(3):
            try:
                # 使用 requests 替代 googleapiclient，徹底解決 httplib2 的 Thread-Safety 與 SSL 問題
                res = requests.post(url, headers=headers, json=body, timeout=15)
                if res.status_code == 200:
                    return res.json().get('rows', [])
                elif res.status_code == 429: # Rate limit
                    time.sleep(2 + attempt * 2)
                    continue
                else:
                    logger.error(f"GSC query regex HTTP {res.status_code}: {res.text}")
                    time.sleep(1 + attempt)
            except Exception as e:
                logger.error(f"GSC query regex fail (attempt {attempt+1}): {repr(e)}")
                time.sleep(1 + attempt)
        return []

    all_rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_regex, r) for r in regexes]
        for f in concurrent.futures.as_completed(futures):
            try:
                all_rows.extend(f.result())
            except Exception as e:
                logger.error(f"Thread pool fail: {repr(e)}")
                
    gsc_map = {}
    for row in all_rows:
        page_url = row['keys'][0]
        clicks = int(row.get('clicks', 0))
        impr = int(row.get('impressions', 0))
        pos = float(row.get('position', 0.0))
        
        full_key = url_path_with_query(page_url)
        key = full_key or page_url
        if key:
            if key not in gsc_map:
                gsc_map[key] = {"gscClicks": 0, "gscImpressions": 0, "gscPosW": 0.0, "gscClicksForPos": 0}
            gsc_map[key]["gscClicks"] += clicks
            gsc_map[key]["gscImpressions"] += impr
            if pos > 0 and clicks > 0:
                gsc_map[key]["gscPosW"] += pos * clicks
                gsc_map[key]["gscClicksForPos"] += clicks

    final_map = {}
    for k, v in gsc_map.items():
        p = 0.0
        if v["gscClicksForPos"] > 0:
            p = v["gscPosW"] / v["gscClicksForPos"]
        final_map[k] = {
            "gscClicks": v["gscClicks"],
            "gscImpressions": v["gscImpressions"],
            "gscPosition": p
        }
    return final_map

def derive_drill_from_drill_snapshot(drill: Dict[str, Any], product: str, group: str, limit: int, product_detail: str, gsc_raw_prev: Dict[str, Any] = None) -> Dict[str, Any]:
    rows = drill.get("rows") or drill.get("cur", {}).get("heat") or []
    filtered_unlimited = filter_heat(rows, product, group, product_detail)
    filtered = filtered_unlimited[:limit]
    
    page_map = load_page_map()
    
    snap_info = drill.get("_snapshot") or {}
    start_date = snap_info.get("startDate")
    end_date = snap_info.get("endDate")
    
    if product and start_date and end_date:
        logger.info(f"[Live GSC] 產品 '{product}' 已選擇，啟動 100% 精確 GSC Regex 並行查詢...")
        regexes = build_gsc_regexes_for_product(page_map, product, product_detail)
        gsc_raw_cur = get_gsc_data_dict_by_regexes(start_date, end_date, regexes)
        page_metrics = build_page_metrics_from_rows(filtered_unlimited, page_map, gsc_dict=gsc_raw_cur, product=product, product_detail=product_detail)
        
        cmp = compute_prev_and_yoy_ranges(start_date, end_date)
        gsc_raw_prev_regex = get_gsc_data_dict_by_regexes(cmp["prev"]["start"], cmp["prev"]["end"], regexes)
        gsc_raw_yoy_regex = get_gsc_data_dict_by_regexes(cmp["yoy"]["start"], cmp["yoy"]["end"], regexes)
        
        p_rows = drill.get("prev", {}).get("heat", [])
        filt_p_unlim = filter_heat(p_rows, product, group, product_detail)
        filt_p = filt_p_unlim[:limit]
        page_metrics_prev = build_page_metrics_from_rows(filt_p_unlim, page_map, gsc_dict=gsc_raw_prev_regex, product=product, product_detail=product_detail)
        
        y_rows = drill.get("yoy", {}).get("heat", [])
        filt_y_unlim = filter_heat(y_rows, product, group, product_detail)
        filt_y = filt_y_unlim[:limit]
        page_metrics_yoy = build_page_metrics_from_rows(filt_y_unlim, page_map, gsc_dict=gsc_raw_yoy_regex, product=product, product_detail=product_detail)
    else:
        # gscRaw 可能在頂層 (l2_drill 格式) 或 cur.gscRaw (monthly 格式)
        gsc_raw = drill.get("gscRaw") or drill.get("cur", {}).get("gscRaw") or {}
        page_metrics = build_page_metrics_from_rows(filtered_unlimited, page_map, gsc_dict=gsc_raw, product=product, product_detail=product_detail)
        
        # 優先從快照讀取 prev.page 和 yoy.page (只有在無產品篩選時才能直接用)
        snap_prev_page = drill.get("prev", {}).get("page")
        snap_yoy_page = drill.get("yoy", {}).get("page")
        
        if snap_prev_page and snap_yoy_page and not product and not product_detail:
            page_metrics_prev = snap_prev_page
            page_metrics_yoy = snap_yoy_page
        else:
            p_rows = drill.get("prev", {}).get("heat", [])
            filt_p_unlim = filter_heat(p_rows, product, group, product_detail)
            filt_p = filt_p_unlim[:limit]
            p_gsc = drill.get("prev", {}).get("gscRaw") or {}
            page_metrics_prev = build_page_metrics_from_rows(filt_p_unlim, page_map, gsc_dict=p_gsc, product=product, product_detail=product_detail)
            
            y_rows = drill.get("yoy", {}).get("heat", [])
            filt_y_unlim = filter_heat(y_rows, product, group, product_detail)
            filt_y = filt_y_unlim[:limit]
            y_gsc = drill.get("yoy", {}).get("gscRaw") or {}
            page_metrics_yoy = build_page_metrics_from_rows(filt_y_unlim, page_map, gsc_dict=y_gsc, product=product, product_detail=product_detail)

    try:
        diagnostics_top5 = calculate_seo_diagnostics_top5(page_metrics, page_metrics_yoy)
        if not diagnostics_top5 and drill.get("diagnosticsTop5"):
            diagnostics_top5 = drill.get("diagnosticsTop5")
    except Exception as e:
        logger.error(f"calculate_seo_diagnostics_top5 發生錯誤: {repr(e)}")
        diagnostics_top5 = drill.get("diagnosticsTop5") or []

    return {
        "ok": True,
        "_from": "drill_snapshot",
        "_snapshot": snap_info,
        "pageMetrics": page_metrics,
        "pageMetrics_prev": page_metrics_prev,
        "pageMetrics_yoy": page_metrics_yoy,
        "diagnosticsTop5": diagnostics_top5
    }


# =====================================================================
# 9.5 員編驗證機制 (Employee ID Auth)
# =====================================================================
def check_emp_id(emp_id: str) -> bool:
    if not emp_id:
        return False
    allowed_file = os.path.join(get_base_dir(), "allowed_emp_ids.txt")
    if not os.path.exists(allowed_file):
        return True  # 若未設定檔案則預設全放行
    with open(allowed_file, "r", encoding="utf-8") as f:
        allowed_ids = [line.strip().upper() for line in f.readlines() if line.strip()]
    return emp_id.strip().upper() in allowed_ids

# =====================================================================
# 9.6 稽核紀錄機制 (Audit Logging)
# =====================================================================
def log_audit_event(emp_id: str, action: str, ip: str, details: str = ""):
    log_file = os.path.join(get_base_dir(), "usage_logs.csv")
    file_exists = os.path.exists(log_file)
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with open(log_file, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Timestamp", "EmpID", "Action", "IP", "Details"])
        writer.writerow([timestamp, emp_id, action, ip, details])
        
    if action == "LOGIN_SUCCESS":
        logger.info(f"[92m[登入成功] 員編: {emp_id}, IP: {ip}[0m")
    elif action == "LOGIN_FAILED":
        logger.warning(f"[93m[登入失敗] 員編: {emp_id}, IP: {ip}[0m")
    elif action == "REQUEST":
        logger.info(f"[資料請求] 員編: {emp_id}, 動作: {details}")

@app.get("/login")
def login(request: FastAPIRequest, emp_id: str = Query(..., description="員工編號")):
    ip = request.client.host if (request and getattr(request, 'client', None)) else "unknown"
    if check_emp_id(emp_id):
        log_audit_event(emp_id, "LOGIN_SUCCESS", ip)
        return {"ok": True}
    log_audit_event(emp_id, "LOGIN_FAILED", ip)
    return {"ok": False, "error": "無效的員工編號"}

# =====================================================================
# 10. API 路由與控制器定義 (對齊 doGet 參數與行為)
# =====================================================================
@app.get("/exec")
def exec_get(
    request: FastAPIRequest,
    emp_id: str = "",
    type: str = Query(..., description="API 功能：all | drill | export | gsc_queries | kpi"),
    k: str = Query("", description="時間區間 (7d, 28d, last_month)"),
    product: str = "",
    product_detail: str = "",
    productDetail: str = "",
    source_group: str = "",
    limit: int = 30000,
    ym: str = "",
    id: str = "",
    category: str = "",
    page_url: str = "",
    invalidate: int = 0,
    nocache: int = 0,
    debug: int = 0,
    query: str = "",
    background_tasks: BackgroundTasks = BackgroundTasks(),
):
    # 雙軌參數支援
    prod_det = product_detail or productDetail
    
    print(f"DEBUG exec_get: received type={type}, emp_id={repr(emp_id)}")
    if not check_emp_id(emp_id):
        print(f"DEBUG exec_get: check_emp_id failed for emp_id={repr(emp_id)}")
        raise HTTPException(status_code=401, detail="Unauthorized")

    ip = request.client.host if (request and getattr(request, 'client', None)) else "unknown"
    log_audit_event(emp_id, "REQUEST", ip, f"type={type}, k={k}")

    # 診斷 API
    if type == "ping":
        return {"ok": True, "now": get_now_taipei().isoformat()}
    if type == "whoami":
        return {"effectiveUser": "local-user", "now": get_now_taipei().isoformat()}
    if type == "sheet_debug":
        try:
            load_mappings()
            load_page_map()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    if type == "ga_debug":
        client = get_ga4_client()
        return {"ok": client is not None}
        
    if type == "kpi":
        # 供前端 warmup 用的空請求，直接回傳成功
        return {"ok": True}

    if is_mock_mode():
        master = load_mock_snapshots_master()
        mock_queries = (master.get("_meta", {}).get("queries", [])) if master else [
            {"query": "2026 控油洗髮精 推薦 蓬鬆", "gscImpressions": 15800, "gscClicks": 2350, "gscCtr": 0.1487, "gscPosition": 1.8},
            {"query": "保濕精華液 乾敏肌 評價 評測", "gscImpressions": 12400, "gscClicks": 1680, "gscCtr": 0.1355, "gscPosition": 2.3},
            {"query": "無糖氣泡水 箱購 免運 折扣", "gscImpressions": 9800, "gscClicks": 1150, "gscCtr": 0.1173, "gscPosition": 3.1},
            {"query": "有機燕麥奶 拿鐵 特調 搭配", "gscImpressions": 8400, "gscClicks": 920, "gscCtr": 0.1095, "gscPosition": 3.8},
            {"query": "低粉塵 豆腐貓砂 除臭 結塊 評比", "gscImpressions": 7200, "gscClicks": 780, "gscCtr": 0.1083, "gscPosition": 4.2}
        ]
        if type in ("gsc_queries", "gsc_top_queries"):
            if type == "gsc_top_queries":
                queries = [{"query": x.get("query", x.get("q")), "clicks": x.get("gscClicks", x.get("clicks")), "impressions": x.get("gscImpressions", x.get("impr")), "ctr": x.get("gscCtr", x.get("ctr")), "position": x.get("gscPosition", x.get("pos"))} for x in mock_queries]
                return {"ok": True, "queries": queries}
            return {"ok": True, "gscQueries": mock_queries[:limit]}
            
        if type == "page_queries":
            target_id = id
            if not target_id and page_url:
                import re
                m = re.search(r'(?:id|_id|_no)=([a-zA-Z0-9_-]+)', page_url)
                if m:
                    target_id = m.group(1)
            
            page_custom_map = {
                "1001": [
                    {"query": "2026 控油洗髮精 推薦 蓬鬆", "clicks": 2350, "yoy_clicks": 2100, "deltaClicks": 250, "impressions": 15800, "yoy_impr": 14200, "deltaImpr": 1600, "ctr": 0.1487, "yoy_ctr": 0.1478, "deltaCtr": 0.0009, "position": 1.8, "yoy_pos": 2.1, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "洗髮精 控油 持香 推薦 品牌", "clicks": 1480, "yoy_clicks": 1620, "deltaClicks": -140, "impressions": 11200, "yoy_impr": 10500, "deltaImpr": 700, "ctr": 0.1321, "yoy_ctr": 0.1542, "deltaCtr": -0.0221, "position": 2.4, "yoy_pos": 2.2, "deltaPos": 0.2, "judgement": "曝光增加但CTR差"},
                    {"query": "夏季 油頭 蓬鬆 洗髮精 評比", "clicks": 920, "yoy_clicks": 1180, "deltaClicks": -260, "impressions": 8900, "yoy_impr": 9400, "deltaImpr": -500, "ctr": 0.1033, "yoy_ctr": 0.1255, "deltaCtr": -0.0222, "position": 3.2, "yoy_pos": 2.6, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "1002": [
                    {"query": "115年 有機燕麥奶 推薦 品牌", "clicks": 2474, "yoy_clicks": 2150, "deltaClicks": 324, "impressions": 44233, "yoy_impr": 39000, "deltaImpr": 5233, "ctr": 0.0559, "yoy_ctr": 0.0551, "deltaCtr": 0.0008, "position": 3.7, "yoy_pos": 4.1, "deltaPos": -0.4, "judgement": "成長中字詞"},
                    {"query": "無糖氣泡水 飲品 趨勢 箱購", "clicks": 1820, "yoy_clicks": 2100, "deltaClicks": -280, "impressions": 28500, "yoy_impr": 26000, "deltaImpr": 2500, "ctr": 0.0638, "yoy_ctr": 0.0807, "deltaCtr": -0.0169, "position": 4.1, "yoy_pos": 3.8, "deltaPos": 0.3, "judgement": "曝光增加但CTR差"},
                    {"query": "健康飲食 燕麥奶 氣泡水 特調", "clicks": 1120, "yoy_clicks": 1450, "deltaClicks": -330, "impressions": 19200, "yoy_impr": 21000, "deltaImpr": -1800, "ctr": 0.0583, "yoy_ctr": 0.0690, "deltaCtr": -0.0107, "position": 4.8, "yoy_pos": 4.2, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "1003": [
                    {"query": "濃縮洗衣精 植萃 防蟎 抗菌", "clicks": 1950, "yoy_clicks": 1720, "deltaClicks": 230, "impressions": 23400, "yoy_impr": 21000, "deltaImpr": 2400, "ctr": 0.0833, "yoy_ctr": 0.0819, "deltaCtr": 0.0014, "position": 2.8, "yoy_pos": 3.1, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "環保 清潔劑 洗衣精 配方 認證", "clicks": 1120, "yoy_clicks": 1380, "deltaClicks": -260, "impressions": 16800, "yoy_impr": 15200, "deltaImpr": 1600, "ctr": 0.0666, "yoy_ctr": 0.0907, "deltaCtr": -0.0241, "position": 3.9, "yoy_pos": 3.4, "deltaPos": 0.5, "judgement": "曝光增加但CTR差"}
                ],
                "1004": [
                    {"query": "親膚濕紙巾 敏弱肌 適用 測試", "clicks": 1842, "yoy_clicks": 1610, "deltaClicks": 232, "impressions": 21104, "yoy_impr": 18900, "deltaImpr": 2204, "ctr": 0.0873, "yoy_ctr": 0.0851, "deltaCtr": 0.0022, "position": 6.1, "yoy_pos": 6.5, "deltaPos": -0.4, "judgement": "成長中字詞"},
                    {"query": "母嬰濕紙巾 推薦 純水 無香精", "clicks": 1250, "yoy_clicks": 1480, "deltaClicks": -230, "impressions": 15200, "yoy_impr": 14000, "deltaImpr": 1200, "ctr": 0.0822, "yoy_ctr": 0.1057, "deltaCtr": -0.0235, "position": 5.4, "yoy_pos": 4.9, "deltaPos": 0.5, "judgement": "曝光增加但CTR差"},
                    {"query": "新生兒 濕紙巾 抽數 優惠 特價", "clicks": 810, "yoy_clicks": 1050, "deltaClicks": -240, "impressions": 11500, "yoy_impr": 12800, "deltaImpr": -1300, "ctr": 0.0704, "yoy_ctr": 0.0820, "deltaCtr": -0.0116, "position": 7.2, "yoy_pos": 6.3, "deltaPos": 0.9, "judgement": "核心字排名下滑"}
                ],
                "1005": [
                    {"query": "無穀貓糧 主食罐 高蛋白 升級", "clicks": 2150, "yoy_clicks": 1890, "deltaClicks": 260, "impressions": 26500, "yoy_impr": 23000, "deltaImpr": 3500, "ctr": 0.0811, "yoy_ctr": 0.0821, "deltaCtr": -0.0010, "position": 2.5, "yoy_pos": 2.8, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "貓咪 主食罐 幼貓 成貓 推薦", "clicks": 1380, "yoy_clicks": 1620, "deltaClicks": -240, "impressions": 18200, "yoy_impr": 16500, "deltaImpr": 1700, "ctr": 0.0758, "yoy_ctr": 0.0981, "deltaCtr": -0.0223, "position": 3.4, "yoy_pos": 3.0, "deltaPos": 0.4, "judgement": "曝光增加但CTR差"}
                ],
                "2001": [
                    {"query": "換季 肌膚 抗乾敏 攻略 精華液", "clicks": 1680, "yoy_clicks": 1950, "deltaClicks": -270, "impressions": 12400, "yoy_impr": 11000, "deltaImpr": 1400, "ctr": 0.1355, "yoy_ctr": 0.1772, "deltaCtr": -0.0417, "position": 2.3, "yoy_pos": 2.1, "deltaPos": 0.2, "judgement": "曝光增加但CTR差"},
                    {"query": "抗老修護霜 保濕精華液 搭配", "clicks": 1240, "yoy_clicks": 980, "deltaClicks": 260, "impressions": 9800, "yoy_impr": 8200, "deltaImpr": 1600, "ctr": 0.1265, "yoy_ctr": 0.1195, "deltaCtr": 0.0070, "position": 2.8, "yoy_pos": 3.2, "deltaPos": -0.4, "judgement": "成長中字詞"}
                ],
                "2002": [
                    {"query": "冷萃黑咖啡 辦公室 上班族 提神", "clicks": 1420, "yoy_clicks": 1200, "deltaClicks": 220, "impressions": 11800, "yoy_impr": 10200, "deltaImpr": 1600, "ctr": 0.1203, "yoy_ctr": 0.1176, "deltaCtr": 0.0027, "position": 2.1, "yoy_pos": 2.4, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "低卡零食 熱量控制 搭配 技巧", "clicks": 890, "yoy_clicks": 1150, "deltaClicks": -260, "impressions": 8500, "yoy_impr": 9200, "deltaImpr": -700, "ctr": 0.1047, "yoy_ctr": 0.1250, "deltaCtr": -0.0203, "position": 3.8, "yoy_pos": 3.2, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "2003": [
                    {"query": "居家大掃除 除霉清潔劑 步驟", "clicks": 1520, "yoy_clicks": 1320, "deltaClicks": 200, "impressions": 13500, "yoy_impr": 11800, "deltaImpr": 1700, "ctr": 0.1125, "yoy_ctr": 0.1118, "deltaCtr": 0.0007, "position": 2.6, "yoy_pos": 2.9, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "地板除菌清潔劑 浴室 去霉 推薦", "clicks": 940, "yoy_clicks": 1210, "deltaClicks": -270, "impressions": 9200, "yoy_impr": 10100, "deltaImpr": -900, "ctr": 0.1021, "yoy_ctr": 0.1198, "deltaCtr": -0.0177, "position": 4.1, "yoy_pos": 3.5, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "2004": [
                    {"query": "消化道 保健指南 專利益生菌", "clicks": 1780, "yoy_clicks": 1520, "deltaClicks": 260, "impressions": 14500, "yoy_impr": 12800, "deltaImpr": 1700, "ctr": 0.1227, "yoy_ctr": 0.1187, "deltaCtr": 0.0040, "position": 1.9, "yoy_pos": 2.3, "deltaPos": -0.4, "judgement": "成長中字詞"},
                    {"query": "綜合維他命 挑選 關鍵 益生菌", "clicks": 1050, "yoy_clicks": 1320, "deltaClicks": -270, "impressions": 10800, "yoy_impr": 11500, "deltaImpr": -700, "ctr": 0.0972, "yoy_ctr": 0.1147, "deltaCtr": -0.0175, "position": 3.5, "yoy_pos": 2.9, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "2005": [
                    {"query": "毛孩 關節保養 高肉糧狗糧 餵食", "clicks": 1620, "yoy_clicks": 1410, "deltaClicks": 210, "impressions": 13800, "yoy_impr": 12200, "deltaImpr": 1600, "ctr": 0.1173, "yoy_ctr": 0.1155, "deltaCtr": 0.0018, "position": 2.2, "yoy_pos": 2.5, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "寵物關節保健粉 狗狗 保養粉 評價", "clicks": 910, "yoy_clicks": 1180, "deltaClicks": -270, "impressions": 8900, "yoy_impr": 9800, "deltaImpr": -900, "ctr": 0.1022, "yoy_ctr": 0.1204, "deltaCtr": -0.0182, "position": 3.9, "yoy_pos": 3.3, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "3001": [
                    {"query": "夏日 油頭救星 控油洗髮精 實測", "clicks": 1450, "yoy_clicks": 1280, "deltaClicks": 170, "impressions": 11200, "yoy_impr": 9900, "deltaImpr": 1300, "ctr": 0.1294, "yoy_ctr": 0.1292, "deltaCtr": 0.0002, "position": 2.1, "yoy_pos": 2.4, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "頭皮蓬鬆感 體驗 控油洗髮精 心得", "clicks": 860, "yoy_clicks": 1120, "deltaClicks": -260, "impressions": 8200, "yoy_impr": 9100, "deltaImpr": -900, "ctr": 0.1048, "yoy_ctr": 0.1230, "deltaCtr": -0.0182, "position": 3.7, "yoy_pos": 3.1, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "3002": [
                    {"query": "低粉塵 豆腐貓砂 除臭力 結塊 報告", "clicks": 1890, "yoy_clicks": 1620, "deltaClicks": 270, "impressions": 15600, "yoy_impr": 13800, "deltaImpr": 1800, "ctr": 0.1211, "yoy_ctr": 0.1173, "deltaCtr": 0.0038, "position": 1.9, "yoy_pos": 2.2, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "豆腐貓砂 快速結塊 評價 開箱 試用", "clicks": 1120, "yoy_clicks": 1380, "deltaClicks": -260, "impressions": 10500, "yoy_impr": 11400, "deltaImpr": -900, "ctr": 0.1066, "yoy_ctr": 0.1210, "deltaCtr": -0.0144, "position": 3.2, "yoy_pos": 2.7, "deltaPos": 0.5, "judgement": "核心字排名下滑"}
                ],
                "3003": [
                    {"query": "植萃洗碗精 溫和不傷手 油膩 重油鍋", "clicks": 1560, "yoy_clicks": 1340, "deltaClicks": 220, "impressions": 12800, "yoy_impr": 11200, "deltaImpr": 1600, "ctr": 0.1218, "yoy_ctr": 0.1196, "deltaCtr": 0.0022, "position": 2.4, "yoy_pos": 2.7, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "不傷手 洗碗精 植萃 界面活性劑 評測", "clicks": 920, "yoy_clicks": 1180, "deltaClicks": -260, "impressions": 8900, "yoy_impr": 9800, "deltaImpr": -900, "ctr": 0.1033, "yoy_ctr": 0.1204, "deltaCtr": -0.0171, "position": 3.8, "yoy_pos": 3.2, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "3004": [
                    {"query": "高純度 膠原蛋白飲 30天 膚況 紀錄", "clicks": 1650, "yoy_clicks": 1420, "deltaClicks": 230, "impressions": 13200, "yoy_impr": 11600, "deltaImpr": 1600, "ctr": 0.1250, "yoy_ctr": 0.1224, "deltaCtr": 0.0026, "position": 2.2, "yoy_pos": 2.5, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "膠原蛋白飲 彈潤 腥味 口感 心得", "clicks": 980, "yoy_clicks": 1240, "deltaClicks": -260, "impressions": 9400, "yoy_impr": 10300, "deltaImpr": -900, "ctr": 0.1042, "yoy_ctr": 0.1203, "deltaCtr": -0.0161, "position": 3.9, "yoy_pos": 3.3, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "4001": [
                    {"query": "線上保養體驗 防曬乳液 水感質地", "clicks": 1720, "yoy_clicks": 1480, "deltaClicks": 240, "impressions": 13900, "yoy_impr": 12100, "deltaImpr": 1800, "ctr": 0.1237, "yoy_ctr": 0.1223, "deltaCtr": 0.0014, "position": 2.3, "yoy_pos": 2.6, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "物理防曬 防護力 清爽不黏膩 解析", "clicks": 1020, "yoy_clicks": 1280, "deltaClicks": -260, "impressions": 9800, "yoy_impr": 10700, "deltaImpr": -900, "ctr": 0.1040, "yoy_ctr": 0.1196, "deltaCtr": -0.0156, "position": 3.6, "yoy_pos": 3.1, "deltaPos": 0.5, "judgement": "核心字排名下滑"}
                ],
                "4002": [
                    {"query": "低卡健康飲食 無糖氣泡水 直播 特調", "clicks": 1580, "yoy_clicks": 1350, "deltaClicks": 230, "impressions": 12600, "yoy_impr": 11000, "deltaImpr": 1600, "ctr": 0.1253, "yoy_ctr": 0.1227, "deltaCtr": 0.0026, "position": 2.5, "yoy_pos": 2.8, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "無糖氣泡水 食譜 教學 搭配 特調", "clicks": 910, "yoy_clicks": 1170, "deltaClicks": -260, "impressions": 8700, "yoy_impr": 9600, "deltaImpr": -900, "ctr": 0.1045, "yoy_ctr": 0.1218, "deltaCtr": -0.0173, "position": 4.0, "yoy_pos": 3.4, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "4003": [
                    {"query": "綠色家居 清潔直播 植萃洗碗精 無毒", "clicks": 1490, "yoy_clicks": 1280, "deltaClicks": 210, "impressions": 11900, "yoy_impr": 10500, "deltaImpr": 1400, "ctr": 0.1252, "yoy_ctr": 0.1219, "deltaCtr": 0.0033, "position": 2.6, "yoy_pos": 2.9, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "4004": [
                    {"query": "新手貓奴 講座 無穀貓糧 換糧 指引", "clicks": 1820, "yoy_clicks": 1560, "deltaClicks": 260, "impressions": 14800, "yoy_impr": 12900, "deltaImpr": 1900, "ctr": 0.1229, "yoy_ctr": 0.1209, "deltaCtr": 0.0020, "position": 2.1, "yoy_pos": 2.4, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "edm101": [
                    {"query": "保濕精華液 體驗組 限時 免費領取", "clicks": 1650, "yoy_clicks": 1420, "deltaClicks": 230, "impressions": 13100, "yoy_impr": 11500, "deltaImpr": 1600, "ctr": 0.1260, "yoy_ctr": 0.1235, "deltaCtr": 0.0025, "position": 1.9, "yoy_pos": 2.2, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "edm102": [
                    {"query": "無糖氣泡水 夏季 箱購 88折 免運", "clicks": 1920, "yoy_clicks": 1680, "deltaClicks": 240, "impressions": 15400, "yoy_impr": 13500, "deltaImpr": 1900, "ctr": 0.1247, "yoy_ctr": 0.1244, "deltaCtr": 0.0003, "position": 2.1, "yoy_pos": 2.4, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "edm103": [
                    {"query": "狂歡寵物節 豆腐貓砂 買二送一 促銷", "clicks": 2100, "yoy_clicks": 1820, "deltaClicks": 280, "impressions": 16800, "yoy_impr": 14600, "deltaImpr": 2200, "ctr": 0.1250, "yoy_ctr": 0.1247, "deltaCtr": 0.0003, "position": 1.8, "yoy_pos": 2.1, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "edm104": [
                    {"query": "保健週年慶 專利益生菌 買大送小 搶購", "clicks": 1850, "yoy_clicks": 1610, "deltaClicks": 240, "impressions": 14600, "yoy_impr": 12800, "deltaImpr": 1800, "ctr": 0.1267, "yoy_ctr": 0.1258, "deltaCtr": 0.0009, "position": 2.0, "yoy_pos": 2.3, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "501": [
                    {"query": "玻尿酸 神經醯胺 保濕技術 解析 專題", "clicks": 1520, "yoy_clicks": 1320, "deltaClicks": 200, "impressions": 12100, "yoy_impr": 10600, "deltaImpr": 1500, "ctr": 0.1256, "yoy_ctr": 0.1245, "deltaCtr": 0.0011, "position": 2.2, "yoy_pos": 2.5, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "502": [
                    {"query": "植物萃取 界面活性劑 去油 溫和配方", "clicks": 1430, "yoy_clicks": 1240, "deltaClicks": 190, "impressions": 11500, "yoy_impr": 10100, "deltaImpr": 1400, "ctr": 0.1243, "yoy_ctr": 0.1228, "deltaCtr": 0.0015, "position": 2.4, "yoy_pos": 2.7, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "503": [
                    {"query": "專利益生菌 三層包埋技術 定殖率 實驗", "clicks": 1610, "yoy_clicks": 1400, "deltaClicks": 210, "impressions": 12900, "yoy_impr": 11200, "deltaImpr": 1700, "ctr": 0.1248, "yoy_ctr": 0.1250, "deltaCtr": -0.0002, "position": 2.1, "yoy_pos": 2.4, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "601": [
                    {"query": "物理性防曬 化學性防曬 清爽度 成份", "clicks": 1740, "yoy_clicks": 1510, "deltaClicks": 230, "impressions": 13800, "yoy_impr": 12000, "deltaImpr": 1800, "ctr": 0.1261, "yoy_ctr": 0.1258, "deltaCtr": 0.0003, "position": 2.0, "yoy_pos": 2.3, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "602": [
                    {"query": "冷萃工藝 咖啡因 咖啡多酚 風味 保留", "clicks": 1590, "yoy_clicks": 1380, "deltaClicks": 210, "impressions": 12700, "yoy_impr": 11100, "deltaImpr": 1600, "ctr": 0.1252, "yoy_ctr": 0.1243, "deltaCtr": 0.0009, "position": 2.3, "yoy_pos": 2.6, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ]
            }

            if target_id and str(target_id) in page_custom_map:
                return {"ok": True, "queries": page_custom_map[str(target_id)]}

            # 備援：依網頁標題動態產生關鍵字
            page_map = load_page_map()
            pinfo = resolve_page_by_params(page_url or target_id or "", page_map)
            page_title = pinfo.get("name") or "FMCG 精選商品"
            prod_name = pinfo.get("product_detail") or pinfo.get("product") or "健康保養"

            return {
                "ok": True,
                "queries": [
                    {
                        "query": f"2026 {prod_name} 推薦 評價",
                        "clicks": 1850, "yoy_clicks": 1620, "deltaClicks": 230,
                        "impressions": 14500, "yoy_impr": 12800, "deltaImpr": 1700,
                        "ctr": 0.1275, "yoy_ctr": 0.1265, "deltaCtr": 0.0010,
                        "position": 2.1, "yoy_pos": 2.4, "deltaPos": -0.3,
                        "judgement": "成長中字詞"
                    },
                    {
                        "query": f"{page_title[:16]} 規格評比",
                        "clicks": 1120, "yoy_clicks": 1380, "deltaClicks": -260,
                        "impressions": 9800, "yoy_impr": 10500, "deltaImpr": -700,
                        "ctr": 0.1142, "yoy_ctr": 0.1314, "deltaCtr": -0.0172,
                        "position": 3.4, "yoy_pos": 2.9, "deltaPos": 0.5,
                        "judgement": "核心字排名下滑"
                    }
                ]
            }

        if type == "query_pages":
            return {
                "ok": True,
                "pages": [
                    {
                        "url": "https://demo.example.com/news/toDetail?news_id=1001",
                        "id": "1001",
                        "title": "2026 夏季新品上市：控油洗髮精強效持香與蓬鬆控油成分公開",
                        "category": "news_id",
                        "impressions": 15800,
                        "clicks": 2350,
                        "ctr": 0.1487,
                        "position": 1.8
                    },
                    {
                        "url": "https://demo.example.com/article/toDetail?article_id=2001",
                        "id": "2001",
                        "title": "換季肌膚抗乾敏攻略：保濕精華液與抗老修護霜完美搭配使用指南",
                        "category": "article_id",
                        "impressions": 12400,
                        "clicks": 1680,
                        "ctr": 0.1355,
                        "position": 2.3
                    }
                ]
            }

        if type == "breakdown":
            return {
                "ok": True,
                "breakdown": [
                    {"group": "Google 搜尋", "source": "google / organic", "views": 2350},
                    {"group": "Meta 廣告", "source": "facebook / cpc", "views": 1280},
                    {"group": "IG 網紅引流", "source": "instagram / referral", "views": 950},
                    {"group": "LINE 官方帳號", "source": "line / oa", "views": 820},
                    {"group": "直接流量", "source": "(direct) / (none)", "views": 610},
                    {"group": "EDM 電子報", "source": "edm / newsletter", "views": 430}
                ]
            }

        if type == "export_data":
            k_val = k or "28d"
            drill = (master.get(k_val) if master else None) or {}
            rows = drill.get("rows", [])
            export_data = []
            for r in rows[:200]:
                export_data.append({
                    "product": r.get("product", "個人護理與美妝"),
                    "product_detail": r.get("product_detail", "控油洗髮精"),
                    "category": r.get("category", "news_id"),
                    "pageId": r.get("pageId", "1001"),
                    "pageName": r.get("display_title", "2026 夏季新品上市：控油洗髮精強效持香與蓬鬆控油成分公開"),
                    "sourceGroup": r.get("source_group", "Google 搜尋"),
                    "sourceName": r.get("source_name", "Google 搜尋"),
                    "views": r.get("users", 1000),
                    "views_prev": int(r.get("users", 1000) * 0.9)
                })
            export_gsc = [
                {
                    "category": "news_id",
                    "pageId": "1001",
                    "pageName": "2026 夏季新品上市：控油洗髮精強效持香與蓬鬆控油成分公開",
                    "query": "2026 控油洗髮精 推薦 蓬鬆",
                    "impressions": 15800, "clicks": 2350, "ctr": 0.1487, "position": 1.8,
                    "impressions_prev": 14200, "clicks_prev": 2100, "ctr_prev": 0.1478, "position_prev": 2.1
                }
            ]
            return {"ok": True, "exportData": export_data, "exportGscData": export_gsc}

    if type == "gsc_queries":
        # 直接從 GSC 拉取產品熱門搜尋字
        site_url = get_gsc_site_url()
        if not site_url:
            return {"ok": True, "gscQueries": []}
            
        page_map = load_page_map()
        regexes = build_gsc_regexes_for_product(page_map, product, prod_det)
        if not regexes or regexes[0] == "MATCH_NOTHING_XXX_999":
            return {"ok": True, "gscQueries": []}
            
        creds = _get_creds()
        token = creds.token if creds else None
        if not token:
            return {"ok": True, "gscQueries": []}
            
        k_val = k or "28d"
        range_info = date_range_for_k(k_val)
        gsc_start_date = range_info["startDate"]
        gsc_end_date = range_info["endDate"]
            
        # 查詢 GSC
        def fetch_gsc_queries_regex(reg):
            import requests
            url = f"https://searchconsole.googleapis.com/webmasters/v3/sites/{urllib.parse.quote(site_url, safe='')}/searchAnalytics/query"
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            
            body = {
                "startDate": gsc_start_date,
                "endDate": gsc_end_date,
                "dimensions": ["query"],
                "dimensionFilterGroups": [{"filters": [{"dimension": "page", "operator": "includingRegex", "expression": reg}]}],
                "rowLimit": 1000
            }
            for attempt in range(3):
                try:
                    import time
                    res = requests.post(url, headers=headers, json=body, timeout=60)
                    if res.status_code == 200:
                        return res.json().get("rows", [])
                    elif res.status_code == 429:
                        time.sleep(2)
                        continue
                    else:
                        break
                except:
                    import time
                    time.sleep(2)
            return []
            
        query_map = {}
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(fetch_gsc_queries_regex, r) for r in regexes]
            for future in concurrent.futures.as_completed(futures):
                rows = future.result()
                for row in rows:
                    if not row.get('keys'): continue
                    q = row['keys'][0]
                    if q not in query_map:
                        query_map[q] = {
                            "query": q,
                            "impressions": 0, "clicks": 0, "ctr": 0.0, "position": 0.0,
                            "posSum": 0.0, "clicksForPos": 0
                        }
                    item = query_map[q]
                    clicks = int(row.get("clicks", 0))
                    impr = int(row.get("impressions", 0))
                    pos = float(row.get("position", 0.0))
                    
                    item["clicks"] += clicks
                    item["impressions"] += impr
                    if pos > 0 and clicks > 0:
                        item["posSum"] += pos * clicks
                        item["clicksForPos"] += clicks
                        
        gsc_queries = []
        for v in query_map.values():
            if v["clicksForPos"] > 0:
                v["position"] = v["posSum"] / v["clicksForPos"]
            v["ctr"] = v["clicks"] / v["impressions"] if v["impressions"] > 0 else 0.0
            gsc_queries.append({
                "query": v["query"],
                "gscImpressions": v["impressions"],
                "gscClicks": v["clicks"],
                "gscCtr": v["ctr"],
                "gscPosition": v["position"]
            })
            
        # 依曝光數排序並取前 N 名
        gsc_queries.sort(key=lambda x: x["gscImpressions"], reverse=True)
        return {"ok": True, "gscQueries": gsc_queries[:limit]}

    if type == "page_queries":
        if is_mock_mode():
            target_id = id
            if not target_id and page_url:
                import re
                m = re.search(r'(?:id|_id|_no)=([a-zA-Z0-9_-]+)', page_url)
                if m:
                    target_id = m.group(1)
            
            page_custom_map = {
                "1001": [
                    {"query": "2026 控油洗髮精 推薦 蓬鬆", "clicks": 2350, "yoy_clicks": 2100, "deltaClicks": 250, "impressions": 15800, "yoy_impr": 14200, "deltaImpr": 1600, "ctr": 0.1487, "yoy_ctr": 0.1478, "deltaCtr": 0.0009, "position": 1.8, "yoy_pos": 2.1, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "洗髮精 控油 持香 推薦 品牌", "clicks": 1480, "yoy_clicks": 1620, "deltaClicks": -140, "impressions": 11200, "yoy_impr": 10500, "deltaImpr": 700, "ctr": 0.1321, "yoy_ctr": 0.1542, "deltaCtr": -0.0221, "position": 2.4, "yoy_pos": 2.2, "deltaPos": 0.2, "judgement": "曝光增加但CTR差"},
                    {"query": "夏季 油頭 蓬鬆 洗髮精 評比", "clicks": 920, "yoy_clicks": 1180, "deltaClicks": -260, "impressions": 8900, "yoy_impr": 9400, "deltaImpr": -500, "ctr": 0.1033, "yoy_ctr": 0.1255, "deltaCtr": -0.0222, "position": 3.2, "yoy_pos": 2.6, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "1002": [
                    {"query": "115年 有機燕麥奶 推薦 品牌", "clicks": 2474, "yoy_clicks": 2150, "deltaClicks": 324, "impressions": 44233, "yoy_impr": 39000, "deltaImpr": 5233, "ctr": 0.0559, "yoy_ctr": 0.0551, "deltaCtr": 0.0008, "position": 3.7, "yoy_pos": 4.1, "deltaPos": -0.4, "judgement": "成長中字詞"},
                    {"query": "無糖氣泡水 飲品 趨勢 箱購", "clicks": 1820, "yoy_clicks": 2100, "deltaClicks": -280, "impressions": 28500, "yoy_impr": 26000, "deltaImpr": 2500, "ctr": 0.0638, "yoy_ctr": 0.0807, "deltaCtr": -0.0169, "position": 4.1, "yoy_pos": 3.8, "deltaPos": 0.3, "judgement": "曝光增加但CTR差"},
                    {"query": "健康飲食 燕麥奶 氣泡水 特調", "clicks": 1120, "yoy_clicks": 1450, "deltaClicks": -330, "impressions": 19200, "yoy_impr": 21000, "deltaImpr": -1800, "ctr": 0.0583, "yoy_ctr": 0.0690, "deltaCtr": -0.0107, "position": 4.8, "yoy_pos": 4.2, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "1003": [
                    {"query": "濃縮洗衣精 植萃 防蟎 抗菌", "clicks": 1950, "yoy_clicks": 1720, "deltaClicks": 230, "impressions": 23400, "yoy_impr": 21000, "deltaImpr": 2400, "ctr": 0.0833, "yoy_ctr": 0.0819, "deltaCtr": 0.0014, "position": 2.8, "yoy_pos": 3.1, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "環保 清潔劑 洗衣精 配方 認證", "clicks": 1120, "yoy_clicks": 1380, "deltaClicks": -260, "impressions": 16800, "yoy_impr": 15200, "deltaImpr": 1600, "ctr": 0.0666, "yoy_ctr": 0.0907, "deltaCtr": -0.0241, "position": 3.9, "yoy_pos": 3.4, "deltaPos": 0.5, "judgement": "曝光增加但CTR差"}
                ],
                "1004": [
                    {"query": "親膚濕紙巾 敏弱肌 適用 測試", "clicks": 1842, "yoy_clicks": 1610, "deltaClicks": 232, "impressions": 21104, "yoy_impr": 18900, "deltaImpr": 2204, "ctr": 0.0873, "yoy_ctr": 0.0851, "deltaCtr": 0.0022, "position": 6.1, "yoy_pos": 6.5, "deltaPos": -0.4, "judgement": "成長中字詞"},
                    {"query": "母嬰濕紙巾 推薦 純水 無香精", "clicks": 1250, "yoy_clicks": 1480, "deltaClicks": -230, "impressions": 15200, "yoy_impr": 14000, "deltaImpr": 1200, "ctr": 0.0822, "yoy_ctr": 0.1057, "deltaCtr": -0.0235, "position": 5.4, "yoy_pos": 4.9, "deltaPos": 0.5, "judgement": "曝光增加但CTR差"},
                    {"query": "新生兒 濕紙巾 抽數 優惠 特價", "clicks": 810, "yoy_clicks": 1050, "deltaClicks": -240, "impressions": 11500, "yoy_impr": 12800, "deltaImpr": -1300, "ctr": 0.0704, "yoy_ctr": 0.0820, "deltaCtr": -0.0116, "position": 7.2, "yoy_pos": 6.3, "deltaPos": 0.9, "judgement": "核心字排名下滑"}
                ],
                "1005": [
                    {"query": "無穀貓糧 主食罐 高蛋白 升級", "clicks": 2150, "yoy_clicks": 1890, "deltaClicks": 260, "impressions": 26500, "yoy_impr": 23000, "deltaImpr": 3500, "ctr": 0.0811, "yoy_ctr": 0.0821, "deltaCtr": -0.0010, "position": 2.5, "yoy_pos": 2.8, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "貓咪 主食罐 幼貓 成貓 推薦", "clicks": 1380, "yoy_clicks": 1620, "deltaClicks": -240, "impressions": 18200, "yoy_impr": 16500, "deltaImpr": 1700, "ctr": 0.0758, "yoy_ctr": 0.0981, "deltaCtr": -0.0223, "position": 3.4, "yoy_pos": 3.0, "deltaPos": 0.4, "judgement": "曝光增加但CTR差"}
                ],
                "2001": [
                    {"query": "換季 肌膚 抗乾敏 攻略 精華液", "clicks": 1680, "yoy_clicks": 1950, "deltaClicks": -270, "impressions": 12400, "yoy_impr": 11000, "deltaImpr": 1400, "ctr": 0.1355, "yoy_ctr": 0.1772, "deltaCtr": -0.0417, "position": 2.3, "yoy_pos": 2.1, "deltaPos": 0.2, "judgement": "曝光增加但CTR差"},
                    {"query": "抗老修護霜 保濕精華液 搭配", "clicks": 1240, "yoy_clicks": 980, "deltaClicks": 260, "impressions": 9800, "yoy_impr": 8200, "deltaImpr": 1600, "ctr": 0.1265, "yoy_ctr": 0.1195, "deltaCtr": 0.0070, "position": 2.8, "yoy_pos": 3.2, "deltaPos": -0.4, "judgement": "成長中字詞"}
                ],
                "2002": [
                    {"query": "冷萃黑咖啡 辦公室 上班族 提神", "clicks": 1420, "yoy_clicks": 1200, "deltaClicks": 220, "impressions": 11800, "yoy_impr": 10200, "deltaImpr": 1600, "ctr": 0.1203, "yoy_ctr": 0.1176, "deltaCtr": 0.0027, "position": 2.1, "yoy_pos": 2.4, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "低卡零食 熱量控制 搭配 技巧", "clicks": 890, "yoy_clicks": 1150, "deltaClicks": -260, "impressions": 8500, "yoy_impr": 9200, "deltaImpr": -700, "ctr": 0.1047, "yoy_ctr": 0.1250, "deltaCtr": -0.0203, "position": 3.8, "yoy_pos": 3.2, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "2003": [
                    {"query": "居家大掃除 除霉清潔劑 步驟", "clicks": 1520, "yoy_clicks": 1320, "deltaClicks": 200, "impressions": 13500, "yoy_impr": 11800, "deltaImpr": 1700, "ctr": 0.1125, "yoy_ctr": 0.1118, "deltaCtr": 0.0007, "position": 2.6, "yoy_pos": 2.9, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "地板除菌清潔劑 浴室 去霉 推薦", "clicks": 940, "yoy_clicks": 1210, "deltaClicks": -270, "impressions": 9200, "yoy_impr": 10100, "deltaImpr": -900, "ctr": 0.1021, "yoy_ctr": 0.1198, "deltaCtr": -0.0177, "position": 4.1, "yoy_pos": 3.5, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "2004": [
                    {"query": "消化道 保健指南 專利益生菌", "clicks": 1780, "yoy_clicks": 1520, "deltaClicks": 260, "impressions": 14500, "yoy_impr": 12800, "deltaImpr": 1700, "ctr": 0.1227, "yoy_ctr": 0.1187, "deltaCtr": 0.0040, "position": 1.9, "yoy_pos": 2.3, "deltaPos": -0.4, "judgement": "成長中字詞"},
                    {"query": "綜合維他命 挑選 關鍵 益生菌", "clicks": 1050, "yoy_clicks": 1320, "deltaClicks": -270, "impressions": 10800, "yoy_impr": 11500, "deltaImpr": -700, "ctr": 0.0972, "yoy_ctr": 0.1147, "deltaCtr": -0.0175, "position": 3.5, "yoy_pos": 2.9, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "2005": [
                    {"query": "毛孩 關節保養 高肉糧狗糧 餵食", "clicks": 1620, "yoy_clicks": 1410, "deltaClicks": 210, "impressions": 13800, "yoy_impr": 12200, "deltaImpr": 1600, "ctr": 0.1173, "yoy_ctr": 0.1155, "deltaCtr": 0.0018, "position": 2.2, "yoy_pos": 2.5, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "寵物關節保健粉 狗狗 保養粉 評價", "clicks": 910, "yoy_clicks": 1180, "deltaClicks": -270, "impressions": 8900, "yoy_impr": 9800, "deltaImpr": -900, "ctr": 0.1022, "yoy_ctr": 0.1204, "deltaCtr": -0.0182, "position": 3.9, "yoy_pos": 3.3, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "3001": [
                    {"query": "夏日 油頭救星 控油洗髮精 實測", "clicks": 1450, "yoy_clicks": 1280, "deltaClicks": 170, "impressions": 11200, "yoy_impr": 9900, "deltaImpr": 1300, "ctr": 0.1294, "yoy_ctr": 0.1292, "deltaCtr": 0.0002, "position": 2.1, "yoy_pos": 2.4, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "頭皮蓬鬆感 體驗 控油洗髮精 心得", "clicks": 860, "yoy_clicks": 1120, "deltaClicks": -260, "impressions": 8200, "yoy_impr": 9100, "deltaImpr": -900, "ctr": 0.1048, "yoy_ctr": 0.1230, "deltaCtr": -0.0182, "position": 3.7, "yoy_pos": 3.1, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "3002": [
                    {"query": "低粉塵 豆腐貓砂 除臭力 結塊 報告", "clicks": 1890, "yoy_clicks": 1620, "deltaClicks": 270, "impressions": 15600, "yoy_impr": 13800, "deltaImpr": 1800, "ctr": 0.1211, "yoy_ctr": 0.1173, "deltaCtr": 0.0038, "position": 1.9, "yoy_pos": 2.2, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "豆腐貓砂 快速結塊 評價 開箱 試用", "clicks": 1120, "yoy_clicks": 1380, "deltaClicks": -260, "impressions": 10500, "yoy_impr": 11400, "deltaImpr": -900, "ctr": 0.1066, "yoy_ctr": 0.1210, "deltaCtr": -0.0144, "position": 3.2, "yoy_pos": 2.7, "deltaPos": 0.5, "judgement": "核心字排名下滑"}
                ],
                "3003": [
                    {"query": "植萃洗碗精 溫和不傷手 油膩 重油鍋", "clicks": 1560, "yoy_clicks": 1340, "deltaClicks": 220, "impressions": 12800, "yoy_impr": 11200, "deltaImpr": 1600, "ctr": 0.1218, "yoy_ctr": 0.1196, "deltaCtr": 0.0022, "position": 2.4, "yoy_pos": 2.7, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "不傷手 洗碗精 植萃 界面活性劑 評測", "clicks": 920, "yoy_clicks": 1180, "deltaClicks": -260, "impressions": 8900, "yoy_impr": 9800, "deltaImpr": -900, "ctr": 0.1033, "yoy_ctr": 0.1204, "deltaCtr": -0.0171, "position": 3.8, "yoy_pos": 3.2, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "3004": [
                    {"query": "高純度 膠原蛋白飲 30天 膚況 紀錄", "clicks": 1650, "yoy_clicks": 1420, "deltaClicks": 230, "impressions": 13200, "yoy_impr": 11600, "deltaImpr": 1600, "ctr": 0.1250, "yoy_ctr": 0.1224, "deltaCtr": 0.0026, "position": 2.2, "yoy_pos": 2.5, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "膠原蛋白飲 彈潤 腥味 口感 心得", "clicks": 980, "yoy_clicks": 1240, "deltaClicks": -260, "impressions": 9400, "yoy_impr": 10300, "deltaImpr": -900, "ctr": 0.1042, "yoy_ctr": 0.1203, "deltaCtr": -0.0161, "position": 3.9, "yoy_pos": 3.3, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "4001": [
                    {"query": "線上保養體驗 防曬乳液 水感質地", "clicks": 1720, "yoy_clicks": 1480, "deltaClicks": 240, "impressions": 13900, "yoy_impr": 12100, "deltaImpr": 1800, "ctr": 0.1237, "yoy_ctr": 0.1223, "deltaCtr": 0.0014, "position": 2.3, "yoy_pos": 2.6, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "物理防曬 防護力 清爽不黏膩 解析", "clicks": 1020, "yoy_clicks": 1280, "deltaClicks": -260, "impressions": 9800, "yoy_impr": 10700, "deltaImpr": -900, "ctr": 0.1040, "yoy_ctr": 0.1196, "deltaCtr": -0.0156, "position": 3.6, "yoy_pos": 3.1, "deltaPos": 0.5, "judgement": "核心字排名下滑"}
                ],
                "4002": [
                    {"query": "低卡健康飲食 無糖氣泡水 直播 特調", "clicks": 1580, "yoy_clicks": 1350, "deltaClicks": 230, "impressions": 12600, "yoy_impr": 11000, "deltaImpr": 1600, "ctr": 0.1253, "yoy_ctr": 0.1227, "deltaCtr": 0.0026, "position": 2.5, "yoy_pos": 2.8, "deltaPos": -0.3, "judgement": "成長中字詞"},
                    {"query": "無糖氣泡水 食譜 教學 搭配 特調", "clicks": 910, "yoy_clicks": 1170, "deltaClicks": -260, "impressions": 8700, "yoy_impr": 9600, "deltaImpr": -900, "ctr": 0.1045, "yoy_ctr": 0.1218, "deltaCtr": -0.0173, "position": 4.0, "yoy_pos": 3.4, "deltaPos": 0.6, "judgement": "核心字排名下滑"}
                ],
                "4003": [
                    {"query": "綠色家居 清潔直播 植萃洗碗精 無毒", "clicks": 1490, "yoy_clicks": 1280, "deltaClicks": 210, "impressions": 11900, "yoy_impr": 10500, "deltaImpr": 1400, "ctr": 0.1252, "yoy_ctr": 0.1219, "deltaCtr": 0.0033, "position": 2.6, "yoy_pos": 2.9, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "4004": [
                    {"query": "新手貓奴 講座 無穀貓糧 換糧 指引", "clicks": 1820, "yoy_clicks": 1560, "deltaClicks": 260, "impressions": 14800, "yoy_impr": 12900, "deltaImpr": 1900, "ctr": 0.1229, "yoy_ctr": 0.1209, "deltaCtr": 0.0020, "position": 2.1, "yoy_pos": 2.4, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "edm101": [
                    {"query": "保濕精華液 體驗組 限時 免費領取", "clicks": 1650, "yoy_clicks": 1420, "deltaClicks": 230, "impressions": 13100, "yoy_impr": 11500, "deltaImpr": 1600, "ctr": 0.1260, "yoy_ctr": 0.1235, "deltaCtr": 0.0025, "position": 1.9, "yoy_pos": 2.2, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "edm102": [
                    {"query": "無糖氣泡水 夏季 箱購 88折 免運", "clicks": 1920, "yoy_clicks": 1680, "deltaClicks": 240, "impressions": 15400, "yoy_impr": 13500, "deltaImpr": 1900, "ctr": 0.1247, "yoy_ctr": 0.1244, "deltaCtr": 0.0003, "position": 2.1, "yoy_pos": 2.4, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "edm103": [
                    {"query": "狂歡寵物節 豆腐貓砂 買二送一 促銷", "clicks": 2100, "yoy_clicks": 1820, "deltaClicks": 280, "impressions": 16800, "yoy_impr": 14600, "deltaImpr": 2200, "ctr": 0.1250, "yoy_ctr": 0.1247, "deltaCtr": 0.0003, "position": 1.8, "yoy_pos": 2.1, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "edm104": [
                    {"query": "保健週年慶 專利益生菌 買大送小 搶購", "clicks": 1850, "yoy_clicks": 1610, "deltaClicks": 240, "impressions": 14600, "yoy_impr": 12800, "deltaImpr": 1800, "ctr": 0.1267, "yoy_ctr": 0.1258, "deltaCtr": 0.0009, "position": 2.0, "yoy_pos": 2.3, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "501": [
                    {"query": "玻尿酸 神經醯胺 保濕技術 解析 專題", "clicks": 1520, "yoy_clicks": 1320, "deltaClicks": 200, "impressions": 12100, "yoy_impr": 10600, "deltaImpr": 1500, "ctr": 0.1256, "yoy_ctr": 0.1245, "deltaCtr": 0.0011, "position": 2.2, "yoy_pos": 2.5, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "502": [
                    {"query": "植物萃取 界面活性劑 去油 溫和配方", "clicks": 1430, "yoy_clicks": 1240, "deltaClicks": 190, "impressions": 11500, "yoy_impr": 10100, "deltaImpr": 1400, "ctr": 0.1243, "yoy_ctr": 0.1228, "deltaCtr": 0.0015, "position": 2.4, "yoy_pos": 2.7, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "503": [
                    {"query": "專利益生菌 三層包埋技術 定殖率 實驗", "clicks": 1610, "yoy_clicks": 1400, "deltaClicks": 210, "impressions": 12900, "yoy_impr": 11200, "deltaImpr": 1700, "ctr": 0.1248, "yoy_ctr": 0.1250, "deltaCtr": -0.0002, "position": 2.1, "yoy_pos": 2.4, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "601": [
                    {"query": "物理性防曬 化學性防曬 清爽度 成份", "clicks": 1740, "yoy_clicks": 1510, "deltaClicks": 230, "impressions": 13800, "yoy_impr": 12000, "deltaImpr": 1800, "ctr": 0.1261, "yoy_ctr": 0.1258, "deltaCtr": 0.0003, "position": 2.0, "yoy_pos": 2.3, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ],
                "602": [
                    {"query": "冷萃工藝 咖啡因 咖啡多酚 風味 保留", "clicks": 1590, "yoy_clicks": 1380, "deltaClicks": 210, "impressions": 12700, "yoy_impr": 11100, "deltaImpr": 1600, "ctr": 0.1252, "yoy_ctr": 0.1243, "deltaCtr": 0.0009, "position": 2.3, "yoy_pos": 2.6, "deltaPos": -0.3, "judgement": "成長中字詞"}
                ]
            }

            if target_id and str(target_id) in page_custom_map:
                return {"ok": True, "queries": page_custom_map[str(target_id)]}

            # 備援：依網頁標題動態產生關鍵字
            page_map = load_page_map()
            pinfo = resolve_page_by_params(page_url or target_id or "", page_map)
            page_title = pinfo.get("name") or "FMCG 精選商品"
            prod_name = pinfo.get("product_detail") or pinfo.get("product") or "健康保養"

            return {
                "ok": True,
                "queries": [
                    {
                        "query": f"2026 {prod_name} 推薦 評價",
                        "clicks": 1850, "yoy_clicks": 1620, "deltaClicks": 230,
                        "impressions": 14500, "yoy_impr": 12800, "deltaImpr": 1700,
                        "ctr": 0.1275, "yoy_ctr": 0.1265, "deltaCtr": 0.0010,
                        "position": 2.1, "yoy_pos": 2.4, "deltaPos": -0.3,
                        "judgement": "成長中字詞"
                    },
                    {
                        "query": f"{page_title[:16]} 規格評比",
                        "clicks": 1120, "yoy_clicks": 1380, "deltaClicks": -260,
                        "impressions": 9800, "yoy_impr": 10500, "deltaImpr": -700,
                        "ctr": 0.1142, "yoy_ctr": 0.1314, "deltaCtr": -0.0172,
                        "position": 3.4, "yoy_pos": 2.9, "deltaPos": 0.5,
                        "judgement": "核心字排名下滑"
                    }
                ]
            }
        # 使用 Search Console API 直接查詢指定頁面的搜尋關鍵字 (包含 YoY 比較與原因診斷)
        site_url = get_gsc_site_url()
        if not site_url:
            return {"ok": False, "error": "Search Console 未連接或無已驗證網站"}
        
        if k == "last_month":
            if ym:
                y, m = int(ym[:4]), int(ym[4:6])
            else:
                now_tp = get_now_taipei()
                first_of_this_month = now_tp.replace(day=1)
                last_month_dt = first_of_this_month - timedelta(days=1)
                y, m = last_month_dt.year, last_month_dt.month
            first = datetime(y, m, 1)
            if m == 12:
                last = datetime(y + 1, 1, 1) - timedelta(days=1)
            else:
                last = datetime(y, m + 1, 1) - timedelta(days=1)
            start_date = first.strftime("%Y-%m-%d")
            end_date = last.strftime("%Y-%m-%d")
        else:
            k_val = k or "28d"
            range_info = date_range_for_k(k_val)
            start_date = range_info["startDate"]
            end_date = range_info["endDate"]
        
        cmp = compute_prev_and_yoy_ranges(start_date, end_date)
        yoy_start = cmp["yoy"]["start"]
        yoy_end = cmp["yoy"]["end"]
        
        # 根據 page_url 或 id 構建 page filter
        page_filter = ""
        operator_val = "contains"
        
        if page_url and page_url.strip() and category != "edm":
            page_filter = clean_gsc_page_url(page_url.strip())
            operator_val = "contains"
        elif id:
            cat_param_map = {
                "news_id": f"news_id={id}",
                "news": f"news_id={id}",
                "article_id": f"article_id={id}",
                "article": f"article_id={id}",
                "comment_id": f"comment_id={id}",
                "comment": f"comment_id={id}",
                "lecture_id": f"lecture_id={id}",
                "lecture": f"lecture_id={id}",
                "edm": f"(edm_id=|edm[_-]?){id}([^a-zA-Z0-9]|$)",
                "f_subject_no": f"f_subject_no={id}",
                "subject_no": f"subject_no={id}",
            }
            page_filter = cat_param_map.get(category, id)
            if category == "edm":
                operator_val = "includingRegex"
        
        service = get_gsc_service()
        if not service:
            return {"ok": False, "error": "Search Console API 服務未就緒"}
        
        body_cur = {
            'startDate': start_date, 'endDate': end_date,
            'dimensions': ['query'], 'rowLimit': 50
        }
        body_yoy = {
            'startDate': yoy_start, 'endDate': yoy_end,
            'dimensions': ['query'], 'rowLimit': 50
        }
        if page_filter:
            filt = [{'dimension': 'page', 'operator': operator_val, 'expression': page_filter}]
            body_cur['dimensionFilterGroups'] = [{'filters': filt}]
            body_yoy['dimensionFilterGroups'] = [{'filters': filt}]
        
        try:
            cur_result = service.searchanalytics().query(siteUrl=site_url, body=body_cur).execute()
            cur_rows = cur_result.get('rows', [])
            yoy_result = service.searchanalytics().query(siteUrl=site_url, body=body_yoy).execute()
            yoy_rows = yoy_result.get('rows', [])
        except Exception as e:
            return {"ok": False, "error": f"GSC API 查詢失敗: {str(e)}"}
        
        yoy_map = {}
        for r in yoy_rows:
            q = r['keys'][0]
            yoy_map[q] = {
                "clicks": int(r.get('clicks', 0)),
                "impressions": int(r.get('impressions', 0)),
                "ctr": float(r.get('ctr', 0.0)),
                "position": float(r.get('position', 0.0))
            }
            
        queries = []
        # Merge all queries from cur and yoy
        all_queries = set([r['keys'][0] for r in cur_rows] + list(yoy_map.keys()))
        
        cur_map = {}
        for r in cur_rows:
            q = r['keys'][0]
            cur_map[q] = r
            
        for q in all_queries:
            # 排除非今年度的過期年度字，避免 YoY 大幅下跌的誤判
            import re
            m_ad = re.search(r'(20\d{2})', q)
            if m_ad and m_ad.group(1) != '2026': continue
            m_mg = re.search(r'(11\d{1})', q)
            if m_mg and m_mg.group(1) != '115': continue
            
            r = cur_map.get(q, {})
            c = int(r.get('clicks', 0))
            i = int(r.get('impressions', 0))
            ctr = float(r.get('ctr', 0.0))
            pos = float(r.get('position', 0.0))
            
            y_r = yoy_map.get(q, {"clicks":0, "impressions":0, "ctr":0.0, "position":0.0})
            y_c = y_r["clicks"]
            y_i = y_r["impressions"]
            y_ctr = y_r["ctr"]
            y_pos = y_r["position"]
            
            dc = c - y_c
            di = i - y_i
            d_ctr = ctr - y_ctr
            d_pos = pos - y_pos if (pos > 0 and y_pos > 0) else (pos if y_pos == 0 else -y_pos)
            
            # Query level judgement
            judgement = ""
            if c < y_c and i < y_i and pos > y_pos and pos > 0 and y_pos > 0:
                judgement = "核心字排名下滑"
            elif i > y_i and ctr < y_ctr:
                judgement = "曝光增加但CTR差"
            elif i < y_i and (abs(d_pos) < 2 or pos == 0):
                judgement = "搜尋需求下降"
            elif c < y_c and i >= y_i:
                judgement = "曝光穩定但點擊衰退"
            elif i > y_i and c > y_c:
                judgement = "成長中字詞"
            else:
                judgement = "待觀察"
                
            queries.append({
                "query": q,
                "clicks": c, "yoy_clicks": y_c, "deltaClicks": dc,
                "impressions": i, "yoy_impr": y_i, "deltaImpr": di,
                "ctr": ctr, "yoy_ctr": y_ctr, "deltaCtr": d_ctr,
                "position": pos, "yoy_pos": y_pos, "deltaPos": d_pos,
                "judgement": judgement
            })
            
        queries.sort(key=lambda x: x["deltaClicks"], reverse=False) # sort by worst click loss
        
        return {"ok": True, "queries": queries[:20]}

    if type == "gsc_top_queries":
        # 全站熱門搜尋關鍵字（用於 GSC 熱門搜尋字頁籤）
        if is_mock_mode():
            master = load_mock_snapshots_master()
            queries = master.get("_meta", {}).get("queries", []) if master else MOCK_QUERIES
            res_queries = []
            for q in queries[:limit]:
                c = int(q.get("clicks", 0) or q.get("gscClicks", 0))
                i = int(q.get("impr", 0) or q.get("gscImpressions", 0))
                ctr_val = float(q.get("ctr", 0.0) or q.get("gscCtr", 0.0))
                p_val = float(q.get("pos", 0.0) or q.get("gscPosition", 0.0))
                res_queries.append({
                    "keys": [q.get("q") or q.get("query")],
                    "clicks": c,
                    "impressions": i,
                    "ctr": ctr_val,
                    "position": p_val
                })
            return {"ok": True, "queries": res_queries}
        site_url = get_gsc_site_url()
        if not site_url:
            return {"ok": False, "error": "Search Console 未連接或無已驗證網站"}
        
        if k == "last_month":
            if ym:
                y, m = int(ym[:4]), int(ym[4:6])
            else:
                now_tp = get_now_taipei()
                first_of_this_month = now_tp.replace(day=1)
                last_month_dt = first_of_this_month - timedelta(days=1)
                y, m = last_month_dt.year, last_month_dt.month
            first = datetime(y, m, 1)
            if m == 12:
                last = datetime(y + 1, 1, 1) - timedelta(days=1)
            else:
                last = datetime(y, m + 1, 1) - timedelta(days=1)
            start_date = first.strftime("%Y-%m-%d")
            end_date = last.strftime("%Y-%m-%d")
        else:
            k_val = k or "28d"
            range_info = date_range_for_k(k_val)
            start_date = range_info["startDate"]
            end_date = range_info["endDate"]
        
        has_prod_filter = bool(product or prod_det)
        
        if has_prod_filter:
            # 有產品篩選時，以 query + page 雙維度查詢，並在記憶體中進行產品過濾與彙總
            rows = gsc_query_analytics(site_url, start_date, end_date, dimensions=['query', 'page'], row_limit=25000)
            page_map = load_page_map()
            
            query_agg = {}
            for row in rows:
                q = row['keys'][0]
                page_url = row['keys'][1]
                
                # 解析網頁產品
                pinfo = resolve_page_by_params(page_url, page_map)
                
                # 產品過濾
                if product and pinfo["product"] != product:
                    continue
                if prod_det and pinfo["product_detail"] != prod_det:
                    continue
                
                clicks = int(row.get('clicks', 0))
                impr = int(row.get('impressions', 0))
                pos = float(row.get('position', 0.0))
                
                if q not in query_agg:
                    query_agg[q] = {"clicks": 0, "impressions": 0, "pos_w": 0.0, "clicks_for_pos": 0}
                
                query_agg[q]["clicks"] += clicks
                query_agg[q]["impressions"] += impr
                if pos > 0 and clicks > 0:
                    query_agg[q]["pos_w"] += pos * clicks
                    query_agg[q]["clicks_for_pos"] += clicks
            
            queries = []
            for q, v in query_agg.items():
                pos_avg = 0.0
                if v["clicks_for_pos"] > 0:
                    pos_avg = v["pos_w"] / v["clicks_for_pos"]
                queries.append({
                    "query": q,
                    "clicks": v["clicks"],
                    "impressions": v["impressions"],
                    "ctr": v["clicks"] / v["impressions"] if v["impressions"] > 0 else 0.0,
                    "position": pos_avg
                })
        else:
            # 無篩選時維持原本 dimensions=['query']，確保最大查詢效能
            rows = gsc_query_analytics(site_url, start_date, end_date, dimensions=['query'], row_limit=250)
            queries = []
            for row in rows:
                q = row['keys'][0]
                queries.append({
                    "query": q,
                    "clicks": int(row.get('clicks', 0)),
                    "impressions": int(row.get('impressions', 0)),
                    "ctr": float(row.get('ctr', 0.0)),
                    "position": float(row.get('position', 0.0))
                })
                
        queries.sort(key=lambda x: x["clicks"], reverse=True)
        queries = queries[:200]
        logger.info(f"gsc_top_queries: 回傳 {len(queries)} 筆關鍵字 (has_filter={has_prod_filter})")
        return {"ok": True, "queries": queries}

    if type == "query_pages":
        # 查詢某個關鍵字帶來流量的網頁列表
        site_url = get_gsc_site_url()
        if not site_url:
            return {"ok": False, "error": "Search Console 未連接或無已驗證網站"}
        
        target_query = query or id
        if not target_query:
            return {"ok": False, "error": "Missing query parameter"}
            
        if k == "last_month":
            if ym:
                y, m = int(ym[:4]), int(ym[4:6])
            else:
                now_tp = get_now_taipei()
                first_of_this_month = now_tp.replace(day=1)
                last_month_dt = first_of_this_month - timedelta(days=1)
                y, m = last_month_dt.year, last_month_dt.month
            first = datetime(y, m, 1)
            if m == 12:
                last = datetime(y + 1, 1, 1) - timedelta(days=1)
            else:
                last = datetime(y, m + 1, 1) - timedelta(days=1)
            start_date = first.strftime("%Y-%m-%d")
            end_date = last.strftime("%Y-%m-%d")
        else:
            k_val = k or "28d"
            range_info = date_range_for_k(k_val)
            start_date = range_info["startDate"]
            end_date = range_info["endDate"]

        service = get_gsc_service()
        if not service:
            return {"ok": False, "error": "Search Console API 服務未就緒"}

        # 用 query 篩選條件，並且 dimension 取 page
        body = {
            'startDate': start_date,
            'endDate': end_date,
            'dimensions': ['page'],
            'dimensionFilterGroups': [{
                'filters': [{
                    'dimension': 'query',
                    'operator': 'equals',
                    'expression': target_query
                }]
            }],
            'rowLimit': 100
        }
        
        try:
            result = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
            gsc_rows = result.get('rows', [])
        except Exception as e:
            logger.error(f"query_pages GSC 查詢失敗: {repr(e)}")
            return {"ok": False, "error": f"GSC API 查詢失敗: {str(e)}"}

        try:
            load_page_map()
        except Exception:
            pass
            
        pages = []
        for row in gsc_rows:
            raw_url = row['keys'][0]
            parsed = urllib.parse.urlparse(raw_url)
            queries_dict = urllib.parse.parse_qs(parsed.query)
            
            page_id = ""
            category_val = ""
            
            for key in ["news_id", "article_id", "comment_id", "lecture_id", "edm_id", "f_subject_no", "subject_no"]:
                if key in queries_dict:
                    page_id = queries_dict[key][0]
                    category_val = "edm" if key == "edm_id" else key
                    break
            
            if not page_id:
                path_parts = [p for p in parsed.path.split('/') if p]
                if path_parts:
                    page_id = path_parts[-1]
            
            page_title = ""
            prod_val = ""
            pdet_val = ""
            
            if _cached_page_map:
                pinfo = resolve_page_by_params(raw_url, _cached_page_map)
                if pinfo.get("name"):
                    page_title = pinfo["name"]
                    prod_val = pinfo.get("product", "")
                    pdet_val = pinfo.get("product_detail", "")
                    if not category_val:
                        category_val = pinfo.get("category", "")
            
            if product and prod_val != product:
                continue
            if prod_det and pdet_val != prod_det:
                continue
                
            if not page_title:
                page_title = parsed.path + (f"?{parsed.query}" if parsed.query else "")

            pages.append({
                "url": raw_url,
                "id": page_id or "N/A",
                "title": page_title,
                "category": category_val or "N/A",
                "impressions": int(row.get('impressions', 0)),
                "clicks": int(row.get('clicks', 0)),
                "ctr": float(row.get('ctr', 0.0)),
                "position": float(row.get('position', 0.0))
            })
            
        pages.sort(key=lambda x: x["clicks"], reverse=True)
        return {"ok": True, "pages": pages}


    # 背景排程手動建立快照 API
    if type == "build_l2":
        if not k: return {"ok": False, "error": "Missing k"}
        slot = get_current_slot_id()
        background_tasks.add_task(build_l2_snapshot_for_k, k, slot)
        return {"ok": True, "kind": "build_l2", "k": k, "slot": slot}
        
    if type == "build_drill":
        if not k: return {"ok": False, "error": "Missing k"}
        slot = get_current_slot_id()
        background_tasks.add_task(build_l2_drill_for_k, k, slot, limit)
        return {"ok": True, "kind": "build_drill", "k": k, "slot": slot, "limit": limit}
        

    # 1. 取得 GA4 client，若無憑證直接擋住
    client = get_ga4_client()
    if not client and not is_mock_mode():
        return {"ok": False, "error": "找不到或無法載入憑證。請確認已放置 client_secrets.json，並執行過 test_ga4.py 生成 token.json。"}

    # 2. 解析時間範圍資訊
    # K 支援 last_month
    if k == "last_month":
        if ym:
            y, m = int(ym[:4]), int(ym[4:6])
        else:
            now_tp = get_now_taipei()
            first_of_this_month = now_tp.replace(day=1)
            last_month_dt = first_of_this_month - timedelta(days=1)
            y, m = last_month_dt.year, last_month_dt.month
        ym_key = f"{y:04d}{m:02d}"
        
        first = datetime(y, m, 1)
        if m == 12:
            last = datetime(y + 1, 1, 1) - timedelta(days=1)
        else:
            last = datetime(y, m + 1, 1) - timedelta(days=1)
        start_date = first.strftime("%Y-%m-%d")
        end_date = last.strftime("%Y-%m-%d")
    else:
        # 預設
        k_val = k or "28d"
        range_info = date_range_for_k(k_val)
        start_date = range_info["startDate"]
        end_date = range_info["endDate"]
        ym_key = ""

    # 3. 優先讀取 L2 / Monthly 快照 (當不是 debug / invalidate / nocache 時)
    if not debug and not invalidate and not nocache:
        if k == "last_month":
            # 優先嘗試讀取該月度快照
            snap_name = f"monthly__{ym_key}__k__last_month"
            snap = read_snapshot_json(snap_name)
            if snap:
                if type == "all":
                    # 派生總覽
                    return derive_all_from_l2(snap, product, source_group, limit, prod_det)
                if type == "drill":
                    # 派生明細
                    gsc_prev = snap.get("prev", {}).get("gscRaw")
                    return derive_drill_from_drill_snapshot(snap, product, source_group, limit, prod_det, gsc_prev)
        else:
            # 一般時間區間快照
            slots = candidate_slots()
            if type == "all":
                for sl in slots:
                    snap_name = f"l2_kshot__{sl}__k__{k}"
                    snap = read_snapshot_json(snap_name)
                    if snap:
                        return derive_all_from_l2(snap, product, source_group, limit, prod_det)
            elif type == "drill":
                for sl in slots:
                    snap_name = f"l2_drill__{sl}__k__{k}"
                    snap = read_snapshot_json(snap_name)
                    if snap:
                        kshot = read_snapshot_json(f"l2_kshot__{sl}__k__{k}")
                        if kshot:
                            page_map = load_page_map()
                            # 重新從 kshot 的彙總資料建構 page_metrics，避免 Live 查詢
                            if "prev" not in snap: snap["prev"] = {}
                            if "yoy" not in snap: snap["yoy"] = {}
                            
                            p_rows = snap.get("prev", {}).get("heat", [])
                            p_gsc = snap.get("prev", {}).get("gscRaw", kshot.get("prev", {}).get("gscRaw", {}))
                            snap["prev"]["page"] = build_page_metrics_from_rows(filter_heat(p_rows, product, source_group, prod_det), page_map, p_gsc, product, prod_det)
                            
                            y_rows = snap.get("yoy", {}).get("heat", [])
                            y_gsc = snap.get("yoy", {}).get("gscRaw", kshot.get("yoy", {}).get("gscRaw", {}))
                            snap["yoy"]["page"] = build_page_metrics_from_rows(filter_heat(y_rows, product, source_group, prod_det), page_map, y_gsc, product, prod_det)
                            
                            gsc_prev = p_gsc
                        else:
                            gsc_prev = None
                        return derive_drill_from_drill_snapshot(snap, product, source_group, limit, prod_det, gsc_prev)

    # 4. 即時計算 (Live Mode) - 無論是強制 invalidate 或是快照 miss
    if is_mock_mode():
        logger.info(f"[Mock Mode] 執行 Mock 資料派生 (type={type}, k={k})")
        master = load_mock_snapshots_master()
        mock_k = (master.get(k or "28d") if master else None) or (master.get("28d") if master else None)
        if mock_k:
            if type == "all":
                return derive_all_from_l2(mock_k, product, source_group, limit, prod_det)
            if type == "drill":
                return derive_drill_from_drill_snapshot(mock_k, product, source_group, limit, prod_det)

    logger.info(f"[Live] 開始即時計算 (type={type}, k={k}, start={start_date}, end={end_date})")
    
    if type == "all":
        # 同步抓取當期
        heat_cur = get_heat_data(client, start_date, end_date)
        kpi_cur = get_kpi_base(client, start_date, end_date)
        
        # 過濾前端篩選
        heat_filtered = filter_heat(heat_cur, product, source_group, prod_det)
        
        # 圓餅圖
        pie_map = {}
        total_u = 0
        for r in heat_filtered:
            g = r["source_group"] or "未分類"
            pie_map[g] = pie_map.get(g, 0) + r["users"]
            total_u += r["users"]
        pie_rows = sorted([{"group": k, "users": v} for k, v in pie_map.items()], key=lambda x: x["users"], reverse=True)
        pie = {"totalUsersApprox": total_u, "rows": pie_rows}
        
        # 頁面清單
        page_map = load_page_map()
        drilldown = build_product_drilldown_data(page_map, product)
        
        # Mappings 彙總
        heat_agg_cur = aggregate_heat_for_summary(heat_filtered)
        heat_agg_by_prod_cur = aggregate_heat_for_summary_by_product(heat_filtered)
        
        # 對照組 (如果需要比較，前端會有 sel-compare；但 all endpoint 為了相容快照，也得回傳對照組資料)
        cmp = compute_prev_and_yoy_ranges(start_date, end_date)
        
        # 前期
        heat_prev = get_heat_data(client, cmp["prev"]["start"], cmp["prev"]["end"])
        filt_prev = filter_heat(heat_prev, product, source_group, prod_det)
        kpi_prev = get_kpi_base(client, cmp["prev"]["start"], cmp["prev"]["end"])
        heat_agg_prev = aggregate_heat_for_summary(filt_prev)
        heat_agg_by_prod_prev = aggregate_heat_for_summary_by_product(filt_prev)
        
        # 去年
        heat_yoy = get_heat_data(client, cmp["yoy"]["start"], cmp["yoy"]["end"])
        filt_yoy = filter_heat(heat_yoy, product, source_group, prod_det)
        kpi_yoy = get_kpi_base(client, cmp["yoy"]["start"], cmp["yoy"]["end"])
        heat_agg_yoy = aggregate_heat_for_summary(filt_yoy)
        heat_agg_by_prod_yoy = aggregate_heat_for_summary_by_product(filt_yoy)



        return {
            "ok": True,
            "_from": "live",
            "kpi": kpi_cur,
            "kpi_prev": kpi_prev,
            "kpi_yoy": kpi_yoy,
            "heatmap": heat_filtered[:limit],
            "heatmap_prev": filt_prev[:limit],
            "heatmap_yoy": filt_yoy[:limit],
            "heatAgg": heat_agg_cur[:limit],
            "heatAgg_prev": heat_agg_prev[:limit],
            "heatAgg_yoy": heat_agg_yoy[:limit],
            "heatAggByProduct": heat_agg_by_prod_cur[:limit],
            "heatAggByProduct_prev": heat_agg_by_prod_prev[:limit],
            "heatAggByProduct_yoy": heat_agg_by_prod_yoy[:limit],
            "pie": pie,
            "drilldown": drilldown,
            "filters": summarize_filters_for_ui(heat_cur, page_map)
        }
        
    if type == "drill":
        # 同步拑取 GSC 資料
        page_map = load_page_map()
        cmp = compute_prev_and_yoy_ranges(start_date, end_date)
        
        if product:
            logger.info(f"[Live Drill GSC] 產品 '{product}' 已選擇，啟動 100% 精確 GSC Regex 並行查詢...")
            regexes = build_gsc_regexes_for_product(page_map, product, prod_det)
            gsc_dict_cur = get_gsc_data_dict_by_regexes(start_date, end_date, regexes)
            gsc_dict_prev = get_gsc_data_dict_by_regexes(cmp["prev"]["start"], cmp["prev"]["end"], regexes)
            gsc_dict_yoy = get_gsc_data_dict_by_regexes(cmp["yoy"]["start"], cmp["yoy"]["end"], regexes)
        else:
            gsc_dict_cur = get_gsc_data_dict(client, start_date, end_date)
            gsc_dict_prev = get_gsc_data_dict(client, cmp["prev"]["start"], cmp["prev"]["end"])
            gsc_dict_yoy = get_gsc_data_dict(client, cmp["yoy"]["start"], cmp["yoy"]["end"])

        heat_cur = get_heat_data(client, start_date, end_date, limit=limit)
        filt_cur = filter_heat(heat_cur, product, source_group, prod_det)
        page_metrics = build_page_metrics_from_rows(filt_cur, page_map, gsc_dict=gsc_dict_cur, product=product, product_detail=prod_det)
        
        # prev
        heat_prev = get_heat_data(client, cmp["prev"]["start"], cmp["prev"]["end"], limit=limit)
        filt_prev = filter_heat(heat_prev, product, source_group, prod_det)
        page_metrics_prev = build_page_metrics_from_rows(filt_prev, page_map, gsc_dict=gsc_dict_prev, product=product, product_detail=prod_det)
        
        # yoy
        heat_yoy = get_heat_data(client, cmp["yoy"]["start"], cmp["yoy"]["end"], limit=limit)
        filt_yoy = filter_heat(heat_yoy, product, source_group, prod_det)
        page_metrics_yoy = build_page_metrics_from_rows(filt_yoy, page_map, gsc_dict=gsc_dict_yoy, product=product, product_detail=prod_det)
        
        try:
            diagnostics_top5 = calculate_seo_diagnostics_top5(page_metrics, page_metrics_prev)
        except Exception as e:
            logger.error(f"calculate_seo_diagnostics_top5 發生錯誤: {repr(e)}")
            diagnostics_top5 = []

        return {
            "ok": True,
            "_from": "live",
            "pageMetrics": page_metrics,
            "pageMetrics_prev": page_metrics_prev,
            "pageMetrics_yoy": page_metrics_yoy,
            "diagnosticsTop5": diagnostics_top5
        }
    if type == "breakdown":
        # 單篇來源組成分析
        target_id = id
        if not target_id: return {"ok": False, "error": "Missing id"}
        
        # 直接拉 Drill 快照明細或 live 計算
        rows = []
        if k:
            slots = candidate_slots()
            for sl in slots:
                snap_name = f"l2_drill__{sl}__k__{k}"
                snap = read_snapshot_json(snap_name)
                if snap and "rows" in snap:
                    rows = snap["rows"]
                    break
        if not rows:
            rows = get_heat_data(client, start_date, end_date, limit=100000)
            
        page_map = load_page_map()
        maps = load_mappings()
        
        def norm_id(sid: str) -> str:
            return str(sid or "").lower().replace("edm", "").replace("_", "").replace("-", "").strip()
            
        target_norm = norm_id(target_id)
        
        alias_map = {
            'news': ['news_id'],
            'article': ['article_id'],
            'comment': ['comment_id'],
            'lecture': ['lecture_id'],
            'edm': ['edm'],
            'course': ['f_subject_no', 'subject_no']
        }
        mapped_cats = alias_map.get(category, [category])
        
        acc = {}
        for r in rows:
            lp_raw = r.get("lp") or ""
            row_id = r.get("pageId") or ""
            row_cat = r.get("category") or ""
            row_prod = r.get("product") or ""
            row_pdet = r.get("product_detail") or ""
            
            # 過濾產品
            if product and row_prod != product: continue
            if prod_det and row_pdet != prod_det: continue
            
            # 比對 ID 與 類別
            if norm_id(row_id) != target_norm: continue
            if row_cat not in mapped_cats: continue
            
            views = int(r.get("users") or r.get("views") or r.get("sessions") or 0)
            
            g = r.get("source_group") or "未分類"
            s = r.get("source") or r.get("source_name") or ""
            if r.get("source_sub"):
                s += " / " + r["source_sub"]
                
            key = f"{g}||{s}"
            if key not in acc:
                acc[key] = {"group": g, "source": s, "views": 0}
            acc[key]["views"] += views
            
        final_arr = sorted(list(acc.values()), key=lambda x: x["views"], reverse=True)
        return {"ok": True, "breakdown": final_arr}
        
    if type == "export_data":
        # 解析真正的比較區間
        is_prev = k.endswith("_prev")
        is_yoy = k.endswith("_yoy")
        base_k = k.replace("_prev", "").replace("_yoy", "")
        
        if base_k == "last_month":
            if ym:
                y, m = int(ym[:4]), int(ym[4:6])
            else:
                now_tp = get_now_taipei()
                first_of_this_month = now_tp.replace(day=1)
                last_month_dt = first_of_this_month - timedelta(days=1)
                y, m = last_month_dt.year, last_month_dt.month
            first = datetime(y, m, 1)
            if m == 12:
                last = datetime(y + 1, 1, 1) - timedelta(days=1)
            else:
                last = datetime(y, m + 1, 1) - timedelta(days=1)
            e_start_date = first.strftime("%Y-%m-%d")
            e_end_date = last.strftime("%Y-%m-%d")
        else:
            e_range = date_range_for_k(base_k or "28d")
            e_start_date = e_range["startDate"]
            e_end_date = e_range["endDate"]
            
        comp_start, comp_end = None, None
        if is_prev or is_yoy:
            c_ranges = compute_prev_and_yoy_ranges(e_start_date, e_end_date)
            if is_prev:
                comp_start = c_ranges["prev"]["start"]
                comp_end = c_ranges["prev"]["end"]
            else:
                comp_start = c_ranges["yoy"]["start"]
                comp_end = c_ranges["yoy"]["end"]
        
        def fetch_ga4_report(sd, ed):
            req = RunReportRequest(
                property=f"properties/{GA4_PROPERTY_ID}",
                date_ranges=[DateRange(start_date=sd, end_date=ed)],
                dimensions=[Dimension(name="sessionSourceMedium"), Dimension(name="pageLocation")],
                metrics=[Metric(name="screenPageViews")],
                limit=100000
            )
            try:
                return client.run_report(req)
            except Exception as e:
                logger.error(f"GA4 export error: {e}")
                return None
                
        resp_curr = fetch_ga4_report(e_start_date, e_end_date)
        resp_comp = fetch_ga4_report(comp_start, comp_end) if comp_start else None
        
        if not resp_curr:
            return {"ok": False, "error": "GA4 fetch failed"}
            
        page_map = load_page_map()
        maps = load_mappings()
        
        results = {}
        def process_ga4_resp(response, is_comp_data=False):
            if not response: return
            for row in response.rows:
                src_raw = row.dimension_values[0].value
                lp_raw = row.dimension_values[1].value
                views = int(row.metric_values[0].value or 0)
                
                pinfo = resolve_page_by_params(lp_raw, page_map)
                if not pinfo or not pinfo.get("id"): continue
                if product and pinfo["product"] != product: continue
                if prod_det and pinfo["product_detail"] != prod_det: continue
                
                src_obj = resolve_source_name(src_raw, lp_raw, maps)
                g = src_obj["group"] or "未分類"
                s = src_obj["name"] or src_obj["label"] or src_raw
                if src_obj["sub"]: s += " / " + src_obj["sub"]
                    
                key = f"{pinfo['category']}|{pinfo['id']}|{g}|{s}"
                if key not in results:
                    results[key] = {
                        "product": pinfo["product"] or "未分類",
                        "product_detail": pinfo["product_detail"] or "",
                        "category": pinfo["category"],
                        "pageId": pinfo["id"],
                        "pageName": pinfo["name"] or pinfo["id"],
                        "sourceGroup": g,
                        "sourceName": s,
                        "views": 0,
                        "views_prev": 0
                    }
                if is_comp_data:
                    results[key]["views_prev"] += views
                else:
                    results[key]["views"] += views

        process_ga4_resp(resp_curr, False)
        process_ga4_resp(resp_comp, True)
            
        # 匯出 GSC 關鍵字資料
        gsc_queries_export = []
        site_url = get_gsc_site_url()
        if site_url:
            regexes = build_gsc_regexes_for_product(page_map, product, prod_det)
            if regexes and regexes[0] != "MATCH_NOTHING_XXX_999":
                creds = _get_creds()
                token = creds.token if creds else None
                if token:
                    def fetch_gsc_queries_regex(reg):
                        import requests
                        url = f"https://searchconsole.googleapis.com/webmasters/v3/sites/{urllib.parse.quote(site_url, safe='')}/searchAnalytics/query"
                        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                        
                        def do_fetch(sd, ed):
                            body = {
                                "startDate": sd,
                                "endDate": ed,
                                "dimensions": ["query", "page"],
                                "dimensionFilterGroups": [{"filters": [{"dimension": "page", "operator": "includingRegex", "expression": reg}]}],
                                "rowLimit": 25000
                            }
                            for attempt in range(3):
                                try:
                                    res = requests.post(url, headers=headers, json=body, timeout=60)
                                    if res.status_code == 200:
                                        return res.json().get("rows", [])
                                    elif res.status_code == 429:
                                        time.sleep(2)
                                        continue
                                    else:
                                        break
                                except:
                                    time.sleep(2)
                            return []
                        
                        rows_curr = do_fetch(e_start_date, e_end_date)
                        rows_comp = do_fetch(comp_start, comp_end) if comp_start else []
                        return (rows_curr, rows_comp)
                    
                    gsc_map = {}
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                        futures = [executor.submit(fetch_gsc_queries_regex, r) for r in regexes]
                        for future in concurrent.futures.as_completed(futures):
                            rows_curr, rows_comp = future.result()
                            
                            def process_gsc_rows(rows, is_comp_data=False):
                                for row in rows:
                                    if not row.get('keys') or len(row['keys']) < 2: continue
                                    q = row['keys'][0]
                                    page_url = row['keys'][1]
                                    pinfo = resolve_page_by_params(page_url, page_map)
                                    if not pinfo or not pinfo.get("id"): continue
                                    if product and pinfo.get("product") != product: continue
                                    if prod_det and pinfo.get("product_detail") != prod_det: continue
                                    
                                    key = f"{pinfo['category']}|{pinfo['id']}|{q}"
                                    if key not in gsc_map:
                                        gsc_map[key] = {
                                            "category": pinfo["category"],
                                            "pageId": str(pinfo["id"]),
                                            "pageName": pinfo["name"] or str(pinfo["id"]),
                                            "query": q,
                                            "impressions": 0, "clicks": 0, "ctr": 0.0, "position": 0.0,
                                            "impressions_prev": 0, "clicks_prev": 0, "ctr_prev": 0.0, "position_prev": 0.0
                                        }
                                    if is_comp_data:
                                        gsc_map[key]["impressions_prev"] = int(row.get("impressions", 0))
                                        gsc_map[key]["clicks_prev"] = int(row.get("clicks", 0))
                                        gsc_map[key]["ctr_prev"] = float(row.get("ctr", 0.0))
                                        gsc_map[key]["position_prev"] = float(row.get("position", 0.0))
                                    else:
                                        gsc_map[key]["impressions"] = int(row.get("impressions", 0))
                                        gsc_map[key]["clicks"] = int(row.get("clicks", 0))
                                        gsc_map[key]["ctr"] = float(row.get("ctr", 0.0))
                                        gsc_map[key]["position"] = float(row.get("position", 0.0))
                                        
                            process_gsc_rows(rows_curr, False)
                            process_gsc_rows(rows_comp, True)
                            
                    gsc_queries_export = list(gsc_map.values())

        return {"ok": True, "exportData": list(results.values()), "exportGscData": gsc_queries_export}

    return {"ok": False, "error": f"Unknown type: {type}"}

# =====================================================================
# 11. APScheduler 背景定時任務啟動
# =====================================================================
scheduler = BackgroundScheduler()

# 每天 06:10 與 12:10、18:10、00:10 背景跑一次快照
scheduler.add_job(build_all_l2_snapshots_job, "cron", hour="0,6,12,18", minute="10")
# 每月 1 號 01:00 產生上個月的月度快照
scheduler.add_job(build_monthly_snapshots_job, "cron", day="1", hour="1", minute="0")

@app.on_event("startup")
def startup_event():
    scheduler.start()
    logger.info("APScheduler 背景排程已啟動。")
    # 啟動時異步下載對照表進行本地初始化快取
    try:
        load_page_map()
        load_mappings()
        logger.info("初始化對照表加載完成。")
    except Exception as e:
        logger.error(f"初始化對照表載入失敗: {repr(e)}")

@app.on_event("shutdown")
def shutdown_event():
    scheduler.shutdown()
    logger.info("APScheduler 背景排程已關閉。")

# 伺服前端 index.html 與靜態資源
if hasattr(sys, '_MEIPASS'):
    frontend_dir = os.path.join(sys._MEIPASS, "frontend")
else:
    frontend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../frontend"))

@app.get("/")
def read_root():
    return FileResponse(os.path.join(frontend_dir, "index.html"))

@app.get("/index.html")
def read_index():
    return FileResponse(os.path.join(frontend_dir, "index.html"))

from fastapi import Response
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)
