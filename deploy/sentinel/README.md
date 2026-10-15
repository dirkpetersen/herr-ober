# Sentinel and Defender for Endpoint deployment

This directory holds everything Azure-side that `ober watch` needs. Only findings and
per-access-key summaries are sent to Sentinel; raw logs never are.

| File | Purpose |
|------|---------|
| `tables.json` | ARM template: custom tables `OberFindings_CL`, `OberKeySummary_CL` |
| `dcr.json` | ARM template: Direct data collection rule (no DCE required); outputs the immutable ID |
| `analytics-rules.json` | ARM template: five scheduled analytics rules |

Variables used below (set them first):

```bash
RG=rg-ober-sentinel
WS=law-ober                     # Log Analytics workspace with Sentinel enabled
LOC=westus2                     # must equal the workspace region
DCR=dcr-herr-ober
APP=herr-ober-watch
```

## 1. Workspace and Sentinel

Skip if you already have a Sentinel-enabled workspace.

```bash
az group create -n $RG -l $LOC
az monitor log-analytics workspace create -g $RG -n $WS -l $LOC
# Enable Sentinel on the workspace (Azure portal: Microsoft Sentinel > Add), or:
az extension add --name sentinel
az sentinel onboarding-state create -g $RG --workspace-name $WS -n default
```

Microsoft will retire the Azure-portal Sentinel experience after 2027-03-31 (Defender portal
only). The ARM resources below are the same in both portals.

## 2. Tables

```bash
az deployment group create -g $RG -n ober-tables \
  --template-file tables.json --parameters workspaceName=$WS retentionInDays=90
```

Equivalent CLI without ARM:

```bash
az monitor log-analytics workspace table create -g $RG --workspace-name $WS \
  -n OberFindings_CL --plan Analytics --columns \
  TimeGenerated=datetime finding_id=string ts=real rule=string severity=string \
  client_ip=string access_key=string bucket=string node=string count=long \
  window_s=long detail=string action=string layer=string

az monitor log-analytics workspace table create -g $RG --workspace-name $WS \
  -n OberKeySummary_CL --plan Analytics --columns \
  TimeGenerated=datetime window_start=real window_s=long access_key=string \
  requests=long bytes_in=long bytes_out=long auth_failures=long errors_5xx=long \
  distinct_ips=long distinct_buckets=long internet_requests=long nodes=dynamic
```

Note: `Finding.id` is sent as `finding_id` because `id` is a reserved column name.
`TimeGenerated` is the finding time (`ts`) for findings and the window end
(`window_start + window_s`) for summaries.

## 3. Data collection rule

A DCR of `kind: Direct` creates its own ingestion endpoint, so no data collection endpoint
(DCE) is needed unless you use Private Link. The DCR must be in the workspace's region.

```bash
az deployment group create -g $RG -n ober-dcr \
  --template-file dcr.json --parameters workspaceName=$WS dcrName=$DCR location=$LOC

az deployment group show -g $RG -n ober-dcr --query properties.outputs
# dcrImmutableId        -> sentinel.dcr_immutable_id   (dcr-...)
# logsIngestionEndpoint -> sentinel.dce_endpoint       (https://<name>.<region>.ingest.monitor.azure.com)
# dcrResourceId         -> used in step 4
```

If you need a DCE (Private Link), create one and add its resource ID to the DCR as
`properties.dataCollectionEndpointId`; then use the DCE's `logsIngestion` endpoint as
`sentinel.dce_endpoint`:

```bash
az monitor data-collection endpoint create -g $RG -n dce-herr-ober -l $LOC --public-network-access Enabled
```

## 4. Entra app registration

