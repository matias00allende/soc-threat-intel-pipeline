# DEPLOY.md - Guía de despliegue: Pipeline SOC de boletines de inteligencia de amenazas

---

## 1. Qué es este proyecto

Este proyecto implementa un pipeline automatizado de ingesta y procesamiento de boletines de inteligencia de amenazas (threat intel) recibidos por correo electrónico. Al llegar un boletín al buzón de ingesta, una Lambda lo filtra, normaliza y extrae indicadores de compromiso (IoC): hashes SHA256, direcciones IP, dominios y URLs. Los indicadores nuevos se incorporan a una blocklist centralizada en S3 y se notifica al equipo SOC por correo con el detalle completo.

La plataforma de automatización es n8n corriendo en ECS Fargate en modo queue (main + worker), con RDS PostgreSQL como persistencia y ElastiCache Redis como broker. El flujo de correo inbound usa SES con dos Lambdas Python (guard y normalizer) que desacoplan la recepción del procesamiento. El correo outbound se despacha a través de una cola SQS que consume un stack de mailing externo, eliminando acoplamiento entre n8n y SES.

El pipeline es genérico y reutilizable. Cualquier CSIRT, SOC o equipo de seguridad que reciba boletines de inteligencia de amenazas por correo puede desplegarlo en su propia cuenta AWS adaptando un único archivo de configuración (`config.env`). Las integraciones con Bitdefender GravityZone y MISP son opcionales y están deshabilitadas por defecto.

---

## 2. Prerequisitos

Antes de comenzar, verificar que se cumple cada punto:

### Cuenta y permisos AWS

- [ ] Cuenta AWS con permisos de despliegue CloudFormation sobre los servicios: ECS, ECR, RDS, ElastiCache, Lambda, SES, S3, SQS, WAFv2, ACM, Route53, Secrets Manager, CloudWatch, IAM (CreateRole, AttachRolePolicy), VPC
- [ ] Perfil AWS CLI configurado localmente con acceso a esa cuenta (`aws sts get-caller-identity --profile <perfil>` debe responder)
- [ ] AWS CLI v2 instalado (`aws --version`)

### Red

- [ ] VPC existente con subnets publicas en al menos 2 AZs distintas (para el ALB)
- [ ] VPC existente con subnets privadas en al menos 2 AZs distintas (para ECS, RDS, Redis, Lambdas)
- [ ] NAT Gateway activo en la VPC (o VPC endpoints para ECR, S3, Secrets Manager) - las tasks ECS necesitan salida a Internet para descargar la imagen de DockerHub

### DNS y TLS

- [ ] Dominio propio con zona hospedada activa en Route53 (Hosted Zone ID disponible)
- [ ] Certificado ACM emitido o importado para el hostname n8n (ej: `n8n.ejemplo.cl` o wildcard `*.ejemplo.cl`) - debe estar en la misma región que el stack
- [ ] Si se habilita CloudFront para la blocklist: certificado ACM adicional en `us-east-1` (requisito de CloudFront)

### SES

- [ ] Dominio de mailing verificado en SES (registro de verificación DNS activo)
- [ ] Registro MX del subdominio de mailing apuntando a SES inbound SMTP (ej: `inbound-smtp.us-east-1.amazonaws.com`)
- [ ] Cuenta SES fuera de sandbox - solicitar en Support Center si es producción real; en sandbox solo se pueden enviar correos a direcciones verificadas
- [ ] Identidad remitente (`SOC_EMAIL_FROM`) verificada en SES

### n8n

- [ ] Licencia n8n - la Community Edition no soporta queue mode (main + worker separados). Para escalar con main y worker independientes se requiere licencia Starter o superior. Sin licencia, n8n corre en modo single-process (un solo contenedor ECS), lo que limita la concurrencia.
  - Obtener en: https://app.n8n.io/

### Local

- [ ] Python 3.10 o superior (`python3 --version`)
- [ ] Bash o PowerShell disponible para ejecutar los scripts de validación

---

## 3. Modos de despliegue

| Aspecto | Monolito (`all_in_one_stack`) | Desacoplado (3 stacks) |
|---------|-------------------------------|------------------------|
| Stacks a desplegar | 1 | 3: `mail_ingest_stack` + `n8n_stack_template` + stack mailing externo |
| Dependencias externas | Ninguna - incluye Lambda de envío simplificada | Requiere stack `stack-mailing-plataformas` desplegado previamente |
| Sender de correo | Lambda simplificada incluida en el stack | Lambda avanzada con supresión, tracking y reportería mensual |
| Recomendado para | Inicio rápido, PoC, organizaciones con bajo volumen | Producción con alto volumen, múltiples proyectos compartiendo el stack de mailing |
| Tiempo estimado de despliegue | ~25 minutos | ~45 minutos |
| Portabilidad | Alta - todo en un stack | Media - depende de convenciones del stack externo |

**Nota sobre el modo desacoplado:** el stack externo `stack-mailing-plataformas` expone una cola SQS. n8n publica mensajes en esa cola con el campo `template_id` para que la Lambda de envío seleccione el template HTML correcto. Si se usa el modo monolito, esta integración es interna y transparente.

---

## 4. Configuración - todas las variables

Copiar `config.example.env` a `config.env` y completar todos los valores antes de desplegar.

### Identidad

| Variable | Descripción | Ejemplo | Requerido |
|----------|-------------|---------|-----------|
| `INSTITUTION_NAME` | Nombre completo de la organización (aparece en correos) | `"CSIRT Ejemplo"` | Sí |
| `INSTITUTION_WEBSITE_URL` | URL pública del sitio web (sin barra final) | `https://www.ejemplo.cl` | Sí |
| `INSTITUTION_LOGO_URL` | URL del logo para los correos HTML | `https://www.ejemplo.cl/assets/logo.png` | Sí |
| `SOC_SOURCE_TAG` | Etiqueta SOC para MISP y sistemas de inteligencia | `soc-ejemplo` | Sí |

### Dominios y DNS

| Variable | Descripción | Ejemplo | Requerido |
|----------|-------------|---------|-----------|
| `INSTITUTION_BASE_DOMAIN` | Dominio base con zona en Route53 | `ejemplo.cl` | Sí |
| `N8N_HOSTNAME` | Hostname completo para acceder a n8n | `n8n.ejemplo.cl` | Sí |
| `MAILING_SUBDOMAIN_HOSTNAME` | Subdominio para SES inbound MX | `mailing.ejemplo.cl` | Sí |
| `BLOCKLIST_HOSTNAME` | Hostname CloudFront para la blocklist (opcional) | `hashfw.ejemplo.cl` | No |
| `IOC_HOSTNAME` | Hostname para endpoint IOC público (opcional) | `ioc.ejemplo.ai` | No |

