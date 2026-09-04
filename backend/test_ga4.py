import os
import sys
from datetime import datetime, timedelta

# 定義 Scope (唯讀權限)
SCOPES = [
    'https://www.googleapis.com/auth/analytics.readonly',
    'https://www.googleapis.com/auth/webmasters.readonly',
]

def test_connection():
    print("=" * 60)
    print("[品質報表] 開始診斷 GA4 API 連通性與個人帳號授權")
    print("=" * 60)
    
    secrets_path = "client_secrets.json"
    token_path = "token.json"
    
    if not os.path.exists(secrets_path):
        print(f"[錯誤] 找不到 {secrets_path} 檔案！")
        print("請至 Google Cloud Console 下載 OAuth 2.0 用戶端 ID (傳統版應用程式/Desktop App) 的 JSON 金鑰，")
        print(f"將其重新命名為 {secrets_path} 並放置於 backend 目錄下。")
        print("-" * 60)
        return
        
    print(f"[資訊] 成功偵測到 {secrets_path}。")
    
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Metric
    except ImportError:
        print("[錯誤] 找不到需要的 Python 庫。請先確保已執行 setup.bat 安裝依賴。")
        return

    creds = None
    # 檢查是否有 token.json
    if os.path.exists(token_path):
        print(f"[資訊] 偵測到現有的快取憑證 {token_path}，正在載入並驗證...")
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception as e:
            print(f"[警告] 讀取 {token_path} 失敗，將重新發起瀏覽器登入授權。({e})")
            creds = None
            
    # 驗證憑證是否有效
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("[資訊] 憑證已過期，正在自動刷新...")
            try:
                creds.refresh(Request())
                with open(token_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
                print("[成功] 憑證自動刷新成功，已更新 token.json！")
            except Exception as e:
                print(f"[警告] 自動刷新失敗: {e}，將重新發起瀏覽器登入授權。")
                creds = None
                
        if not creds:
            print("[引導] 即將啟動瀏覽器進行 Google 帳號授權登入...")
            print("請注意：")
            print("1. 程式會自動彈出瀏覽器視窗。")
            print("2. 請登入「具備 GA4 報表檢視權限」的 Google 帳號。")
            print("3. 若出現「Google 尚未驗證此應用程式」警告，請點擊「進階」並選擇「前往 Local GA4 Report (安全)」。")
            print("4. 請務必勾選「查看您的 Google Analytics (分析) 資料」及「管理 Search Console 網站上已驗證的網站清單」權限。")
            print("-" * 60)
            
            try:
                flow = InstalledAppFlow.from_client_secrets_file(secrets_path, SCOPES)
                # 開啟本地臨時 server 接收授權碼
                creds = flow.run_local_server(port=0)
                with open(token_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
                print(f"[成功] 本地授權成功！已生成憑證快取 {token_path}。")
            except Exception as e:
                print(f"[錯誤] 瀏覽器授權登入失敗！錯誤訊息: {e}")
                return

    # 連線 GA4 API 測試
    try:
        client = BetaAnalyticsDataClient(credentials=creds)
        property_id = "257689285"
        print(f"[資訊] 正在發送測試查詢到 GA4 Property: {property_id} (近 1 天使用者數)...")
        
        req = RunReportRequest(
            property=f"properties/{property_id}",
            date_ranges=[DateRange(start_date="yesterday", end_date="today")],
            metrics=[Metric(name="totalUsers")]
        )
        response = client.run_report(req)
        users = 0
        if response.rows:
            users = response.rows[0].metric_values[0].value
            
        print("[成功] 順利與 GA4 Data API 連線並取得資料！")
        print(f"[結果] 昨今兩日全站總使用者數為: {users}")
        print("=" * 60)
        print("[恭喜] GA4 連通性診斷通過！")
        print("=" * 60)
    except Exception as e:
        print(f"[錯誤] 向 GA4 請求資料失敗！")
        print(f"詳細原因: {e}")
        print("-" * 60)
        print("常見原因排除：")
        print("1. 登入的 Google 帳號對該 GA4 Property ID (257689285) 並無權限。")
        print("2. 本機網路無法連上 Google API (請檢查防火牆或 Proxy)。")
        print("=" * 60)

    # ---- Search Console API 測試 ----
    print()
    print("=" * 60)
    print("[GSC] 開始診斷 Google Search Console API 連通性")
    print("=" * 60)
    try:
        from googleapiclient.discovery import build
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
        
        authorized_http = AuthorizedHttp(creds, http=httplib2.Http())
        gsc_service = build('searchconsole', 'v1', http=authorized_http)
        
        sites = gsc_service.sites().list().execute()
        site_list = sites.get('siteEntry', [])
        
        if site_list:
            print(f"[成功] Search Console API 連線成功！偵測到 {len(site_list)} 個已驗證的網站：")
            for s in site_list:
                perm = s.get('permissionLevel', '?')
                print(f"  - {s['siteUrl']}  (權限: {perm})")
            
            # 嘗試查詢第一個網站的搜尋數據
            target_site = site_list[0]['siteUrl']
            print(f"\n[資訊] 正在查詢 {target_site} 的近 7 天搜尋數據...")
            body = {
                'startDate': (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d'),
                'endDate': datetime.now().strftime('%Y-%m-%d'),
                'dimensions': ['query'],
                'rowLimit': 5
            }
            result = gsc_service.searchanalytics().query(siteUrl=target_site, body=body).execute()
            rows = result.get('rows', [])
            if rows:
                print(f"[成功] 取得 {len(rows)} 筆搜尋關鍵字（顯示前 5 筆）：")
                for r in rows:
                    q = r['keys'][0]
                    cl = int(r.get('clicks', 0))
                    im = int(r.get('impressions', 0))
                    pos = r.get('position', 0)
                    print(f"  🔍 {q[:50]:50s}  clicks={cl:<6} impr={im:<8} pos={pos:.1f}")
            else:
                print("[警告] 該網站在近 7 天內無搜尋數據。")
        else:
            print("[警告] Search Console 中沒有已驗證的網站。")
            print("請前往 https://search.google.com/search-console 新增並驗證您的網站。")
        
        print("=" * 60)
        print("[恭喜] 所有連通性診斷完成！")
        print("=" * 60)
    except ImportError:
        print("[警告] 找不到 google-api-python-client 套件，無法測試 Search Console。")
        print("請執行: pip install google-api-python-client")
    except Exception as e:
        print(f"[錯誤] Search Console API 連線失敗: {e}")
        print("可能原因：")
        print("1. token.json 不包含 webmasters.readonly scope（請刪除 token.json 重新授權）")
        print("2. Google Cloud 專案未啟用 Search Console API")
        print("3. 帳號沒有任何已驗證的 Search Console 網站")
        print("=" * 60)

if __name__ == "__main__":
    test_connection()
