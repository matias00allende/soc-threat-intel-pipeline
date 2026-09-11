param(
  [int]$LookbackMinutes = 15,
  [string]$SqsQueueUrl = "",
  [string]$SocBotEmail = "",
  [string]$MailingWorkerLogGroup = "",
  [string]$AwsProfile = "mfa-session",
  [string]$Region = "us-east-1"
)

$ErrorActionPreference = 'Stop'

foreach ($reqParam in @('SqsQueueUrl', 'SocBotEmail', 'MailingWorkerLogGroup')) {
    if ([string]::IsNullOrEmpty((Get-Variable $reqParam).Value)) {
        Write-Error "$reqParam es requerido."
        exit 1
    }
}

$Profile = $AwsProfile
$QueueUrl = $SqsQueueUrl
$N8nLogGroup = '/ecs/stack-ecs-n8n'
$WorkerLogGroup = $MailingWorkerLogGroup
$ExpectedFrom = $SocBotEmail

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
$start = [DateTimeOffset]::UtcNow.AddMinutes(-$LookbackMinutes).ToUnixTimeMilliseconds()

$queue = aws sqs get-queue-attributes `
  --queue-url $QueueUrl `
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible ApproximateNumberOfMessagesDelayed `
  --profile $Profile `
  --region $Region | ConvertFrom-Json

$visible = if ($null -ne $queue.Attributes.ApproximateNumberOfMessages) { [int]$queue.Attributes.ApproximateNumberOfMessages } else { 0 }
$inFlight = if ($null -ne $queue.Attributes.ApproximateNumberOfMessagesNotVisible) { [int]$queue.Attributes.ApproximateNumberOfMessagesNotVisible } else { 0 }
$delayed = if ($null -ne $queue.Attributes.ApproximateNumberOfMessagesDelayed) { [int]$queue.Attributes.ApproximateNumberOfMessagesDelayed } else { 0 }
$queueOk = ($visible -eq 0) -and ($inFlight -eq 0) -and ($delayed -eq 0)
Add-Result $results 'queue_backlog' $queueOk "visible=$visible inFlight=$inFlight delayed=$delayed"

$n8nForbidden = aws logs filter-log-events `
  --log-group-name $N8nLogGroup `
  --profile $Profile `
  --region $Region `
  --start-time $start `
  --filter-pattern 'Forbidden' | ConvertFrom-Json
Add-Result $results 'n8n_forbidden_recent' (@($n8nForbidden.events).Count -eq 0) "lookbackMinutes=$LookbackMinutes count=$(@($n8nForbidden.events).Count)"

$n8nDynamic = aws logs filter-log-events `
  --log-group-name $N8nLogGroup `
  --profile $Profile `
  --region $Region `
  --start-time $start `
  --filter-pattern 'Node type does not have method defined' | ConvertFrom-Json
Add-Result $results 'n8n_dynamic_options_recent' (@($n8nDynamic.events).Count -eq 0) "lookbackMinutes=$LookbackMinutes count=$(@($n8nDynamic.events).Count)"

$workerStreams = aws logs describe-log-streams `
  --log-group-name $WorkerLogGroup `
  --order-by LastEventTime `
  --descending `
  --max-items 10 `
  --profile $Profile `
  --region $Region | ConvertFrom-Json

$workerEvents = @()
foreach ($stream in @($workerStreams.logStreams)) {
  $streamEvents = aws logs get-log-events `
    --log-group-name $WorkerLogGroup `
    --log-stream-name $stream.logStreamName `
    --start-time $start `
    --limit 100 `
    --profile $Profile `
    --region $Region | ConvertFrom-Json
  $workerEvents += @($streamEvents.events)
}

$workerSocBot = @($workerEvents | Where-Object {
  $_.message -match '"actionTaken": "send"' -and $_.message -match [regex]::Escape($ExpectedFrom)
})
Add-Result $results 'worker_send_soc_bot_recent' (@($workerSocBot).Count -gt 0) "lookbackMinutes=$LookbackMinutes count=$(@($workerSocBot).Count)"

$workerErrors = @($workerEvents | Where-Object { $_.message -match '\[ERROR\]' -or $_.message -match '\bERROR\b' })
Add-Result $results 'worker_error_recent' (@($workerErrors).Count -eq 0) "lookbackMinutes=$LookbackMinutes count=$(@($workerErrors).Count)"

$results | ConvertTo-Json -Depth 5