### AWS

| Variable | Descripción | Ejemplo | Requerido |
|----------|-------------|---------|-----------|
| `AWS_ACCOUNT_ID` | ID de cuenta AWS (12 dígitos); auto-detectado si vacío | `123456789012` | Recomendado |
| `AWS_REGION` | Región de despliegue | `us-east-1` | Sí |
| `AWS_PROFILE` | Perfil AWS CLI | `default` | Sí |

### Red - VPC y subnets

| Variable | Descripción | Ejemplo | Requerido |
|----------|-------------|---------|-----------|
| `VPC_ID` | ID de la VPC existente | `vpc-0abc123` | Sí |
| `PUBLIC_SUBNET_1` | Subnet pública AZ1 (para ALB) | `subnet-0abc123` | Sí |
| `PUBLIC_SUBNET_2` | Subnet pública AZ2 (para ALB) | `subnet-0def456` | Sí |
| `PRIVATE_SUBNET_1` | Subnet privada AZ1 (para ECS, RDS, Redis) | `subnet-0ghi789` | Sí |
| `PRIVATE_SUBNET_2` | Subnet privada AZ2 (para ECS, RDS, Redis) | `subnet-0jkl012` | Sí |
| `ADMIN_CIDR` | CIDR autorizado para acceder a n8n | `203.0.113.1/32` | Sí |
| `NAT_CIDR_A` | IP pública /32 del NAT Gateway de la VPC (AZ A). Se usa como `WebhookNatCidrA`. Obténla con `aws ec2 describe-nat-gateways --filter "Name=vpc-id,Values=$VPC_ID" --query 'NatGateways[*].NatGatewayAddresses[0].PublicIp' --output text` | `198.51.100.10/32` | Sí |
| `NAT_CIDR_B` | IP pública /32 del segundo NAT Gateway (AZ B). Se usa como `WebhookNatCidrB`. Si la VPC tiene un solo NAT, repetir el valor de `NAT_CIDR_A` | `198.51.100.11/32` | Sí |

### DNS y TLS

| Variable | Descripción | Ejemplo | Requerido |
|----------|-------------|---------|-----------|
| `N8N_HOSTED_ZONE_ID` | Hosted Zone ID en Route53 para el dominio base | `ZXXXXXXXXXXXX` | Sí |
| `N8N_ACM_CERT_ARN` | ARN del certificado ACM para `N8N_HOSTNAME` | `arn:aws:acm:us-east-1:...` | Sí |
| `BLOCKLIST_ACM_CERT_ARN` | ARN del cert ACM para `BLOCKLIST_HOSTNAME` (CloudFront, us-east-1) | `arn:aws:acm:us-east-1:...` | No |
| `IOC_HOSTED_ZONE_ID` | Hosted Zone ID para `IOC_HOSTNAME` (puede ser distinto al base) | `ZYYYYYYYYYYYYY` | No |
| `IOC_ACM_CERT_ARN` | ARN del cert ACM para `IOC_HOSTNAME` (CloudFront, us-east-1) | `arn:aws:acm:us-east-1:...` | No |

### Correo

| Variable | Descripción | Ejemplo | Requerido |
|----------|-------------|---------|-----------|
| `INGEST_EMAIL_ADDRESS` | Dirección donde llegan los boletines (SES Receipt Rule) | `soc-ingest@mailing.ejemplo.cl` | Sí |
| `ALLOWED_SENDER_DOMAINS` | Dominios remitentes autorizados, separados por coma | `ejemplo.cl,proveedor.com` | Sí |
| `SOC_EMAIL_FROM` | Dirección remitente del SOC Bot (verificada en SES) | `soc-bot@mailing.ejemplo.cl` | Sí |
| `SOC_EMAIL_TO` | Destinatario principal de las alertas | `equipo-soc@ejemplo.cl` | Sí |
| `SOC_EMAIL_CC` | CC de las alertas (separados por coma) | `ciso@ejemplo.cl` | No |

### S3

| Variable | Descripción | Ejemplo | Requerido |
|----------|-------------|---------|-----------|
| `S3_BUCKET_RAW` | Bucket para correos crudos RFC 5322 | `soc-mail-raw-123456789012-us-east-1` | Sí |
| `S3_BUCKET_NORMALIZED` | Bucket para correos normalizados JSON | `soc-mail-normalized-123456789012-us-east-1` | Sí |
| `S3_BUCKET_IOC` | Bucket para blocklist `hash_sha256.txt` | `soc-ioc-blocklist-123456789012-us-east-1` | Sí |
| `S3_BUCKET_ACCESS_LOGS` | Bucket existente para access logs del ALB | `mi-bucket-logs-alb` | Sí |

### SQS

| Variable | Descripción | Ejemplo | Requerido |
|----------|-------------|---------|-----------|
| `SQS_SEND_QUEUE_URL` | URL de la cola SQS para envío de correo | `https://sqs.us-east-1.amazonaws.com/123456789012/soc-send-queue` | Sí |
| `SQS_SEND_QUEUE_ARN` | ARN de la misma cola (para políticas IAM) | `arn:aws:sqs:us-east-1:123456789012:soc-send-queue` | Sí |

### n8n y compute

| Variable | Descripción | Ejemplo | Requerido |
|----------|-------------|---------|-----------|
| `N8N_LICENSE_KEY` | Clave de licencia n8n (vacío = Community Edition, sin queue mode) | `n8n_... ` | Recomendado |
| `N8N_IMAGE_URI` | Imagen Docker de n8n | `n8nio/n8n:latest` | Sí |
| `N8N_WEBHOOK_BASE_URL` | URL base del webhook | `https://n8n.ejemplo.cl/webhook/soc` | Sí |
| `N8N_TASK_CPU` | CPU ECS en unidades (512 = 0.5 vCPU) | `512` | Sí |
| `N8N_TASK_MEMORY` | Memoria ECS en MiB | `2048` | Sí |
| `RDS_INSTANCE_CLASS` | Clase de instancia RDS PostgreSQL | `db.t4g.medium` | Sí |
| `RDS_ALLOCATED_STORAGE` | Almacenamiento inicial RDS en GiB | `50` | Sí |
| `RDS_MAX_ALLOCATED_STORAGE` | Almacenamiento máximo RDS en GiB (autoscaling) | `100` | Sí |
| `REDIS_INSTANCE_CLASS` | Clase de instancia ElastiCache Redis | `cache.t4g.medium` | Sí |

