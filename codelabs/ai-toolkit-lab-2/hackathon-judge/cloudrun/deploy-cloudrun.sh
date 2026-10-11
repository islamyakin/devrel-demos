#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Deploy Hackathon Judge on Cloud Run only (no GKE).
#
#   web    Cloud Run service   nginx frontend + Go backend sidecar
#   agent  Cloud Run worker pool with --sandbox-launcher (pulls tasks from Pub/Sub)
#
# Usage (from the repo root):
#   GOOGLE_CLOUD_PROJECT=my-project ./cloudrun/deploy-cloudrun.sh
#   PUBLIC=true ...   also lets anyone open the web UI (default: private)
set -euo pipefail
cd "$(dirname "$0")/.."

: "${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT}"
export GOOGLE_CLOUD_REGION="${GOOGLE_CLOUD_REGION:-asia-southeast2}"   # Jakarta
export AR_REPO="${AR_REPO:-hackathon-judge}"
export BQ_DATASET="${BQ_DATASET:-hackathon_judge}"
export TASKS_TOPIC="${TASKS_TOPIC:-judge-tasks}"
export TASKS_SUBSCRIPTION="${TASKS_SUBSCRIPTION:-judge-tasks-sub}"
export RESULTS_TOPIC="${RESULTS_TOPIC:-judge-results}"
export RESULTS_SUB="${RESULTS_SUB:-judge-results-sub}"
export WEB_SERVICE="${WEB_SERVICE:-hackathon-judge-web}"
export AGENT_POOL="${AGENT_POOL:-hackathon-judge-agent}"
export AGENT_INSTANCES="${AGENT_INSTANCES:-1}"
export JUDGE_MODEL="${JUDGE_MODEL:-gemini-3-flash-preview}"
export PUBLIC="${PUBLIC:-false}"
export TAG="${TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%s)}"
export IMAGE_BASE="${GOOGLE_CLOUD_REGION}-docker.pkg.dev/${GOOGLE_CLOUD_PROJECT}/${AR_REPO}"
export WEB_SA="judge-web@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"
export AGENT_SA="judge-agent@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
gcloud config set project "$GOOGLE_CLOUD_PROJECT" >/dev/null

log "1/7 Enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com pubsub.googleapis.com \
  bigquery.googleapis.com aiplatform.googleapis.com iam.googleapis.com

log "2/7 Artifact Registry"
gcloud artifacts repositories describe "$AR_REPO" --location="$GOOGLE_CLOUD_REGION" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "$AR_REPO" --repository-format=docker --location="$GOOGLE_CLOUD_REGION"

log "3/7 Pub/Sub"
for t in "$TASKS_TOPIC" "$RESULTS_TOPIC"; do
  gcloud pubsub topics describe "$t" >/dev/null 2>&1 || gcloud pubsub topics create "$t"
done
# Long ack deadline: one evaluation can take several minutes.
gcloud pubsub subscriptions describe "$TASKS_SUBSCRIPTION" >/dev/null 2>&1 \
  || gcloud pubsub subscriptions create "$TASKS_SUBSCRIPTION" --topic="$TASKS_TOPIC" --ack-deadline=600
gcloud pubsub subscriptions describe "$RESULTS_SUB" >/dev/null 2>&1 \
  || gcloud pubsub subscriptions create "$RESULTS_SUB" --topic="$RESULTS_TOPIC"

log "4/7 BigQuery"
export COLUMNS=80 LINES=24
bq show --project_id="$GOOGLE_CLOUD_PROJECT" "$BQ_DATASET" >/dev/null 2>&1 \
  || bq --project_id="$GOOGLE_CLOUD_PROJECT" mk --location="$GOOGLE_CLOUD_REGION" --dataset "$GOOGLE_CLOUD_PROJECT:$BQ_DATASET"
sed -e "s/<<YOUR PROJECT ID>>/${GOOGLE_CLOUD_PROJECT}/g" -e "s/<<REGION>>/${GOOGLE_CLOUD_REGION}/g" \
    -e "s/hackathon_judge/${BQ_DATASET}/g" backend/internal/repository/schema.sql \
  | bq --project_id="$GOOGLE_CLOUD_PROJECT" query --use_legacy_sql=false --location="$GOOGLE_CLOUD_REGION"
