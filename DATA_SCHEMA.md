# DATA_SCHEMA.md —— 关键数据文件字段手册（新窗口 AI 查表用）

## data/forum_cache.json（帖子缓存，采集层产出）
```
{ "<sourceKey>": {
    "platform": "eastmoney|sina|ths|taoguba",
    "fetchedAt": "ISO8601",
    "rawRows": int, "pageCount": int, "coverageDays": int,
    "capHit": bool, "coverageStatus": "normal|non_chronological_hot_feed|third_day_reached|anonymous_page_cap_lower_bound",
    "posts": [ {
        "id": "平台前缀:数字id",            # 跨平台不唯一——同帖转发拿不同 id（K3）
        "title": str, "source": str, "sourceCode": "600519", "sourceKey": str,
        "publishTime": "ISO8601", "topic": str
    } ]
} }
```

## data/llm_scores_ollama.json（评分库，STORE，唯一评分事实源）
```
[ { "id": "同帖子id", "direction": -3..3, "fear": 0..3, "greed": 0..3,
    "fomo": 0|1, "panic": 0|1, "newcomer": 0|1, "sarcasm": 0|1, "ai_marketing": 0|1,
    "persona": "baoma|novice|retail|none", "reason": "≤30字" } ]
```
旧存档条目可能缺 fear/greed（2026-11-25 前打的）——聚合层自动跳过。

## data/study_emotion.csv（研究瘦日志，按交易日 upsert）
`date,fear,greed,sectors_json,valid,platforms,window` —— sectors_json 为标准 CSV 引用的 JSON 单元格。

## data.js / latest.json 的 snapshot（发布层产物）
顶层键：version, marketDate, tradingDays[3], signals{heat,optimism,...,llmFear,llmGreed,llmValidPostCount,llmPlatformCount,llmPlatformSamples,llmWindowDays}, sectorIndices[{name,heat,heatRank,emotion,emotionN,activityHeat,...}], llmSample[24], community, history, historyMeta, snapshotVersion。
缺失语义：字段不存在=回落词典；emotion:null=样本不足（前端显"—"）。

## data/study/price/*.csv（akshare 东财 ETF 日线，列以中文表头：日期/开盘/收盘/...）