### n8n y compute - avanzado (opcional)

| Variable | Descripción | Ejemplo | Requerido |
|----------|-------------|---------|-----------|
| `N8N_EPHEMERAL_STORAGE_GIB` | Almacenamiento efímero (GiB) de las tareas Fargate de n8n | `40` | No (default 40) |
| `N8N_EXECUTIONS_DATA_PRUNE` | Habilita la poda automática de ejecuciones antiguas | `true` | No (default `true`) |
| `N8N_EXECUTIONS_DATA_MAX_AGE_HOURS` | Antigüedad máxima (horas) de ejecuciones antes de podarse | `336` | No (default `336` = 14 días) |

### Secrets Manager

| Variable | Descripción | Ejemplo | Requerido |
|----------|-------------|---------|-----------|
| `SECRET_NAME_DB` | Nombre del secreto para credenciales RDS | `app/n8n/dbcredentials` | Sí |
| `SECRET_NAME_REDIS` | Nombre del secreto para Redis | `app/n8n/redis` | Sí |
| `SECRET_NAME_N8N` | Nombre del secreto de configuración n8n | `app/n8n/config` | Sí |
| `SECRET_NAME_BITDEFENDER` | Secreto API Bitdefender GravityZone (opcional) | `app/security/bitdefender` | No |
| `SECRET_NAME_MISP` | Secreto API MISP (opcional) | `app/security/misp` | No |

### CloudFront (opcional)

| Variable | Descripción | Ejemplo | Requerido |
|----------|-------------|---------|-----------|
| `CLOUDFRONT_BLOCKLIST_DIST_ID` | ID de distribución CloudFront para blocklist (post-deploy) | `EXXXXXXXXXXXX` | No |
| `CLOUDFRONT_IOC_DIST_ID` | ID de distribución CloudFront para endpoint IOC (post-deploy) | `EYYYYYYYYYYYYY` | No |

### WAF

| Variable | Descripción | Ejemplo | Requerido |
|----------|-------------|---------|-----------|
| `WAF_RATE_LIMIT` | Límite de requests por 5 minutos por IP | `2000` | Sí |
| `WAF_LOG_RETENTION_DAYS` | Retención logs WAF en CloudWatch (días) | `30` | Sí |

### Integraciones opcionales

| Variable | Descripción | Ejemplo | Requerido |
|----------|-------------|---------|-----------|
| `MISP_URL` | URL del servidor MISP | `https://misp.ejemplo.cl` | No |
| `BITDEFENDER_INCIDENTS_V12_URL` | Endpoint Incidents API v1.2 GravityZone (default oficial; no cambiar salvo indicación de Bitdefender) | `https://cloud.gravityzone.bitdefender.com/api/v1.2/jsonrpc/incidents` | No |
| `BITDEFENDER_LICENSING_V10_URL` | Endpoint Licensing API v1.0 GravityZone (default oficial; usado por el reconcile diario) | `https://cloud.gravityzone.bitdefender.com/api/v1.0/jsonrpc/licensing` | No |

---

## 5. Despliegue modo monolito

Este modo despliega todo en un único stack CloudFormation, incluyendo la Lambda simplificada de envío de correo.

