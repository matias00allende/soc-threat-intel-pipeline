param(
  [string]$SqsQueueUrl = "",
  [string]$SocBotEmail = "",
  [string]$AwsProfile = "mfa-session",
  [string]$Region = "us-east-1"
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrEmpty($SqsQueueUrl)) {
    Write-Error "SqsQueueUrl es requerido."
    exit 1
}
if ([string]::IsNullOrEmpty($SocBotEmail)) {
    Write-Error "SocBotEmail es requerido."
    exit 1
}

$RepoRoot = Split-Path $PSScriptRoot -Parent
$WorkflowPath = Join-Path $RepoRoot 'Workflows\boletines_csoc.json'
$Profile = $AwsProfile
$LogGroup = '/ecs/stack-ecs-n8n'
$QueueUrl = $SqsQueueUrl
$ExpectedFrom = $SocBotEmail
$CredentialName = 'AWS (IAM) account'
$LookbackMinutes = 5

function Add-Result {
  param(
    [System.Collections.Generic.List[object]]$Results,
    [string]$Check,
    [bool]$Ok,
    [string]$Detail
  )

  $Results.Add([pscustomobject]@{
    check = $Check
    ok = $Ok
    detail = $Detail
  })
}

$results = [System.Collections.Generic.List[object]]::new()
$wf = Get-Content -Raw -Path $WorkflowPath | ConvertFrom-Json

$awsSesNodes = @($wf.nodes | Where-Object type -eq 'n8n-nodes-base.awsSes')
$awsSqsNodes = @($wf.nodes | Where-Object type -eq 'n8n-nodes-base.awsSqs')
$queueNode = $wf.nodes | Where-Object name -eq 'Corporate Mail Queue'
$buildNode = $wf.nodes | Where-Object name -eq 'Build Corporate Mail Payload'

Add-Result $results 'workflow_active' ([bool]$wf.active) "active=$($wf.active)"
Add-Result $results 'workflow_transport_nodes' (($awsSesNodes.Count -eq 0) -and ($awsSqsNodes.Count -eq 1)) "awsSes=$($awsSesNodes.Count) awsSqs=$($awsSqsNodes.Count)"

$queueNodeOk = $null -ne $queueNode -and
               $queueNode.parameters.operation -eq 'sendMessage' -and
               $queueNode.parameters.queue -eq $QueueUrl -and
               $queueNode.credentials.aws.name -eq $CredentialName
Add-Result $results 'workflow_queue_node' $queueNodeOk "operation=$($queueNode.parameters.operation) queue=$($queueNode.parameters.queue) credential=$($queueNode.credentials.aws.name)"

$buildFromOk = $null -ne $buildNode -and
               ($buildNode.parameters.jsCode -match [regex]::Escape($ExpectedFrom))
Add-Result $results 'workflow_from_payload' $buildFromOk "expectedFrom=$ExpectedFrom"

$mergeTargets = @($wf.connections.'Merge Actions'.main[0] | ForEach-Object node) + @($wf.connections.'Merge Actions'.main[1] | ForEach-Object node)
$buildTargets = @($wf.connections.'Build Corporate Mail Payload'.main[0] | ForEach-Object node)
$connectionOk = ($mergeTargets -contains 'Build Corporate Mail Payload') -and
                ($mergeTargets -contains 'Write Blocklist') -and
                ($buildTargets -contains 'Corporate Mail Queue')
Add-Result $results 'workflow_connections' $connectionOk "mergeTargets=$($mergeTargets -join ', ') buildTargets=$($buildTargets -join ', ')"

$start = [DateTimeOffset]::UtcNow.AddMinutes(-$LookbackMinutes).ToUnixTimeMilliseconds()
$forbidden = aws logs filter-log-events --log-group-name $LogGroup --profile $Profile --region $Region --start-time $start --filter-pattern 'Forbidden' | ConvertFrom-Json
$nodeType = aws logs filter-log-events --log-group-name $LogGroup --profile $Profile --region $Region --start-time $start --filter-pattern 'Node type does not have method defined' | ConvertFrom-Json

Add-Result $results 'recent_forbidden_logs' (@($forbidden.events).Count -eq 0) "lookbackMinutes=$LookbackMinutes count=$(@($forbidden.events).Count)"
Add-Result $results 'recent_dynamic_option_logs' (@($nodeType.events).Count -eq 0) "lookbackMinutes=$LookbackMinutes count=$(@($nodeType.events).Count)"

$results | ConvertTo-Json -Depth 5