# Seeds are plain INSERTs: only load them into an empty table so reruns don't duplicate rows.
SEEDED=$(bq --project_id="$GOOGLE_CLOUD_PROJECT" --format=csv query --use_legacy_sql=false --location="$GOOGLE_CLOUD_REGION" \
  "SELECT COUNT(*) FROM \`${BQ_DATASET}.hackathons\`" | tail -1)
if [ "$SEEDED" = "0" ]; then
  sed -e "s/<<YOUR PROJECT ID>>/${GOOGLE_CLOUD_PROJECT}/g" -e "s/hackathon_judge/${BQ_DATASET}/g" \
      backend/internal/repository/seeds.sql \
    | bq --project_id="$GOOGLE_CLOUD_PROJECT" query --use_legacy_sql=false --location="$GOOGLE_CLOUD_REGION"
fi

log "5/7 Service accounts (least privilege, one per workload)"
for sa in judge-web judge-agent; do
  gcloud iam service-accounts describe "${sa}@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$sa"
done
bind() { gcloud projects add-iam-policy-binding "$GOOGLE_CLOUD_PROJECT" \
  --member="serviceAccount:$1" --role="$2" --condition=None --quiet >/dev/null; }
bind "$WEB_SA" roles/bigquery.dataEditor
bind "$WEB_SA" roles/bigquery.jobUser
gcloud pubsub topics add-iam-policy-binding "$TASKS_TOPIC" --member="serviceAccount:$WEB_SA" --role=roles/pubsub.publisher >/dev/null
gcloud pubsub subscriptions add-iam-policy-binding "$RESULTS_SUB" --member="serviceAccount:$WEB_SA" --role=roles/pubsub.subscriber >/dev/null
# The agent talks to Gemini. Its sandboxes can't use this identity: they have
# no access to the metadata server.
bind "$AGENT_SA" roles/aiplatform.user
gcloud pubsub subscriptions add-iam-policy-binding "$TASKS_SUBSCRIPTION" --member="serviceAccount:$AGENT_SA" --role=roles/pubsub.subscriber >/dev/null
gcloud pubsub topics add-iam-policy-binding "$RESULTS_TOPIC" --member="serviceAccount:$AGENT_SA" --role=roles/pubsub.publisher >/dev/null

log "6/7 Building images with Cloud Build (tag: $TAG)"
gcloud builds submit --config=cloudrun/cloudbuild.cloudrun.yaml \
  --substitutions="_IMAGE_BASE=${IMAGE_BASE},_TAG=${TAG}" .

[ "${BUILD_ONLY:-false}" = "true" ] && { log "BUILD_ONLY=true, skipping deploy"; exit 0; }

log "7/7 Deploying"
envsubst < cloudrun/web-service.yaml > /tmp/web-service.rendered.yaml
gcloud run services replace /tmp/web-service.rendered.yaml --region="$GOOGLE_CLOUD_REGION"
if [ "$PUBLIC" = "true" ]; then
  gcloud run services add-iam-policy-binding "$WEB_SERVICE" --region="$GOOGLE_CLOUD_REGION" \
    --member=allUsers --role=roles/run.invoker >/dev/null
fi

# Sandboxes share the instance's CPU and memory: size for the agent + one
# submission build. Scale with AGENT_INSTANCES, not with more concurrency.
gcloud beta run worker-pools deploy "$AGENT_POOL" \
  --region="$GOOGLE_CLOUD_REGION" \
  --image="${IMAGE_BASE}/agent-cloudrun:${TAG}" \
  --service-account="$AGENT_SA" \
  --sandbox-launcher \
  --cpu=4 --memory=8Gi \
  --instances="$AGENT_INSTANCES" \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT},TASKS_SUBSCRIPTION=${TASKS_SUBSCRIPTION},RESULTS_TOPIC=${RESULTS_TOPIC},AGENT_TYPE=cloudrun,JUDGE_MODEL=${JUDGE_MODEL},MAX_CONCURRENT_EVALS=1,GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_LOCATION=global"

URL=$(gcloud run services describe "$WEB_SERVICE" --region="$GOOGLE_CLOUD_REGION" --format='value(status.url)')
log "Done"
echo "Web UI: $URL"
[ "$PUBLIC" = "true" ] || echo "Private service. Open it locally with: gcloud run services proxy $WEB_SERVICE --region=$GOOGLE_CLOUD_REGION"
echo "Agent logs: Cloud Console > Cloud Run > Worker pools > $AGENT_POOL > Logs"