```bash
# Paso 1: Preparar configuración
cp config.example.env config.env
# Editar config.env con todos los valores reales
# REGLA: ningún valor debe quedar como PLACEHOLDER al desplegar

# Paso 2: Cargar las variables
set -a && source config.env && set +a

# Paso 3: Crear los secretos en Secrets Manager
aws secretsmanager create-secret \
  --name "${SECRET_NAME_DB}" \
  --secret-string "{\"username\":\"postgres\",\"password\":\"CAMBIA_ESTO\"}" \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}"

aws secretsmanager create-secret \
  --name "${SECRET_NAME_REDIS}" \
  --secret-string "{\"auth_token\":\"CAMBIA_ESTO\"}" \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}"

aws secretsmanager create-secret \
  --name "${SECRET_NAME_N8N}" \
  --secret-string "{\"encryption_key\":\"CAMBIA_ESTO_32_CHARS_MINIMO\"}" \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}"

# Paso 4: Crear el bucket IOC (no es creado por el stack) e inicializarlo
# El bucket S3_BUCKET_IOC NO esta declarado como recurso del all_in_one_stack:
# el stack lo referencia (BitdefenderStateBucketName / HashfwOriginBucketName),
# por lo que debe existir antes del create-stack para evitar errores de
# CloudFront origin / IAM policy.
aws s3 mb "s3://${S3_BUCKET_IOC}" \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}"

aws s3api put-public-access-block \
  --bucket "${S3_BUCKET_IOC}" \
  --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}"

# Inicializar la blocklist con al menos 1000 lineas
# (el workflow valida que la blocklist no haya encogido mas de un 10%)
python3 -c "
import hashlib, random, string
for _ in range(1000):
    h = hashlib.sha256(''.join(random.choices(string.ascii_letters, k=20)).encode()).hexdigest()
    print(f'{h} SHA256_SEED')
" > /tmp/hash_sha256_seed.txt

aws s3 cp /tmp/hash_sha256_seed.txt \
  "s3://${S3_BUCKET_IOC}/hash_sha256.txt" \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}"

# Paso 5: Definir el nombre del stack y verificar las IPs de los NAT Gateways
STACK_NAME="soc-n8n"

# Las IPs publicas de salida son obligatorias para el ALB (SES posts al webhook).
# Si NAT_CIDR_A y NAT_CIDR_B ya estan completos en config.env, este bloque solo
# verifica que coinciden con la realidad de la VPC. Si todavia estan en
# placeholder, ejecutarlo, anotar las IPs, completarlas en config.env (formato
# x.x.x.x/32) y volver a Paso 2 para recargar el archivo.
NAT_IPS=$(aws ec2 describe-nat-gateways \
  --filter "Name=vpc-id,Values=${VPC_ID}" \
  --query 'NatGateways[*].NatGatewayAddresses[0].PublicIp' \
  --output text \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}")
echo "NAT public IPs (VPC): ${NAT_IPS}"
echo "NAT CIDRs (config.env): A=${NAT_CIDR_A} B=${NAT_CIDR_B}"
# Si solo hay un NAT Gateway, repetir el mismo valor en NAT_CIDR_B.

# Paso 6: Crear o reutilizar un bucket S3 para subir el template CFN
# El template all_in_one_stack.json supera el limite de 51 KiB para template-body,
# por lo que CloudFormation exige template-url apuntando a un bucket S3.
# Este bucket es independiente del resto del stack: solo aloja el JSON del template.
# Si ya tienes un bucket de artefactos, exporta TEMPLATE_BUCKET con su nombre y
# salta el `aws s3 mb`.
TEMPLATE_BUCKET="${STACK_NAME}-cfn-templates-${AWS_ACCOUNT_ID}-${AWS_REGION}"
aws s3 mb "s3://${TEMPLATE_BUCKET}" \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}"

aws s3 cp infra/cloudformation/all_in_one_stack.json \
  "s3://${TEMPLATE_BUCKET}/cfn-templates/all_in_one_stack.json" \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}"

# Paso 7: Calcular valores derivados que el stack exige como parametros
DB_MASTER_PASSWORD="CAMBIA_ESTO_DB"
REDIS_PASSWORD="CAMBIA_ESTO_REDIS"
WEBHOOK_BASIC_USER="soc-ingest"
WEBHOOK_BASIC_PASSWORD="CAMBIA_ESTO_WEBHOOK"

# ARN de la identidad SES verificada que enviara los correos del SOC
SES_IDENTITY_ARN="arn:aws:ses:${AWS_REGION}:${AWS_ACCOUNT_ID}:identity/${SOC_EMAIL_FROM}"

# Bucket que sirve el HTML de la notificacion SOC. Por simplicidad puede ser
# el mismo S3_BUCKET_IOC, o un bucket dedicado existente. NO se crea por este
# stack: debe existir antes (paso 11 sube el HTML).
MAIL_TEMPLATE_BUCKET="${S3_BUCKET_IOC}"
MAIL_TEMPLATE_KEY="templates/soc_ioc_notification_v1.html"

# Paso 8: Desplegar el stack
aws cloudformation create-stack \
  --stack-name "${STACK_NAME}" \
  --template-url "https://s3.amazonaws.com/${TEMPLATE_BUCKET}/cfn-templates/all_in_one_stack.json" \
  --parameters \
    ParameterKey=ExistingVpcId,ParameterValue="${VPC_ID}" \
    ParameterKey=ExistingPublicSubnetIds,ParameterValue="${PUBLIC_SUBNET_1}\,${PUBLIC_SUBNET_2}" \
    ParameterKey=ExistingPrivateSubnetIds,ParameterValue="${PRIVATE_SUBNET_1}\,${PRIVATE_SUBNET_2}" \
    ParameterKey=VpcSubnetIds,ParameterValue="${PRIVATE_SUBNET_1}\,${PRIVATE_SUBNET_2}" \
    ParameterKey=WebhookNatCidrA,ParameterValue="${NAT_CIDR_A}" \
    ParameterKey=WebhookNatCidrB,ParameterValue="${NAT_CIDR_B}" \
    ParameterKey=N8nHostedZone,ParameterValue="${N8N_HOSTED_ZONE_ID}" \
    ParameterKey=N8nHostRecordName,ParameterValue="${N8N_HOSTNAME}" \
    ParameterKey=N8nCertArn,ParameterValue="${N8N_ACM_CERT_ARN}" \
    ParameterKey=AllowedIngressCidr,ParameterValue="${ADMIN_CIDR}" \
    ParameterKey=MasterUserPassword,ParameterValue="${DB_MASTER_PASSWORD}" \
    ParameterKey=RedisPassword,ParameterValue="${REDIS_PASSWORD}" \
    ParameterKey=IngestEmailAddress,ParameterValue="${INGEST_EMAIL_ADDRESS}" \
    ParameterKey=AllowedSenderDomains,ParameterValue="${ALLOWED_SENDER_DOMAINS}" \
    ParameterKey=RawBucketName,ParameterValue="${S3_BUCKET_RAW}" \
    ParameterKey=NormalizedBucketName,ParameterValue="${S3_BUCKET_NORMALIZED}" \
    ParameterKey=WebhookUrl,ParameterValue="${N8N_WEBHOOK_BASE_URL}/boletines-csoc-ingest" \
    ParameterKey=WebhookBasicUser,ParameterValue="${WEBHOOK_BASIC_USER}" \
    ParameterKey=WebhookBasicPassword,ParameterValue="${WEBHOOK_BASIC_PASSWORD}" \
    ParameterKey=SesFromAddress,ParameterValue="${SOC_EMAIL_FROM}" \
    ParameterKey=SesIdentityArn,ParameterValue="${SES_IDENTITY_ARN}" \
    ParameterKey=NotificationToAddresses,ParameterValue="${SOC_EMAIL_TO}" \
    ParameterKey=NotificationCcAddresses,ParameterValue="${SOC_EMAIL_CC}" \
    ParameterKey=MunicipalityName,ParameterValue="${INSTITUTION_NAME}" \
    ParameterKey=MunicipalityUrl,ParameterValue="${INSTITUTION_WEBSITE_URL}" \
    ParameterKey=MunicipalityLogoUrl,ParameterValue="${INSTITUTION_LOGO_URL}" \
    ParameterKey=MailTemplateBucketName,ParameterValue="${MAIL_TEMPLATE_BUCKET}" \
    ParameterKey=MailTemplateKey,ParameterValue="${MAIL_TEMPLATE_KEY}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}"

# Paso 9: Esperar a que el stack complete
aws cloudformation wait stack-create-complete \
  --stack-name "${STACK_NAME}" \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}"

# Paso 10: Verificar estado
aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}" \
  --query "Stacks[0].StackStatus"

# Paso 11: Activar el SES Receipt Rule Set (REQUERIDO)
# CloudFormation crea el Rule Set pero NO lo activa. Sin esta activacion SES
# descarta los correos entrantes silenciosamente.
RULE_SET_NAME=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --query 'Stacks[0].Outputs[?OutputKey==`ReceiptRuleSetName`].OutputValue' \
  --output text \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}")

aws ses set-active-receipt-rule-set \
  --rule-set-name "${RULE_SET_NAME}" \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}"

# Paso 12: Subir el template HTML al bucket que sirve la notificacion SOC (REQUERIDO)
# El stack NO crea el bucket MailTemplateBucketName: lo asume existente. Si pasaste
# como MAIL_TEMPLATE_BUCKET un nombre que no existe aun, crearlo antes del primer
# envio. En el ejemplo del Paso 7 reutilizamos S3_BUCKET_IOC para simplificar.
aws s3 cp templates/mailing/soc_ioc_notification_v1.html \
  "s3://${MAIL_TEMPLATE_BUCKET}/${MAIL_TEMPLATE_KEY}" \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}"
```

