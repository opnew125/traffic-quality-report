# 部署與本機執行指南 (Deployment & Setup Guide)

本指南說明如何在本地端執行 **Web Traffic Quality & SEO Diagnosis Dashboard**、產生 Mock 快照資料進行測試，以及將前端部署為靜態預覽。

---

## 1. 環境需求 (Prerequisites)

- **Python**：3.9 以上版本（建議 Python 3.11）
- **網頁瀏覽器**：Modern Browser (Chrome, Edge, Firefox, Safari)

---

## 2. 快速開始：獨立離線 Demo (無須 Google API Credentials)

本專案內建純前端與本機離線 Demo 引擎，無須設定 GCP 憑證或 API Keys 即可直接啟動體驗。

### 步驟 1：複製儲存庫 (Clone Repository)
```bash
git clone https://github.com/opnew125/traffic-quality-report.git
cd traffic-quality-report
```

### 步驟 2：安裝依賴套件 (Install Dependencies)
```bash
pip install -r requirements.txt
```

### 步驟 3：產生 Mock 資料快照 (Generate Mock Data)
```bash
python backend/generate_mock_data.py
```
此腳本會在 `backend/snapshots/` 產生包含 100+ 筆跨產品去識別化測試資料的 `mock_snapshots.json`。

### 步驟 4：啟動 FastAPI 後端伺服器 (Start Backend)
```bash
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8888
```
後端服務會啟動於 `http://127.0.0.1:8888`。

### 步驟 5：開啟前端看板 (Open Frontend)
在瀏覽器開啟 `frontend/index.html`，或透過 Python 內建 HTTP Server 啟動靜態託管：
```bash
cd frontend
python -m http.server 8080
```
瀏覽器連線至 `http://127.0.0.1:8080`，使用 Demo 帳號登入：
- **員工編號**：`DEMO001` 或 `DEMO002`

---

## 3. GitHub Pages 靜態預覽模式 (GitHub Pages Preview)

前端 (`frontend/index.html`) 支援免伺服器直接於 **GitHub Pages** 上獨立運作。

當託管於 GitHub Pages 或 `file://` 協定時，前端 JavaScript 會自動辨識 Hostname 並切換至 **Client-side Mock Data Generator**，無須運行後端即可點擊體驗全套看板互動功能。

- **GitHub Pages 預覽網址**：`https://opnew125.github.io/traffic-quality-report/frontend/`

---

## 4. 獨立 ngrok 外網連線設定 (Optional Remote Tunneling)

若需要將本機 FastAPI 服務開放給外部網路測試：

> [!IMPORTANT]
> 請勿將執行檔提交至 Git 儲存庫。請從官方管道獨立安裝 `ngrok`。

1. 從官方網站 [https://ngrok.com/download](https://ngrok.com/download) 下載安裝 ngrok。
2. 啟動本機 8888 Port 的 HTTP Tunnel：
   ```bash
   ngrok http 8888
   ```
3. 使用生成的 `https://xxxx.ngrok-free.app` 網址進行遠端 API 測試。
