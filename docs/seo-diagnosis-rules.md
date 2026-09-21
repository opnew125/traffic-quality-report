# Rule-based SEO 診斷引擎 (Rule-based SEO Diagnosis Engine)

## 1. 診斷引擎總覽 (Diagnosis Engine Overview)

**Rule-based SEO 診斷引擎** 透過比較當期與基準期 (YoY 去年同期或上一區間) 的 Search Console 指標，評估頁面級別的搜尋表現。

系統不採用黑盒機器學習 (Machine Learning)，而是運用**確定性的 Rule-based 條件**計算 Anomaly Score (0~100 分) 並標註診斷病徵標籤。這讓行銷與 SEO 團隊無須手動拉 Excel 即可快速進行排查。

---

## 2. 異常分數計算 (Anomaly Score Calculation, 0~100 分)

當 YoY 點擊衰退 (`deltaClicks < 0`) 時，系統會依據 4 大扣分因子累加分數；若點擊成長 (`deltaClicks > 0`)，則 Anomaly Score 強制歸 0（健康狀態）。

### 扣分因子算式表

| 扣分因子 | 最高配分 | 計算算式 | 評估指標意義 |
| :--- | :--- | :--- | :--- |
| **A. 點擊流失懲罰** | 50 分 | `min(50, abs(deltaClicks) * 0.1)` | YoY 點擊流失總量 |
| **B. 排名下降懲罰** | 30 分 | `min(30, max(0, deltaPosition) * 5)` | SERP 平均排名退步幅度 |
| **C. CTR 衰退懲罰** | 20 分 | `min(20, abs(deltaCTR) * 100 * 2)` | 自然點閱率 (CTR) 衰退幅度 |
| **D. 高曝光零點擊** | 20 分 | `min(20, currentImpressions * 0.01)` | 有搜尋曝光但點擊為 0 (Impressions > 500) |

總異常分數 (Anomaly Score) = `min(100, 因子 A + 因子 B + 因子 C + 因子 D)`

---

## 3. 診斷病徵標籤與調查規則 (Diagnostic Archetypes)

算出總分後，系統依優先順序比對規則，為網址標註診斷病徵標籤並給予調查建議。

> [!IMPORTANT]
> **分析注意事項**：診斷病徵標籤旨在凸顯數據中「可觀察的模式現象 (Observable Patterns)」，用以指引排查方向，而非代表已證實的因果關係。實際原因需綜合考量 SERP 版面改動、季節性趨勢、搜尋意圖轉變或網站內容異動。

| 診斷病徵標籤 | 可觀察數據模式 | 建議調查方向 |
| :--- | :--- | :--- |
| **高價值流量流失 (High-Value Traffic Loss)** | 點擊 ↓ (`deltaClicks < 0`) 且 曝光 ↓ (`deltaImpressions < 0`) | 檢查文章內容是否過時、觀察季節性趨勢，或確認主要目標關鍵字搜尋需求是否下降。 |
| **排名衰退 (Ranking Decline)** | 點擊 ↓ (`deltaClicks < 0`) 且 排名退步 > 1 名 | 分析 SERP 競品動態、檢查新竄起競品內容，並優化本頁內容深度。 |
| **CTR衰退 (CTR Decline)** | 點擊 ↓ (`deltaClicks < 0`) 且 CTR 衰退 > 5% | 檢查 Meta Title 與 Description 的 SERP 吸引力，或確認是否有新的 SERP Feature 搶走版面。 |
| **高曝光低 CTR (High Exposure / Low CTR)** | 當期點擊 = 0 且 曝光量 > 1,000 | 評估搜尋意圖符合度，嘗試重寫 Meta 標題以提升點閱誘因。 |
| **搜尋需求下降 (Search Demand Decline)** | 曝光 ↓ > 500 且 點擊 ↓ (`deltaClicks < 0`) | 反映大環境對於該關鍵字議題的搜尋熱度降低，屬市場現象而非技術懲罰。 |
| **快進首頁 (Near Page-One Opportunity)** | 點擊 ↑ (`deltaClicks > 0`) 且 排名 < 10 名 | 識別出正在接近第一頁的潛力頁面，可新增內部連結與更新內容以鞏固排名。 |
| **表現正常 (Normal Performance)** | 未觸發上述任何規則 | 表現平穩，屬例行監測範圍。 |

---

## 4. UI 看板整合與排序

在前端看板中：
- 頁面預設依 **Anomaly Score (異常分數)** 降序排列，將高風險或高潛力頁面置於審查佇列頂端。
- ECharts 散佈圖將 **點擊差異 (Click Delta) vs. 曝光差異 (Impression Delta)** 繪製於四個象限中。
- 診斷摘要卡片支援點擊過濾，即時篩選特定病徵的頁面清單。