**Tiempo estimado:** ~25 minutos. Si el stack falla, revisar Events en la consola CloudFormation para identificar el recurso en error.

> **Nota sobre `MailTemplateBucketName`:** este parametro apunta a un bucket S3 que el stack asume existente. No se crea como recurso del template. Si reutilizas `S3_BUCKET_IOC` (creado por el stack), el orden de los Pasos 8-12 funciona porque el bucket ya existe cuando se sube el HTML. Si prefieres un bucket dedicado, crearlo antes del Paso 8 con `aws s3 mb` y pasar su nombre como `MailTemplateBucketName`.

---

## 6. Despliegue modo desacoplado

Este modo despliega tres stacks en orden de dependencia. El stack de mailing externo debe existir previamente.

### Que es el stack de mailing externo

En modo desacoplado, n8n NO envia correos directamente. Publica mensajes en una cola SQS, y otra Lambda (que vive en un stack separado) consume la cola y envia via SES. Esto permite que multiples proyectos compartan la misma flota de envio, separadores de bounce/complaint y reportes mensuales.

Como referencia se ofrece el repositorio interno `mailing-plataformas` (no publico). Cualquier stack que cumpla el siguiente contrato sirve:

- Una cola SQS publica - exporta `SendQueueUrl` y `SendQueueArn` como Outputs CloudFormation.
- Mensajes SQS con cuerpo JSON con las claves siguientes (en este orden de prioridad):
  ```json
  {
    "subject":     "Asunto del correo",
    "html":        "<html>...</html>",
    "text":        "Version texto plano del correo",
    "email_dest":  "destinatario@dominio.cl",
    "email_from":  "remitente@dominio.cl",
    "email_cc":    "cc1@dominio.cl,cc2@dominio.cl",
    "template_id": "soc_ioc_notification_v1"
  }
  ```
  Importante: las claves son `html` y `text` (no `body_html` ni `body_text`).
- Una Lambda consumidora con permisos SES `SendEmail`/`SendRawEmail` sobre la identidad verificada.

Para obtener la URL de la cola una vez desplegado el stack mailing:

```bash
SQS_SEND_QUEUE_URL=$(aws cloudformation describe-stacks \
  --stack-name <NOMBRE_STACK_MAILING> \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}" \
  --query "Stacks[0].Outputs[?OutputKey=='SendQueueUrl'].OutputValue" \
  --output text)
```

Si tu stack mailing usa otro Output Key, ajusta la query. El valor debe quedar en `SQS_SEND_QUEUE_URL` de `config.env`.

### Orden obligatorio

```
1. mail_ingest_stack.json     - SES + Lambdas guard/normalizer + S3 raw/normalized
2. n8n_stack_template.json    - ECS + RDS + Redis + ALB + WAF (referencia outputs del paso 1)
3. stack-mailing-plataformas  - stack externo ya desplegado (proporciona SQS_SEND_QUEUE_URL)
```

### Stack 1 - mail_ingest_stack

```bash
set -a && source config.env && set +a

aws cloudformation create-stack \
  --stack-name "${STACK_NAME}-mail-ingest" \
  --template-body file://infra/cloudformation/mail_ingest_stack.json \
  --parameters \
    ParameterKey=IngestEmailAddress,ParameterValue="${INGEST_EMAIL_ADDRESS}" \
    ParameterKey=AllowedSenderDomains,ParameterValue="${ALLOWED_SENDER_DOMAINS}" \
    ParameterKey=RawBucketName,ParameterValue="${S3_BUCKET_RAW}" \
    ParameterKey=NormalizedBucketName,ParameterValue="${S3_BUCKET_NORMALIZED}" \
    ParameterKey=WebhookUrl,ParameterValue="${N8N_WEBHOOK_BASE_URL}/boletines-csoc-ingest" \
    ParameterKey=WebhookBasicUser,ParameterValue="soc-ingest" \
    ParameterKey=WebhookBasicPassword,ParameterValue="CAMBIA_ESTO" \
    ParameterKey=VpcSubnetIds,ParameterValue="${PRIVATE_SUBNET_1}\,${PRIVATE_SUBNET_2}" \
  --capabilities CAPABILITY_IAM \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}"

aws cloudformation wait stack-create-complete \
  --stack-name "${STACK_NAME}-mail-ingest" \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}"
```

### Stack 2 - n8n_stack_template

Obtener primero el SQS URL del stack de mailing externo:

```bash
SQS_SEND_QUEUE_URL=$(aws cloudformation describe-stacks \
  --stack-name stack-mailing-plataformas \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}" \
  --query "Stacks[0].Outputs[?OutputKey=='SendQueueUrl'].OutputValue" \
  --output text)

# Subir template (supera límite de 51 KB)
aws s3 cp infra/cloudformation/n8n_stack_template.json \
  "s3://${TEMPLATE_BUCKET}/cfn-templates/n8n_stack_template.json" \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}"

aws cloudformation create-stack \
  --stack-name "${STACK_NAME}" \
  --template-url "https://s3.amazonaws.com/${TEMPLATE_BUCKET}/cfn-templates/n8n_stack_template.json" \
  --parameters \
    ParameterKey=ExistingVpcId,ParameterValue="${VPC_ID}" \
    ParameterKey=ExistingPublicSubnetIds,ParameterValue="${PUBLIC_SUBNET_1}\,${PUBLIC_SUBNET_2}" \
    ParameterKey=ExistingPrivateSubnetIds,ParameterValue="${PRIVATE_SUBNET_1}\,${PRIVATE_SUBNET_2}" \
    ParameterKey=WebhookNatCidrA,ParameterValue="${NAT_CIDR_A}" \
    ParameterKey=WebhookNatCidrB,ParameterValue="${NAT_CIDR_B}" \
    ParameterKey=N8nHostedZone,ParameterValue="${N8N_HOSTED_ZONE_ID}" \
    ParameterKey=N8nHostRecordName,ParameterValue="${N8N_HOSTNAME}" \
    ParameterKey=N8nCertArn,ParameterValue="${N8N_ACM_CERT_ARN}" \
    ParameterKey=AllowedIngressCidr,ParameterValue="${ADMIN_CIDR}" \
    ParameterKey=MasterUserPassword,ParameterValue="CAMBIA_ESTO" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}"

aws cloudformation wait stack-create-complete \
  --stack-name "${STACK_NAME}" \
  --region "${AWS_REGION}" \
  --profile "${AWS_PROFILE}"
```

