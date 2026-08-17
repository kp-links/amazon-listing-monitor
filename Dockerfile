# Inventory Pulse — 在庫スナップショット Cloud Run Job 用イメージ。
# ビルド/デプロイは手動（このリポジトリにビルドトリガーは無い）。手順は PowerShell で:
#   cd C:\Users\staki\company-ai\amazon-listing-monitor
#   gcloud builds submit --tag asia-northeast1-docker.pkg.dev/keypath-pulse/pulse/inventory-snapshot:latest .
#   gcloud run jobs update pulse-inventory-snapshot --image <同タグ> --region asia-northeast1
# ⚠️ keypath_ai_amazon の cloudbuild.yaml とは独立（あちらの update 対象リストには載せない）。
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements-job.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-job.txt
COPY . .
# ブランドと sink は Job 側の env（BRAND / SNAPSHOT_SINK）で上書きする
CMD ["python", "inventory_snapshot.py", "--sink", "parquet"]
