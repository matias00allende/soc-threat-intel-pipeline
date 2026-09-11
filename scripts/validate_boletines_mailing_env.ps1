param(
  [string]$SqsQueueUrl = "",
  [string]$MailingDomain = "",
  [string]$SocBotEmail = "",
  [string]$CloudAwsEmail = "",
  [string]$NotificacionesEmail = "",
  [string]$AwsProfile = "mfa-session",
  [string]$Region = "us-east-1"
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrEmpty($SqsQueueUrl)) {
    Write-Error "SqsQueueUrl es requerido."
    exit 1
}
if ([string]::IsNullOrEmpty($MailingDomain)) {
    Write-Error "MailingDomain es requerido."
    exit 1
}
if ([string]::IsNullOrEmpty($SocBotEmail)) {
    Write-Error "SocBotEmail es requerido."
    exit 1
}
if ([string]::IsNullOrEmpty($CloudAwsEmail)) {
    Write-Error "CloudAwsEmail es requerido."
    exit 1
}
if ([string]::IsNullOrEmpty($NotificacionesEmail)) {
    Write-Error "NotificacionesEmail es requerido."
    exit 1
}

$AwsAccountId = aws sts get-caller-identity --profile $AwsProfile --query Account --output text
if ([string]::IsNullOrEmpty($AwsAccountId)) {
    Write-Error "No se pudo obtener el Account ID de AWS. Verifica la sesion activa."
    exit 1
}

$Profile = $AwsProfile
$QueueArn = "arn:aws:sqs:${Region}:${AwsAccountId}:stack-mailing-plataformas-send-queue"
$QueueUrl = $SqsQueueUrl
$ConfigSet = 'stack-mailing-plataformas-config-set'
$DomainIdentity = $MailingDomain
$SocBotIdentity = $SocBotEmail
$CloudAwsIdentity = $CloudAwsEmail
$TaskRole = 'stack-ecs-n8n-task-role'
$TaskQueuePolicy = 'soc-n8n-mailing-send-queue-inline'
$N8nCredentialUser = 'cli_imp-n8n-bedrock'
$N8nCredentialUserPolicy = 'soc-n8n-corporate-mail-sqs-inline'
$SendRole = 'stack-mailing-plataformas-lambda-send-role'
$SendPolicy = 'send-policy'
$SuppressionTable = 'soc-ses-suppression-table'

function Get-Json {
  param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
  )

  if ($Args.Count -eq 1) {
    $Args = @($Args[0] -split ' ')
  }

  aws @Args --profile $Profile --region $Region | ConvertFrom-Json
}

$results = [System.Collections.Generic.List[object]]::new()

function Add-Result([string]$Check, [bool]$Ok, [string]$Detail) {
  $results.Add([pscustomobject]@{
    check = $Check
    ok = $Ok
    detail = $Detail
  })
}

$account = Get-Json @('sts', 'get-caller-identity')
Add-Result 'account' ($account.Account -eq $AwsAccountId) "caller=$($account.Arn)"

$sesAccount = Get-Json @('sesv2', 'get-account')
$quotaOk = ($sesAccount.SendQuota.Max24HourSend -eq 50000.0) -and ($sesAccount.SendQuota.MaxSendRate -eq 14.0)
Add-Result 'ses_account' $quotaOk "quota=$($sesAccount.SendQuota.Max24HourSend)/day rate=$($sesAccount.SendQuota.MaxSendRate)/sec status=$($sesAccount.EnforcementStatus)"

$domain = Get-Json @('sesv2', 'get-email-identity', '--email-identity', $DomainIdentity)
$domainOk = $domain.VerifiedForSendingStatus -and ($domain.ConfigurationSetName -eq $ConfigSet)
Add-Result 'ses_domain_identity' $domainOk "verified=$($domain.VerifiedForSendingStatus) configSet=$($domain.ConfigurationSetName)"

$socBot = Get-Json @('sesv2', 'get-email-identity', '--email-identity', $SocBotIdentity)
$socBotOk = $socBot.VerifiedForSendingStatus
Add-Result 'ses_soc_bot_identity' $socBotOk "verifiedForSending=$($socBot.VerifiedForSendingStatus) verificationStatus=$($socBot.VerificationStatus)"

$cloudAws = Get-Json @('sesv2', 'get-email-identity', '--email-identity', $CloudAwsIdentity)
$cloudAwsOk = $cloudAws.VerifiedForSendingStatus
Add-Result 'ses_cloud_aws_identity' $cloudAwsOk "verifiedForSending=$($cloudAws.VerifiedForSendingStatus)"