---

## 7. Post-despliegue - configurar n8n

Una vez que el stack está en estado `CREATE_COMPLETE`, completar los siguientes pasos antes de poner el pipeline en operación.

### Paso 1 - Acceder a n8n

Abrir `https://<N8N_HOSTNAME>` desde el CIDR autorizado (`ADMIN_CIDR`). Completar el wizard de primer uso (crear usuario administrador y contraseña).

### Paso 2 - Crear credenciales AWS en n8n

n8n corre en ECS con un IAM Task Role. No se necesitan access keys. Crear la credencial del tipo **AWS** en n8n con los campos `region` configurado y sin access key/secret key (usar IAM Role).

En n8n: **Settings → Credentials → Add credential → AWS** y seleccionar "Use IAM Role" si está disponible, o dejar los campos de key en blanco.

### Paso 3 - Adaptar el workflow con `parametrize_workflow.py`

El workflow exportado contiene referencias a la instancia de origen. El script reemplaza cuentas AWS, nombres de buckets, URLs, direcciones de correo e identificadores de credenciales:

```bash
python3 scripts/parametrize_workflow.py \
  --input workflows/Boletines-CSOC.template.json \
  --output workflows/Boletines-CSOC.adapted.json \
  --config config.env
```

Variables requeridas en `config.env` para este script:

```
AWS_ACCOUNT_ID, AWS_REGION, S3_BUCKET_IOC, S3_BUCKET_NORMALIZED, S3_BUCKET_RAW,
SQS_SEND_QUEUE_URL, SOC_EMAIL_FROM, SOC_EMAIL_TO, SOC_EMAIL_CC,
INSTITUTION_NAME, INSTITUTION_BASE_DOMAIN
```

Variable opcional: `BLOCKLIST_HOSTNAME` (si está vacío, el workflow usa la URL S3 directa).

### Paso 4 - Importar el workflow en n8n

En n8n: **Settings → Import from file** → seleccionar `workflows/Boletines-CSOC.adapted.json`.

### Paso 5 - Importar workflows Bitdefender (opcional)

Solo si se planea integrar con Bitdefender GravityZone. Saltar este paso si no aplica.

Antes de importar, los workflows Bitdefender necesitan que el placeholder `PLACEHOLDER_IOC_BUCKET_NAME` sea reemplazado por `S3_BUCKET_IOC`. Pasarlos por el mismo script:

```bash
python3 scripts/parametrize_workflow.py \
  --input workflows/bitdefender_reconcile_daily.template.json \
  --output workflows/bitdefender_reconcile_daily.adapted.json \
  --config config.env

python3 scripts/parametrize_workflow.py \
  --input workflows/bitdefender_sync_incremental.template.json \
  --output workflows/bitdefender_sync_incremental.adapted.json \
  --config config.env
```

Luego en n8n: **Settings → Import from file** → seleccionar cada `*.adapted.json`. En cada nodo S3 importado, asignar la credencial AWS IAM creada en el Paso 2 (igual que con `Boletines-CSOC`).

Requisitos para que estos workflows funcionen:

- El secreto `${SECRET_NAME_BITDEFENDER}` debe existir en Secrets Manager con campos `apiKey` y `companyId` (ver Seccion 9).
- El stack debe estar desplegado con los parametros `BitdefenderSecretName` y `BitdefenderStateBucketName` no vacios para que las task definitions inyecten las variables `SOC_BITDEFENDER_*`.
- Activar primero `bitdefender_reconcile_daily` en modo manual una vez para sembrar `gravityzone/state/bitdefender_known_present_sha256.txt` en S3, y luego activar `bitdefender_sync_incremental` para operacion recurrente.

### Paso 6 - Reemplazar credential IDs

El script marca los nodos que requieren credenciales con el placeholder `CONFIGURE_AFTER_IMPORT_*`. Abrir cada nodo marcado en el editor de n8n y seleccionar la credencial real creada en el Paso 2. Los nodos afectados son típicamente:

- Nodos AWS S3 (lectura y escritura de blocklist)
- Nodo SQS (publicación del mensaje al equipo SOC)
- Nodo webhook inbound (Basic Auth de ingesta)

### Paso 7 - Activar el workflow

Una vez todos los nodos tienen credenciales asignadas, activar el workflow desde el toggle en la esquina superior derecha del editor.

### Paso 8 - Ajustar el umbral de validacion de la blocklist

El workflow `Boletines-CSOC.template.json` incluye protecciones de calidad antes de escribir cambios sobre la blocklist. Estos parametros viven dentro del codigo JavaScript de los nodos. Adaptarlos a la realidad de la nueva instancia es **obligatorio** - si no se ajustan, el workflow puede quedar bloqueado en cada ejecucion.

**Nodo `Detect New SHA`** (linea con `MIN_EXPECTED_EXISTING_SHA_COUNT`):

- **Que hace:** lanza una excepcion si la blocklist actual descargada tiene menos del valor configurado de hashes (proteccion contra "blocklist truncada"). Por defecto `1000`.
- **Como ajustarlo:** abrir el nodo `Detect New SHA` en el editor de n8n y modificar la linea:
  ```js
  const MIN_EXPECTED_EXISTING_SHA_COUNT = 1000;  // ajustar al volumen real
  ```
- **Recomendacion:** despues del primer despliegue real con datos productivos, fijar este valor al 90% del tamano estable observado en S3.

**Nodo `Extract IoC Input` y nodo IF subsiguiente** (filtro de calidad de input):

- **Que hace:** descarta correos cuyo cuerpo de texto extraido tenga menos de 50 caracteres. Es el unico filtro de "umbral de input" antes de invocar Bedrock.
- **Como ajustarlo:** abrir el nodo `If` (despues de `Extract IoC Input`) y modificar `rightValue: 50` por el tamano minimo deseado.

**Modelo Bedrock y clasificacion:**