```bash
APPID=$(az ad app create --display-name $APP --query appId -o tsv)
az ad sp create --id $APPID
SECRET=$(az ad app credential reset --id $APPID --append --display-name ober-watch \
         --years 1 --query password -o tsv)
TENANT=$(az account show --query tenantId -o tsv)

DCR_ID=$(az deployment group show -g $RG -n ober-dcr --query properties.outputs.dcrResourceId.value -o tsv)
az role assignment create --assignee $APPID --role "Monitoring Metrics Publisher" --scope "$DCR_ID"
```

Role assignment can take a few minutes to propagate; until then ingestion returns 403 and
`ober watch` spools the rows and retries.

Put the secret on the analysis host (permissions 600):

```bash
echo "SENTINEL_CLIENT_SECRET=$SECRET" >> ~/.ober/login
```

and in `ober.yaml`:

```yaml
sentinel:
  enabled: true
  tenant_id: <TENANT>
  client_id: <APPID>
  dce_endpoint: https://<name>.<region>.ingest.monitor.azure.com
  dcr_immutable_id: dcr-xxxxxxxxxxxxxxxxxxxxxxxx
  findings_stream: Custom-OberFindings_CL
  summary_stream: Custom-OberKeySummary_CL
```

## 5. Analytics rules

```bash
az deployment group create -g $RG -n ober-rules \
  --template-file analytics-rules.json --parameters workspaceName=$WS
```

| Rule | Severity | Schedule (every / lookback) | Notes |
|------|----------|-----------------------------|-------|
| (a) any high-severity finding | High | 5 min / 1 h | Entities: IP `client_ip`, Host `node`; details `access_key`, `bucket`, `rule` |
| (b) same IP flagged on >1 node within 1 h | Medium | 15 min / 1 h | IP + up to three Host mappings (`node_1..3`) |
| (c) key used from >20 distinct IPs in a window | Medium | 5 min / 1 h | Summaries carry no client IP, so only Host (first node) is mapped; details `access_key`, `rule` |
| (d) egress spike | High | 30 min / 14 d | Last-hour `bytes_out` > 5x the key's average hourly bytes over the previous ~14 days and > 50 GiB; details `access_key`, `rule` |
| (e) health: no summaries for 30 min | Medium | 10 min / 30 min | Will fire during genuinely idle periods if `ober watch` only emits summaries for keys with traffic; adjust the window or disable |

Behaviour to know:

* Scheduled rules only see rows whose `TimeGenerated` is inside the lookback. Findings
  replayed from the spool after an outage longer than the lookback are stored but do not alert.
* Rules (a) and (c) use `ingestion_time() > ago(5m)` to avoid re-alerting on the same row
  across overlapping runs.
* Rule (d) keys the baseline on 14 days of summaries; a new key has no baseline, so the 50 GiB
  floor alone applies. The lookback of `P14D` is the documented maximum.
* The KQL has not been executed against a live workspace in this repository's tests. Run each
  query once in Logs ("verify") before relying on it.
* Only the `bucket` custom detail is meaningful for findings; summaries have no bucket name.

## 6. Microsoft Defender for Endpoint (optional, off by default)

`ober watch` can submit confirmed attacker IPs (findings with `action == "block"` and severity
at or above `mde.min_severity`, globally routable single IPs only) as custom network indicators.

Use a separate app registration (least privilege), or the same one if you prefer.

```bash
MDEAPP=$(az ad app create --display-name herr-ober-mde --query appId -o tsv)
az ad sp create --id $MDEAPP

# WindowsDefenderATP ("Microsoft Threat Protection"/MDE API) application id: fc780465-2017-40d4-a0c5-307022471b92 (verify)
MDE_API=fc780465-2017-40d4-a0c5-307022471b92
ROLE=$(az ad sp show --id $MDE_API --query "appRoles[?value=='Ti.ReadWrite.All'].id | [0]" -o tsv)
az ad app permission add --id $MDEAPP --api $MDE_API --api-permissions $ROLE=Role
az ad app permission admin-consent --id $MDEAPP        # needs Global Admin / Privileged Role Admin

MDESECRET=$(az ad app credential reset --id $MDEAPP --append --display-name ober-watch \
            --years 1 --query password -o tsv)
echo "MDE_CLIENT_SECRET=$MDESECRET" >> ~/.ober/login
```

