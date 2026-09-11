# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.0.0/).

## [1.1] - 2026-09-10

### Corregido

- **Ejecuciones duplicadas en `main`:** se agregó la variable de entorno `N8N_MULTI_MAIN_SETUP=true`
  al contenedor `main`. El template ya desplegaba `main` con `DesiredCount: 2`, pero sin esta
  variable cada trigger programado (cron/interval) se ejecutaba dos veces en paralelo. Corregido
  en `infra/cloudformation/n8n_stack_template.json` y propagado a `all_in_one_stack.json`.
- **Almacenamiento efímero insuficiente:** las task definitions de `main` y `worker` no fijaban
  `EphemeralStorage`, quedando en el default de Fargate (20 GiB). Con volúmenes de datos binarios
  significativos (`N8N_DEFAULT_BINARY_DATA_MODE=filesystem`) esto puede agotar el disco de forma
  intermitente. Se agregó el parámetro `N8nEphemeralStorageGiB` (default 40 GiB).

### Agregado

- `SslPolicy: ELBSecurityPolicy-TLS13-1-2-2021-06` explícito en el listener HTTPS del ALB
  (antes usaba el default `ELBSecurityPolicy-2016-08`, que permite TLS 1.0/1.1).
- Parámetros `N8nExecutionsDataPrune` y `N8nExecutionsDataMaxAgeHours` para controlar la poda
  automática del historial de ejecuciones de n8n, evitando crecimiento sin límite en RDS.
- Archivo `LICENSE` (MIT).
- Nueva sección "Lecciones operativas y advertencias conocidas" en `DEPLOY.md`: alcance real
  (global) de `N8N_BLOCK_ENV_ACCESS_IN_NODE`, gotcha de tipo de credencial al reescribir
  integraciones a `HTTP Request` nativo, recomendación de fijar versión de imagen de n8n y
  validarla en un entorno aislado antes de producción, y riesgo de VPC Interface Endpoints con
  DNS privado en VPCs compartidas.
- Fila nueva en la tabla de Troubleshooting de `DEPLOY.md` sobre credenciales AWS editadas en la
  UI de n8n que no se recargan solas en modo queue (requieren `--force-new-deployment`).

## [1.0] - Lanzamiento inicial

Primera versión pública del pipeline: ingesta de boletines de amenazas por correo, extracción de
IoC, blocklist en S3/CloudFront, integraciones opcionales con Bitdefender GravityZone y MISP.