- El modelo por defecto es `global.anthropic.claude-haiku-4-5` (configurado en el nodo `AWS Bedrock Chat Model`).
- La salida del LLM se interpreta directamente; **no existe en este workflow un campo `confidence_score` ni un umbral de confianza por porcentaje**. La proteccion contra alucinaciones se hace por validacion estructural posterior (formato hash 64 hex + tag canonico) en el mismo nodo `Detect New SHA`. Si tu institucion requiere un gate de confianza por LLM, implementarlo como nodo `If` adicional consumiendo el JSON estructurado del modelo.

---

## 8. Verificación

### Validar ambiente de mailing

```powershell
./scripts/validate_boletines_mailing_env.ps1 `
  -SqsQueueUrl $env:SQS_SEND_QUEUE_URL `
  -SocBotEmail $env:SOC_EMAIL_FROM `
  -SocDestEmail $env:SOC_EMAIL_TO `
  -AwsProfile $env:AWS_PROFILE `
  -AwsRegion $env:AWS_REGION
```

Este script verifica: cola SQS accesible, identidad SES verificada, permisos IAM correctos.

### Smoke test end-to-end

```powershell
./scripts/validate_boletines_smoke_test.ps1 `
  -SqsQueueUrl $env:SQS_SEND_QUEUE_URL `
  -SocBotEmail $env:SOC_EMAIL_FROM `
  -MailingWorkerLogGroup "/aws/lambda/${STACK_NAME}-soc-mail-sender" `
  -AwsProfile $env:AWS_PROFILE `
  -AwsRegion $env:AWS_REGION
```

Este script inyecta un mensaje de prueba en la cola SQS y verifica que la Lambda de envío lo procesa y que aparece en CloudWatch Logs.

### Verificación manual del flujo completo

Enviar un correo de prueba desde un dominio autorizado (`ALLOWED_SENDER_DOMAINS`) a `INGEST_EMAIL_ADDRESS`. El flujo completo debería producir:

1. Objeto creado en `S3_BUCKET_RAW/raw/inbox/<message_id>`
2. Objeto creado en `S3_BUCKET_NORMALIZED/normalized/<message_id>.json`
3. Ejecución del workflow en n8n visible en **Executions**
4. Correo de notificación recibido en `SOC_EMAIL_TO`

---

## 9. Integraciones opcionales

### Bitdefender GravityZone (opcional)

Los dos workflows `workflows/bitdefender_*.template.json` se distribuyen con el repo y consumen variables de entorno n8n (`SOC_BITDEFENDER_*`) que el stack inyecta automaticamente cuando los parametros `BitdefenderSecretName` y `BitdefenderStateBucketName` estan definidos en el `create-stack` (o `update-stack`).

1. Crear el secreto con el formato exacto que esperan los workflows (claves `apiKey` y `companyId`, ambas como string JSON):
   ```bash
   aws secretsmanager create-secret \
     --name "${SECRET_NAME_BITDEFENDER}" \
     --secret-string '{"apiKey":"CLAVE_API_BITDEFENDER","companyId":"COMPANY_ID_BITDEFENDER"}' \
     --region "${AWS_REGION}" \
     --profile "${AWS_PROFILE}"
   ```

2. Asegurarse de que el stack se desplego con los parametros Bitdefender no vacios. Si se omitieron en el Paso 8, ejecutar `update-stack` agregando:
   ```
   ParameterKey=BitdefenderSecretName,ParameterValue="${SECRET_NAME_BITDEFENDER}"
   ParameterKey=BitdefenderStateBucketName,ParameterValue="${S3_BUCKET_IOC}"
   ```
   Tras el update, las task definitions de n8n main y worker exponen las variables `SOC_BITDEFENDER_API_KEY`, `SOC_BITDEFENDER_COMPANY_ID`, `SOC_BITDEFENDER_INCIDENTS_V12_URL`, `SOC_BITDEFENDER_LICENSING_V10_URL`, `SOC_BITDEFENDER_STATE_BUCKET`, `SOC_BITDEFENDER_ADD_CHUNK_SIZE`, `SOC_BITDEFENDER_ADD_INTERVAL_SECONDS`.

3. Importar los workflows en n8n via Settings -> Import from file:
   - `workflows/bitdefender_reconcile_daily.template.json` - reconciliacion completa (recomendado ejecutar manualmente la primera vez para sembrar el archivo de estado)
   - `workflows/bitdefender_sync_incremental.template.json` - sync incremental cada N minutos

4. En cada nodo S3 importado, asignar la credencial AWS IAM creada en el Post-despliegue (placeholder `CONFIGURE_AFTER_IMPORT_AWS_CRED`). Reemplazar el placeholder `PLACEHOLDER_IOC_BUCKET_NAME` por el valor de `S3_BUCKET_IOC` (manualmente en cada nodo, o pasando el archivo por `scripts/parametrize_workflow.py`).

5. Activar `bitdefender_sync_incremental` para operacion recurrente. Mantener `bitdefender_reconcile_daily` en modo manual.

**Advertencia:** no activar ambos workflows simultaneamente en modo automatico. La reconciliacion completa puede generar un volumen de llamadas API significativo. Los workflows usan los nodos `Code` para invocar JSON-RPC contra GravityZone; no requieren credencial HTTP Header Auth en n8n.

### MISP (opcional)

El workflow principal `Boletines-CSOC.template.json` no incluye un nodo MISP preconfigurado. Para integrarlo:

1. Crear el secreto con el auth key de MISP:
   ```bash
   aws secretsmanager create-secret \
     --name "${SECRET_NAME_MISP}" \
     --secret-string "{\"auth_key\":\"CLAVE_API_MISP\",\"url\":\"${MISP_URL}\"}" \
     --region "${AWS_REGION}" \
     --profile "${AWS_PROFILE}"
   ```

2. En n8n: **Settings -> Credentials -> Add credential -> Header Auth**. Configurar el header `Authorization: <auth_key>` y la URL base del servidor MISP.

3. Editar el workflow `Boletines-CSOC` ya importado y agregar al final un nodo `HTTP Request` que invoque el endpoint MISP `/events/add` con la credencial creada en el paso 2 y el cuerpo JSON con los IoC extraidos del boletin.

### CloudFront para la blocklist

Habilitar la distribución CloudFront para `BLOCKLIST_HOSTNAME` permite que los sistemas de protección consuman la blocklist vía HTTPS desde un CDN sin impacto en los permisos del bucket S3.