```yaml
mde:
  enabled: true
  tenant_id: <TENANT>
  client_id: <MDEAPP>
  api_base: https://api.securitycenter.microsoft.com
  min_severity: high
  action: Block        # or Audit while evaluating
  expiry_days: 7
```

Requirements and limits (see sources):

* Application permission `Ti.ReadWrite.All` ("Read and write All Indicators").
* Network indicators (IP) are only enforced on devices with Network Protection / the
  appropriate Defender settings enabled; confirm in your tenant ("verify").
* Maximum 15,000 active indicators per tenant. Indicators expire after `expiry_days`.
* CIDR notation is NOT supported for IP indicators, so `ober` submits single IPs only.
* Batch endpoint: up to 500 indicators per call, 30 calls per minute. `ober` stays at
  25 calls/minute.
* `ober` keeps `<watch_data_dir>/mde_state.json` to avoid resubmitting an unexpired IP.
* The documentation's examples use the host `api.security.microsoft.com`; `api_base` defaults to
  `api.securitycenter.microsoft.com` (the regional/legacy MDE API host and token audience).
  Both are documented as MDE API hosts; if token requests are rejected, switch both
  `api_base` and the audience ("verify" for your tenant/cloud).

## Verified facts and sources

| Fact | Source |
|------|--------|
| Ingestion URL `{endpoint}/dataCollectionRules/{dcrImmutableId}/streams/{stream}?api-version=...`; token audience `https://monitor.azure.com`; `Content-Encoding: gzip` is supported; JSON array body; reserved columns include `id` | https://learn.microsoft.com/en-us/azure/azure-monitor/logs/logs-ingestion-api-overview |
| API call limit 1 MB (compressed and uncompressed), field values 64 KB, 2 GB/min and 12,000 requests/min per DCR, honor `Retry-After` | https://learn.microsoft.com/en-us/azure/azure-monitor/fundamentals/service-limits |
| `kind: Direct` DCR needs no DCE; `transformKql: source`; role "Monitoring Metrics Publisher" on the DCR; table schema/ARM/CLI forms | https://learn.microsoft.com/en-us/azure/azure-monitor/logs/create-custom-table |
| `api-version=2023-01-01` for the ingestion call | Microsoft's sample code in https://learn.microsoft.com/en-us/azure/azure-monitor/logs/tutorial-logs-ingestion-code (not re-fetched; "verify") |
| `Microsoft.SecurityInsights/alertRules` ARM schema (entityMappings, customDetails, eventGroupingSettings); API version 2024-09-01 is listed | https://learn.microsoft.com/en-us/azure/templates/microsoft.securityinsights/alertrules |
| Scheduled rule limits: interval and lookback 5 min to 14 days, up to 10 entity mappings, `TimeGenerated` required in results | https://learn.microsoft.com/en-us/azure/sentinel/create-analytics-rules |
| MDE batch endpoint `POST /api/indicators/import`, max 500 per call, 30 calls/min, 15,000 active indicators, no CIDR, `Ti.ReadWrite.All`, body `{"Indicators": [...]}` | https://learn.microsoft.com/en-us/defender-endpoint/api/import-ti-indicators |
| MDE single endpoint `POST /api/indicators`, 100 calls/min and 1,500/hour, fields (`indicatorValue`, `indicatorType`, `action`, `title`, `description`, `severity`, `expirationTime`, `generateAlert`, `application`), no CIDR | https://learn.microsoft.com/en-us/defender-endpoint/api/post-ti-indicator |

Not verified (marked "verify" above): the DCR and tables ARM API versions (`2023-03-11`,
`2022-10-01`), the MDE application ID used by `az ad app permission add`, the
`az sentinel` extension command names, and the KQL against a live workspace.
