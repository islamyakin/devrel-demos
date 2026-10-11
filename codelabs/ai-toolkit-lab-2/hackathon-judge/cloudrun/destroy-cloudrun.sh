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
# Removes what deploy-cloudrun.sh created. Keeps the BigQuery dataset unless DROP_DATA=true.
set -uo pipefail
: "${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT}"
R="${GOOGLE_CLOUD_REGION:-asia-southeast2}"
gcloud beta run worker-pools delete "${AGENT_POOL:-hackathon-judge-agent}" --region="$R" --quiet
gcloud run services delete "${WEB_SERVICE:-hackathon-judge-web}" --region="$R" --quiet
for s in "${TASKS_SUBSCRIPTION:-judge-tasks-sub}" "${RESULTS_SUB:-judge-results-sub}"; do gcloud pubsub subscriptions delete "$s" --quiet; done
for t in "${TASKS_TOPIC:-judge-tasks}" "${RESULTS_TOPIC:-judge-results}"; do gcloud pubsub topics delete "$t" --quiet; done
if [ "${DROP_DATA:-false}" = "true" ]; then bq rm -r -f -d "$GOOGLE_CLOUD_PROJECT:${BQ_DATASET:-hackathon_judge}"; fi
echo "Done. Artifact Registry repo and service accounts were kept."