$cfg = Get-Json @('sesv2', 'get-configuration-set', '--configuration-set-name', $ConfigSet)
$cfgOk = $cfg.SendingOptions.SendingEnabled -and $cfg.ReputationOptions.ReputationMetricsEnabled
Add-Result 'ses_configuration_set' $cfgOk "sendingEnabled=$($cfg.SendingOptions.SendingEnabled) reputationMetrics=$($cfg.ReputationOptions.ReputationMetricsEnabled)"

$queue = Get-Json @('sqs', 'get-queue-attributes', '--queue-url', $QueueUrl, '--attribute-names', 'All')
$queueOk = ($queue.Attributes.QueueArn -eq $QueueArn) -and $queue.Attributes.RedrivePolicy
Add-Result 'sqs_queue' $queueOk "arn=$($queue.Attributes.QueueArn) visibility=$($queue.Attributes.VisibilityTimeout) redrive=$($queue.Attributes.RedrivePolicy)"

$mapping = Get-Json @('lambda', 'list-event-source-mappings', '--function-name', 'stack-mailing-plataformas-send-worker')
$mappingRow = $mapping.EventSourceMappings | Where-Object { $_.EventSourceArn -eq $QueueArn }
$mappingOk = $null -ne $mappingRow -and $mappingRow.State -eq 'Enabled'
Add-Result 'send_worker_mapping' $mappingOk "state=$($mappingRow.State) batchSize=$($mappingRow.BatchSize)"

$fn = Get-Json @('lambda', 'get-function-configuration', '--function-name', 'stack-mailing-plataformas-send-worker')
$fnOk = ($fn.Environment.Variables.soc_SES_REGION -eq $Region) -and
        ($fn.Environment.Variables.DEFAULT_FROM_EMAIL -eq $NotificacionesEmail) -and
        ($fn.Environment.Variables.CONFIGURATION_SET -eq $ConfigSet) -and
        ($fn.Environment.Variables.soc_SUPPRESSION_TABLE -eq $SuppressionTable)
Add-Result 'send_worker_env' $fnOk "region=$($fn.Environment.Variables.soc_SES_REGION) defaultFrom=$($fn.Environment.Variables.DEFAULT_FROM_EMAIL) configSet=$($fn.Environment.Variables.CONFIGURATION_SET)"

$sendRolePolicy = Get-Json @('iam', 'get-role-policy', '--role-name', $SendRole, '--policy-name', $SendPolicy)
$sendResources = @($sendRolePolicy.PolicyDocument.Statement | Where-Object { $_.Action -contains 'ses:SendEmail' } | ForEach-Object { $_.Resource }) | ForEach-Object { $_ }
$sendRoleOk = $sendResources -contains "arn:aws:ses:${Region}:${AwsAccountId}:identity/${SocBotEmail}"
Add-Result 'send_worker_policy' $sendRoleOk "resources=$($sendResources -join ', ')"

$taskQueue = Get-Json @('iam', 'get-role-policy', '--role-name', $TaskRole, '--policy-name', $TaskQueuePolicy)
$taskActions = @($taskQueue.PolicyDocument.Statement | ForEach-Object { $_.Action }) | ForEach-Object { $_ }
$taskResources = @($taskQueue.PolicyDocument.Statement | ForEach-Object { $_.Resource }) | ForEach-Object { $_ }
$taskOk = ($taskActions -contains 'sqs:SendMessage') -and
          ($taskActions -contains 'sqs:ListQueues') -and
          ($taskResources -contains $QueueArn) -and
          ($taskResources -contains '*')
Add-Result 'n8n_task_role_queue_send' $taskOk "actions=$($taskActions -join ', ') resource=$($taskResources -join ', ')"

$credentialUserPolicies = aws iam list-user-policies --user-name $N8nCredentialUser --profile $Profile | ConvertFrom-Json
$credentialUserOk = $credentialUserPolicies.PolicyNames -contains $N8nCredentialUserPolicy
Add-Result 'n8n_credential_user_queue_send' $credentialUserOk "user=$N8nCredentialUser inlinePolicies=$($credentialUserPolicies.PolicyNames -join ', ')"

$suppression = Get-Json @('dynamodb', 'describe-table', '--table-name', $SuppressionTable)
$suppressionOk = $suppression.Table.TableStatus -eq 'ACTIVE'
Add-Result 'suppression_table' $suppressionOk "status=$($suppression.Table.TableStatus) items=$($suppression.Table.ItemCount)"

$results | ConvertTo-Json -Depth 6
