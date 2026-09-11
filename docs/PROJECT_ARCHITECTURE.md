# PROJECT_ARCHITECTURE.md
# Arquitectura lógica y técnica - SOC Threat Intel Pipeline

**Última actualización:** 2026-04-09

---

## DIAGRAMA GENERAL

```
Internet (ADMIN_CIDR)
        │
        ▼
  [Route53 DNS]
  n8n.YOURDOMAIN → ALB
        │
        ▼
  [WAFv2 REGIONAL] - [STACK_NAME]-waf
  • AllowList IP
  • RateLimit (2000 req/5min)
  • AWS Managed Common Rules
  • AWS Managed Bad Input Rules
        │
        ▼
  [ALB] - [STACK_NAME]-alb
  • HTTP:80 → redirect HTTPS
  • HTTPS:443 → forward a ECS
  • ACM certificado n8n.YOURDOMAIN
        │
        ▼
  ┌─────────────────────────────────┐
  │         ECS Fargate Cluster     │
  │      [STACK_NAME]-cluster       │
  │                                 │
  │  [n8n-main] ←→ [n8n-worker]    │
  │  Task: n8n-main:N              │
  │  Task: n8n-worker:N            │
  │  CPU: 512 / Mem: 2048 MiB      │
  │  Image: n8nio/n8n:latest       │
  └──────────┬────────┬────────────┘
             │        │
       ┌─────┘        └──────┐
       ▼                     ▼
  [RDS PostgreSQL]    [ElastiCache Redis]
  [STACK_NAME]-pg-db  [STACK_NAME]-redis
  db.t4g.medium       cache.t4g.medium
  Multi-AZ            Multi-AZ, 2 nodos
  16.10               Encrypted at-rest+in-transit
       │
       ▼
  [S3] [STACK_NAME]-n8n-bin
  Binarios y datos n8n
```

---

## DIAGRAMA FLUJO DE INGESTA DE BOLETINES

```
Proveedor externo                    Infraestructura AWS
(threat-intel-provider.com)
        │
        │ correo a soc-ingest@mailing.YOURDOMAIN
        ▼
  [SES Inbound Mail]
  mailing.YOURDOMAIN → MX → SES inbound SMTP
        │
        │ Receipt Rule
        ▼
  [Lambda soc-mail-guard]
  • Valida destinatario
  • Valida dominio remitente
  • Verifica verdicts spam/virus/SPF/DKIM
  → STOP_RULE_SET (rechaza) o CONTINUE (acepta)
        │ CONTINUE
        ▼
  [S3 raw] S3_BUCKET_RAW
  raw/inbox/<message_id>
        │
        │ S3 event trigger
        ▼
  [Lambda soc-mail-normalizer]
  • Parsea multipart RFC 5322
  • Extrae text+HTML+attachments
  • Extrae IoC preliminares (URLs, IPs, SHA256)
  • Publica en S3 normalized
  • Marca dispatched (anti-duplicados)
  • POST webhook n8n
        │
        │ HTTP POST + token auth
        ▼
  https://n8n.YOURDOMAIN/webhook/soc/boletines-csoc-ingest
        │
        ▼ (workflow boletines_csoc)
  [n8n Worker]
  • Recibe JSON normalizado
  • Parse IoC definitivos
  • Llama Amazon Bedrock (Claude) para clasificación
  • Lee blocklist actual de S3
  • Detecta nuevos SHA256
  • Actualiza hash_sha256.txt en S3
  • Construye payload corporativo:
    { template_id: "soc_ioc_notification_v1",
      template_data: {...} }
  • Publica en SQS:
    SQS_SEND_QUEUE
        │
        │ (stack externo, Lambda separada)
        ▼
  [stack-mailing-plataformas-send-worker]
  • Consume SQS
  • Reconoce template_id
  • Renderiza HTML desde soc_ioc_notification_v1.html
  • Envía por SES desde SOC_EMAIL_FROM
        │
        ▼
  Correo al equipo SOC
  (Branding)
```

---

## DIAGRAMA SINCRONIZACIÓN BITDEFENDER

```
[n8n Workflow] (manual, active=false)
bitdefender_reconcile_daily / bitdefender_sync_incremental
        │
        │ lee hash_sha256.txt de S3 blocklist
        │ compara con GravityZone
        ▼
  [Bitdefender GravityZone API]
  https://cloud.gravityzone.bitdefender.com/api/v1.2/jsonrpc/incidents
  Autenticación: API key desde Secrets Manager (parametro BitdefenderSecretName, ej: STACK_NAME/bitdefendergravityzone)
        │
        ▼
  GravityZone actualiza blocklist en endpoints

MISP externo (opcional):
  misp.YOURDOMAIN
```

---

## COMPONENTES

### Capa Edge

| Componente | Tipo | Rol |
|-----------|------|-----|
| Route53 | AWS Managed | DNS: n8n.YOURDOMAIN → ALB |
| ACM | AWS Managed | Certificado TLS |
| WAFv2 (x2) | AWS Managed | Filtrado HTTP malicioso |
| ALB | AWS Managed | Terminación TLS, routing, health checks |

