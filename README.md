# Pipeline SOC - Boletines de inteligencia de amenazas

Automatiza la ingesta de boletines de ciberseguridad, extrae indicadores de compromiso (IoC),
actualiza listas de bloqueo en el firewall y notifica al equipo por correo, todo en menos de
cinco minutos desde la recepción del boletín.

---

## Qué hace

1. Recibe boletines de ciberseguridad por correo electrónico (SES inbound o SMTP local).
2. Normaliza el contenido y extrae hashes SHA256.
3. Actualiza el archivo de blocklist (`hash_sha256.txt`) en almacenamiento.
4. Sincroniza los IoC con Bitdefender GravityZone via dos workflows opcionales incluidos: reconciliación diaria completa (`bitdefender_reconcile_daily`) y sync incremental (`bitdefender_sync_incremental`).
5. Permite publicar eventos en un servidor MISP propio configurando un nodo HTTP del workflow principal con las credenciales del servidor (opcional).
6. Envía una notificación al equipo SOC con el detalle de los IoC procesados.

Cada paso es una función Lambda o un nodo n8n; el pipeline completo es auditable,
extensible y desplegable en AWS o en infraestructura propia con Docker Compose.

---

## Modos de despliegue

| Modo | Descripción | Cuándo usarlo |
|------|-------------|---------------|
| **Monolito AWS** | Un solo stack CloudFormation (`all_in_one_stack.json`) | Despliegue rápido, sin dependencias externas |
| **Desacoplado AWS** | Tres stacks separados: mail ingest, n8n y mailing | Entornos que ya tienen stack de correo propio |
| **On-premise** | Docker Compose con PostgreSQL, Redis y n8n | Sin dependencia de nube; red corporativa cerrada |