1. Desplegar con el parámetro `HashfwOriginBucketName` apuntando a `S3_BUCKET_IOC` y `HashfwDomainName` configurado como `BLOCKLIST_HOSTNAME`.
2. Una vez desplegado, copiar el Distribution ID al campo `CLOUDFRONT_BLOCKLIST_DIST_ID` de `config.env`.
3. El workflow usa la URL `https://<BLOCKLIST_HOSTNAME>/hash_sha256.txt` automáticamente cuando `BLOCKLIST_HOSTNAME` está definido.

**Nota:** el certificado ACM para CloudFront debe estar en `us-east-1` independientemente de la región del resto del stack.

---

## 10. Lecciones operativas y advertencias conocidas

Aprendizajes reales de operar este pipeline en producción, ya incorporados como default seguro en
el template donde correspondía. Se documentan aquí porque son decisiones de configuración o
arquitectura que cada institución debe entender antes de modificarlas.

**Escalar `main` a más de 1 instancia exige `N8N_MULTI_MAIN_SETUP=true`.** El template ya inyecta
esta variable en el contenedor `main` (ver `N8nMainTaskDefinition`). Si se quita o se despliega
`main` con `DesiredCount > 1` sin ella, cada trigger programado (cron/interval de un workflow) se
ejecuta duplicado en paralelo - un síntoma típico es una tasa de error alta e inexplicable en
workflows con schedule trigger, causada por dos ejecuciones simultáneas compitiendo por el mismo
estado.

**`N8N_BLOCK_ENV_ACCESS_IN_NODE=true` bloquea `$env` de forma global, no solo en el nodo Code.**
El nombre de la variable sugiere que solo afecta al nodo Code, pero bloquea el mismo
`WorkflowDataProxy` que usan las expresiones de *cualquier* nodo (Set, If, etc.). Un nodo
Set/Edit Fields intermedio que resuelva `{{$env.MI_SECRETO}}` para "sacarlo" del nodo Code no
sobrevive el flag - falla igual. La alternativa real para aislar secretos de los nodos Code es
reescribir la integración con un nodo `HTTP Request` nativo, con la credencial adjunta a nivel de
nodo (no vía `$env`).

**Al reescribir la integración Bitdefender a `HTTP Request` nativo con autenticación custom:**
usar `genericCredentialType` + `genericAuthType` en la definición de la credencial, no
`predefinedCredentialType` - esta línea de versiones de n8n rechaza ese tipo con un error de
credencial no soportada.

**Fijar la imagen de n8n a un tag explícito, nunca `:latest` en producción.** `:latest` puede
saltar decenas de versiones menores entre despliegues sin aviso, acumulando CVEs conocidos sin
que nadie lo note (revisar el scanner de imágenes del registro que se use). Al actualizar de
versión, validar primero en un entorno efímero aislado: restaurar el snapshot de RDS en una
instancia temporal, usar Security Groups exclusivos sin conectividad a producción, y levantar una
task Fargate ad-hoc con `EXECUTIONS_MODE=regular` para confirmar que los workflows existentes
arrancan sin error de nodo antes de tocar el stack real.

**Editar una credencial AWS en la UI de n8n no la recarga en `main`/`worker` en modo queue.** Los
procesos ECS mantienen la credencial descifrada en memoria desde el arranque; si se cambia desde
la UI mientras los servicios corren, hay que forzar
`aws ecs update-service --cluster <cluster> --service <main|worker> --force-new-deployment` en
ambos servicios y verificar con una ejecución real posterior (ideal: confirmar en CloudTrail que
la nueva identidad es la que realmente invoca la API).

**VPC Interface Endpoints con `PrivateDnsEnabled=true` en una VPC compartida afectan a todos los
consumidores del servicio, no solo a quien crea el endpoint.** Si otro proyecto en la misma VPC
crea un endpoint de este tipo para un servicio que este pipeline también usa (p. ej. SQS), la
resolución DNS de ese servicio cambia para toda la VPC de forma silenciosa, y este pipeline puede
empezar a fallar con timeouts sin que nadie haya tocado su configuración. Evaluar el impacto
cruzado antes de crear un endpoint de este tipo en una VPC compartida con otros proyectos.

---

## 11. Troubleshooting

| Síntoma | Causa probable | Solución |
|---------|----------------|----------|
| Lambda `soc-mail-guard` rechaza todos los correos | `ALLOWED_RECIPIENT` no coincide exactamente con la dirección real del MX configurado en SES | Verificar que la variable de entorno de la Lambda coincide exactamente (incluyendo mayúsculas/minúsculas) con `INGEST_EMAIL_ADDRESS` |
| Nodo AWS en n8n falla con `InvalidClientTokenId` | Credential ID incorrecto post-import - los placeholders `CONFIGURE_AFTER_IMPORT_*` no fueron reemplazados | Abrir cada nodo marcado en el editor, seleccionar la credencial AWS real creada en n8n |
| SES no entrega correos a la Lambda | Receipt Rule inactiva o SES en modo sandbox | Verificar en SES → Email receiving que el Rule Set está activo; si se envía a dirección no verificada en sandbox, solicitar salida del sandbox en Support Center |
| Workflow falla con "Blocklist shrunk" | Bucket `S3_BUCKET_IOC` vacío o con menos de 1000 líneas en `hash_sha256.txt` | Inicializar el archivo con al menos 1000 hashes de prueba (ver Paso 4 de la sección de despliegue) |
| n8n worker no procesa trabajos de la cola | Redis no accesible o credenciales del secreto `SECRET_NAME_REDIS` incorrectas | Verificar en Secrets Manager que el secreto existe y tiene el campo `auth_token`; revisar SG de Redis (solo accesible desde ECS SG) |
| Stack create falla en recurso ECS | Imagen `n8nio/n8n:latest` no accesible desde la VPC | Verificar que la VPC tiene NAT Gateway activo o VPC endpoint para ECR/DockerHub; alternativamente, replicar la imagen en ECR privado y actualizar `N8N_IMAGE_URI` |
| Ejecuciones duplicadas de un workflow con schedule trigger | `main` escalado a más de 1 instancia sin `N8N_MULTI_MAIN_SETUP=true` | Confirmar que la variable está presente en la task definition de `main` (ya viene por defecto en este template); si se desplegó con `--use-previous-template` sobre una versión antigua, actualizar |
| Nodo AWS S3/SQS en n8n falla con `NoSuchKey`/`AccessDenied` justo después de rotar una credencial AWS | `main`/`worker` mantienen la credencial vieja cacheada en memoria | Forzar `--force-new-deployment` en ambos servicios ECS tras cualquier cambio de credencial hecho desde la UI de n8n |
