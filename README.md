# Capital Flow Radar

世界の資金需給を、公表された純フローと先物建玉に分けて可視化するダッシュボードです。

公開サイト: https://capital-flow-radar.rysawaki.chatgpt.site

## 現在の対象

- CFTC: S&P 500、NASDAQ 100、WTI原油、銅、金、円、10年米国債
- JPX: 海外投資家の日本株売買
- Farside Investors: 米国現物Bitcoin ETF
- FRED / Yahoo Finance: 基準日価格と為替

## 自動更新

- 毎週土曜日6:00（日本時間）にGitHub Actionsで更新
- GitHub画面から手動実行も可能
- 取得結果を検証してから `dist/data.json` を更新
- 欠損時に架空値を生成しない
- 個別取得に失敗した場合は、検証済みの直近保存値を維持

## 構成

- `scripts/update_data.py`: データ取得と計算
- `scripts/validate_data.py`: 公開前のデータ検証
- `dist/data.json`: 公開用スナップショット
- `dist/index.html`: ダッシュボード
- `.github/workflows/weekly-update.yml`: 週次自動更新

## 実行

```bash
python -m pip install -r requirements.txt
python scripts/update_data.py
python scripts/validate_data.py
```

## 注意

先物の金額は想定元本であり、現金の純流入額ではありません。異なる分類の金額は単純合算しません。