Ver [`DEPLOY.md`](DEPLOY.md) para instrucciones completas de cada modo. La sección
["Lecciones operativas y advertencias conocidas"](DEPLOY.md#10-lecciones-operativas-y-advertencias-conocidas)
resume gotchas reales encontrados en producción (multi-main, alcance de
`N8N_BLOCK_ENV_ACCESS_IN_NODE`, upgrades de imagen, VPC endpoints en redes compartidas) - revisarla
antes de personalizar el stack.

---

## Estructura del repositorio

```
.
- config.example.env                     # Plantilla de configuración - copiar a config.env
- DEPLOY.md                              # Guía de despliegue completa (AWS y on-premise)
- infra/
  - cloudformation/
    - n8n_stack_template.json            # Stack n8n: ECS + RDS + Redis + ALB + WAF
    - mail_ingest_stack.json             # Stack ingesta: SES + Lambda guard/normalizer + S3
    - all_in_one_stack.json              # Stack monolito: todo en uno
    - _build_all_in_one.py               # Script de construcción del monolito
  - config/
    - stack_parameters.example.json      # Ejemplo de parámetros CloudFormation
  - mail_ingest/                         # Artefactos de configuración SES/Lambda
  - policies/                            # Políticas IAM de referencia
- src/
  - lambdas/
    - soc_mail_guard/                    # Lambda: filtra correos entrantes
    - soc_mail_normalizer/               # Lambda: normaliza a JSON estructurado
- workflows/
  - Boletines-CSOC.template.json                # Workflow principal de ingesta y procesamiento
  - bitdefender_reconcile_daily.template.json   # Workflow opcional: reconciliación diaria GravityZone
  - bitdefender_sync_incremental.template.json  # Workflow opcional: sync incremental GravityZone
- scripts/
  - parametrize_workflow.py              # Adapta workflows a una nueva institución
  - validate_boletines_post_import.ps1   # Valida workflow tras importar en n8n
  - validate_boletines_mailing_env.ps1   # Valida entorno de correo
  - validate_boletines_smoke_test.ps1    # Smoke test end-to-end
- templates/
  - mailing/
    - soc_ioc_notification_v1.html       # Template HTML de notificación SOC
```

---

## Inicio rápido - AWS (monolito)

```bash
# 1. Copiar y completar la configuración
cp config.example.env config.env
# Editar config.env con los valores de la institución

# 2. Subir el template a S3 (supera 51 KB)
aws s3 cp infra/cloudformation/all_in_one_stack.json \
  s3://<BUCKET>/cfn-templates/all_in_one_stack.json \
  --profile <PROFILE>

# 3. Desplegar
aws cloudformation create-stack \
  --stack-name soc-boletin-pipeline \
  --template-url https://<BUCKET>.s3.<REGION>.amazonaws.com/cfn-templates/all_in_one_stack.json \
  --parameters file://infra/config/stack_parameters.json \
  --capabilities CAPABILITY_NAMED_IAM \
  --profile <PROFILE>
```

---

## Inicio rápido - On-premise (Docker Compose)

### Prerrequisitos

- Servidor Linux (Ubuntu 22.04+, Debian 12 o RHEL 9)
- Docker Engine 24.x y Docker Compose v2
- FQDN con certificado TLS (Let's Encrypt o CA interna)
- 4 vCPU / 8 GB RAM / 50 GB SSD mínimo

### Instalación

```bash
# Dependencias base (Ubuntu/Debian)
sudo apt update && sudo apt install -y docker.io docker-compose-v2 nginx certbot
sudo usermod -aG docker $USER

# Estructura de directorios
sudo mkdir -p /opt/soc-pipeline/{n8n_data,postgres_data,redis_data,blocklists,nginx/conf.d,nginx/certs}
sudo chown -R $USER:$USER /opt/soc-pipeline
cd /opt/soc-pipeline
```

### `docker-compose.yml`

```yaml
services:

  postgres:
    image: postgres:16
    restart: always
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    volumes:
      - ./postgres_data:/var/lib/postgresql/data
    networks: [internal]

  redis:
    image: redis:7-alpine
    restart: always
    command: ["redis-server", "--requirepass", "${REDIS_PASSWORD}"]
    volumes:
      - ./redis_data:/data
    networks: [internal]

  n8n:
    image: n8nio/n8n:latest
    restart: always
    environment:
      DB_TYPE: postgresdb
      DB_POSTGRESDB_HOST: postgres
      DB_POSTGRESDB_PORT: 5432
      DB_POSTGRESDB_DATABASE: ${POSTGRES_DB}
      DB_POSTGRESDB_USER: ${POSTGRES_USER}
      DB_POSTGRESDB_PASSWORD: ${POSTGRES_PASSWORD}
      N8N_ENCRYPTION_KEY: ${N8N_ENCRYPTION_KEY}
      WEBHOOK_URL: https://${N8N_HOSTNAME}
      EXECUTIONS_PROCESS: main
      QUEUE_BULL_REDIS_HOST: redis
      QUEUE_BULL_REDIS_PASSWORD: ${REDIS_PASSWORD}
    volumes:
      - ./n8n_data:/home/node/.n8n
    depends_on: [postgres, redis]
    networks: [internal]

  nginx:
    image: nginx:alpine
    restart: always
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/conf.d:/etc/nginx/conf.d:ro
      - ./nginx/certs:/etc/nginx/certs:ro
    depends_on: [n8n]
    networks: [internal, external]

networks:
  internal:
    driver: bridge
    internal: true
  external:
    driver: bridge
```

### Archivo `.env` on-premise

```bash
POSTGRES_USER=n8n
POSTGRES_PASSWORD=<password-seguro>
POSTGRES_DB=n8n
REDIS_PASSWORD=<password-redis>
N8N_ENCRYPTION_KEY=<clave-aleatoria-32-chars>
N8N_HOSTNAME=n8n.tu-institucion.ejemplo.cl
```

### Configuración nginx

```nginx
# /opt/soc-pipeline/nginx/conf.d/n8n.conf
server {
    listen 443 ssl http2;
    server_name n8n.tu-institucion.ejemplo.cl;

    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    location / {
        proxy_pass         http://n8n:5678;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        chunked_transfer_encoding on;
    }
}

server {
    listen 80;
    server_name n8n.tu-institucion.ejemplo.cl;
    return 301 https://$host$request_uri;
}
```

### Levantar el stack

```bash
cd /opt/soc-pipeline
docker compose up -d
# Verificar
docker compose ps
docker compose logs n8n --tail=50
```

### Ingesta de correo on-premise

El pipeline recibe el boletín como petición HTTP al webhook de n8n.
Para enrutar correo entrante al webhook sin SES:

1. Instalar la dependencia Python necesaria para el script de envío:
   ```bash
   pip install requests   # requerido por el script de envío al webhook
   ```
2. Configurar un alias en Postfix/Exim que ejecute un script:
   ```bash
   # /etc/aliases
   boletines-soc: "| /opt/soc-pipeline/scripts/email_to_webhook.sh"
   ```
3. El script convierte el correo RFC 5322 (stdin) en JSON y hace POST al webhook:
   ```bash
   #!/bin/bash
   WEBHOOK_URL="https://n8n.tu-institucion.ejemplo.cl/webhook/soc/boletines-ingest"
   BASIC_AUTH="usuario:password"
   python3 -c "
   import sys, json, email, base64, requests
   raw = sys.stdin.read()
   msg = email.message_from_string(raw)
   payload = {'raw_email': base64.b64encode(raw.encode()).decode(),
              'subject': msg.get('Subject',''), 'from': msg.get('From','')}
   r = requests.post('$WEBHOOK_URL', json=payload,
                     auth=tuple('$BASIC_AUTH'.split(':')), timeout=30)
   sys.exit(0 if r.ok else 1)
   "
   ```

### Blocklist on-premise

En lugar de S3 + CloudFront, servir el archivo directamente desde nginx:

```nginx
location /blocklist/ {
    alias /opt/soc-pipeline/blocklists/;
    autoindex off;
    add_header Content-Type text/plain;
}
```

La blocklist se actualiza escribiendo directamente el archivo en el directorio configurado.

#### Cómo el workflow n8n actualiza el archivo de blocklist local

En modo on-premise, el workflow n8n usa un nodo `Write Binary File` apuntando a un volumen montado del contenedor n8n que también está expuesto a nginx. Pasos para habilitarlo:

1. En el `docker-compose.yml`, agregar el bind mount de `blocklists/` al servicio n8n:
   ```yaml
   n8n:
     # ... resto de la config existente ...
     volumes:
       - ./n8n_data:/home/node/.n8n
       - ./blocklists:/data/blocklist     # volumen compartido con nginx
   ```
2. En el servicio nginx del mismo `docker-compose.yml`, montar el mismo volumen en read-only:
   ```yaml
   nginx:
     # ... resto de la config existente ...
     volumes:
       - ./nginx/conf.d:/etc/nginx/conf.d:ro
       - ./nginx/certs:/etc/nginx/certs:ro
       - ./blocklists:/usr/share/nginx/html/blocklist:ro
   ```
3. Ajustar el `location /blocklist/` para servir desde la nueva ruta:
   ```nginx
   location /blocklist/ {
       alias /usr/share/nginx/html/blocklist/;
       autoindex off;
       add_header Content-Type text/plain;
   }
   ```
4. Adaptar el workflow `Boletines-CSOC.template.json` para que el nodo de escritura de la blocklist use la ruta `/data/blocklist/hash_sha256.txt` en lugar de un PutObject de S3. Reemplazar el nodo S3 final por un nodo `Write Binary File` con `fileName=/data/blocklist/hash_sha256.txt`.
5. nginx no requiere reload: el `alias` lee el archivo en cada request. Si se cambia la ruta del archivo, ejecutar `docker compose exec nginx nginx -s reload`.

> **Advertencia:** este modo elimina la propiedad de inmutabilidad y versionado que ofrece S3. Cualquier reset de contenedor n8n sin volumen persistente vacía la blocklist. Mantener `./blocklists/` fuera del directorio de datos efímeros y respaldarlo periódicamente.

---

## Adaptación de workflows a una nueva institución

```bash
# Copiar y completar la configuración
cp config.example.env config.env
# Editar config.env

# Generar el workflow adaptado
python scripts/parametrize_workflow.py \
  --input  workflows/Boletines-CSOC.template.json \
  --output workflows/Boletines-CSOC.json \
  --config config.env

# Previsualizar sin escribir
python scripts/parametrize_workflow.py \
  --input  workflows/Boletines-CSOC.template.json \
  --output /tmp/out.json \
  --config config.env \
  --dry-run
```

Tras importar en n8n, asignar manualmente las credenciales marcadas como placeholder:

| Placeholder | Tipo de credencial en n8n |
|-------------|--------------------------|
| `CONFIGURE_AFTER_IMPORT_WEBHOOK_CRED` | Basic Auth (webhook entrante) |
| `CONFIGURE_AFTER_IMPORT_AWS_CRED` | AWS IAM (solo despliegue AWS) |

---

## Integraciones opcionales

Las integraciones siguientes están deshabilitadas por defecto. Activarlas solo cuando se cuenta con la cuenta del proveedor y se han creado los secretos correspondientes.

### Bitdefender GravityZone

El repositorio incluye dos workflows n8n listos para importar:

| Archivo | Función | Trigger |
|---------|---------|---------|
| `workflows/bitdefender_reconcile_daily.template.json` | Reconciliación completa: lee `hash_sha256.txt` desde S3, lista todos los hashes en GravityZone via `getBlocklistItems` (paginado), agrega los faltantes en lotes y deja un `audit` JSON en `s3://<bucket>/gravityzone/audit/`. | Schedule diario (cron `0 0 3 * * *`) o ejecución manual. |
| `workflows/bitdefender_sync_incremental.template.json` | Sync incremental: compara la blocklist deseada en S3 contra el archivo de estado `gravityzone/state/bitdefender_known_present_sha256.txt` y agrega solo el delta en bloques. | Schedule por minutos o ejecución manual. |

Ambos workflows leen su configuración desde variables de entorno n8n que el stack inyecta automáticamente cuando `BitdefenderSecretName` está definido (`SOC_BITDEFENDER_API_KEY`, `SOC_BITDEFENDER_COMPANY_ID`, `SOC_BITDEFENDER_INCIDENTS_V12_URL`, `SOC_BITDEFENDER_LICENSING_V10_URL`, `SOC_BITDEFENDER_ADD_CHUNK_SIZE`, `SOC_BITDEFENDER_ADD_INTERVAL_SECONDS`).

Para habilitarlos:

1. Crear el secreto en Secrets Manager con el formato esperado por los workflows:
   ```bash
   aws secretsmanager create-secret \
     --name "${STACK_NAME}/bitdefendergravityzone" \
     --secret-string '{"apiKey":"CLAVE_API","companyId":"COMPANY_ID"}' \
     --profile "${AWS_PROFILE}"
   ```
2. Pasar el nombre del secreto al stack en el parámetro `BitdefenderSecretName` (ej: `soc-n8n/bitdefendergravityzone`) y desplegar/actualizar el stack para que las task definitions incluyan las variables `SOC_BITDEFENDER_*`.
3. En n8n, importar ambos archivos `.template.json` (Settings → Import from file). En cada nodo S3 marcado con `CONFIGURE_AFTER_IMPORT_AWS_CRED`, asignar la credencial AWS IAM. Reemplazar `PLACEHOLDER_IOC_BUCKET_NAME` con el nombre real del bucket IoC (puede hacerse manualmente en cada nodo S3 o pasando los archivos por `scripts/parametrize_workflow.py`).
4. Recomendación operativa: ejecutar primero `bitdefender_reconcile_daily` en modo manual para sembrar el estado conocido, y luego activar `bitdefender_sync_incremental` para operación recurrente. No activar ambos en automático simultáneamente.

Si la institución usa otro EDR (CrowdStrike, SentinelOne, Defender, etc.), la estructura de los workflows (lectura S3 → diff → POST API EDR → guardar estado) es portable: hay que reemplazar el bloque `Code` que invoca la API JSON-RPC de GravityZone por uno equivalente para el nuevo EDR.

### MISP

El workflow principal `Boletines-CSOC.template.json` no incluye un nodo MISP preconfigurado. Para integrarlo manualmente:

1. Crear el secreto con la URL y el `auth_key` de MISP:
   ```bash
   aws secretsmanager create-secret \
     --name "${STACK_NAME}/misp" \
     --secret-string '{"url":"https://misp.tu-institucion.cl","auth_key":"TOKEN_API"}' \
     --profile "${AWS_PROFILE}"
   ```
2. En n8n: **Settings → Credentials → Add credential → "Header Auth"**. Configurar el header `Authorization: <auth_key>` y la URL base del servidor MISP.
3. Agregar un nodo `HTTP Request` al final del workflow `Boletines-CSOC` que invoque el endpoint MISP `/events/add` con la credencial creada en el paso 2 y el cuerpo JSON con los IoC extraídos del boletín.

---

## Licencia

MIT