### Capa Compute

| Componente | Tipo | Rol |
|-----------|------|-----|
| ECS Fargate Cluster | AWS Managed | Orquestador de contenedores |
| n8n-main service | ECS Service | UI n8n + coordinación workflows |
| n8n-worker service | ECS Service | Ejecución asíncrona de workflows |
| Lambda soc-mail-guard | AWS Lambda (Python) | Filtrado correo inbound |
| Lambda soc-mail-normalizer | AWS Lambda (Python) | Normalización JSON + despacho webhook |

### Capa Datos

| Componente | Tipo | Rol |
|-----------|------|-----|
| RDS PostgreSQL 16.10 | AWS Managed | Persistencia n8n (workflows, ejecuciones, credentials) |
| ElastiCache Redis | AWS Managed | Broker cola main↔worker |
| S3 stack-ecs-n8n-n8n-bin | S3 | Binarios y almacenamiento n8n |
| S3 S3_BUCKET_RAW | S3 | Correos crudos inbound |
| S3 S3_BUCKET_NORMALIZED | S3 | Correos normalizados JSON |
| S3 S3_BUCKET_IOC | S3 | Blocklist hash_sha256.txt |

### Capa Identidad y Secretos

| Componente | Tipo | Rol |
|-----------|------|-----|
| IAM Task Role | IAM Role | Permisos runtime ECS (Bedrock, S3, SQS, Secrets Manager) |
| IAM Execution Role | IAM Role | Permisos plano de control ECS (ECR, CloudWatch, Secrets Manager) |
| Secrets Manager (x5) | AWS Managed | Credenciales DB, Redis, n8n, Bitdefender |

### Capa Integración

| Componente | Tipo | Rol |
|-----------|------|-----|
| Amazon Bedrock | AWS Managed | LLM para clasificación IoC |
| SQS send-queue | AWS SQS | Cola desacoplada para envío correo |
| SES mailing.YOURDOMAIN | AWS SES | Ingesta y envío correo |

### Capa Observabilidad

| Componente | Tipo | Rol |
|-----------|------|-----|
| CloudWatch Logs | AWS Managed | Logs ECS, Lambda, WAF |
| CloudWatch Alarms | AWS Managed | Alertas AutoScaling |
| CloudTrail | AWS Managed | Auditoría API |
| AWS Config | AWS Managed | Conformidad recursos |
| GuardDuty | AWS Managed | Detección amenazas |
| Access Analyzer | AWS Managed | Análisis accesos IAM |

---

## FLUJOS DE RED

### Acceso usuario (HTTPS)
```
Client:443 → Route53 → ALB → WAF → ECS Task :5678
```

### Conexión n8n a datos
```
ECS Task → RDS :5432 (SG: solo desde ECS SG)
ECS Task → Redis :6379 (SG: solo desde ECS SG)
ECS Task → S3 :443 (via VPC endpoint o NAT)
```

### Flujo correo inbound
```
Internet → SES inbound → Lambda guard → S3 raw → Lambda normalizer → n8n webhook
```

### Flujo correo outbound
```
n8n → SQS → send-worker Lambda → SES → destinatarios corporativos
```

---

## SEGURIDAD EN LA ARQUITECTURA

### Controles de perímetro
- WAFv2 con reglas managed + IP allowlist (ADMIN_CIDR)
- ALB con SG restringido al WAF
- No hay puertos de administración expuestos a Internet

### Controles de red
- Subnets privadas para ECS, RDS y Redis
- Security Groups por componente con least privilege
- Task SG: :5678 solo desde ALB SG
- RDS SG: :5432 solo desde ECS SG
- Redis SG: :6379 solo desde ECS SG

### Controles de datos
- RDS: cifrado at-rest (AES-256), in-transit (TLS - pendiente habilitación completa)
- Redis: cifrado at-rest + in-transit
- S3: SSE-S3 (AES256), versionado, public access block
- Secrets Manager: cifrado por AWS KMS managed

### Controles de identidad
- ECS usa IAM Task Role (no credenciales de instancia)
- Credenciales n8n vía Secrets Manager (no hardcoded en task definition)
- No hay usuarios IAM con acceso programático para esta aplicación (solo roles)

---

## DECISIONES ARQUITECTÓNICAS CLAVE

| Decisión | Rationale |
|---------|-----------|
| ECS Fargate mode queue | Escalabilidad independiente main/worker sin gestión de servidores |
| Redis como broker | Requerimiento nativo de n8n para mode queue |
| S3 para binarios n8n | Persistencia independiente del ciclo de vida del contenedor |
| SES+Lambda para correo inbound | Desacoplamiento de la plataforma n8n del filtrado de correo |
| SQS para correo outbound | Desacoplamiento temporal, resiliencia, separación de responsabilidades |
| VPC compartida | Costo reducido, red preexistente con acceso a Internet |
| Bedrock para IoC | Clasificación contextual que supera regex fijo |

Ver `docs/PROJECT_ADR.md` para registros completos.
