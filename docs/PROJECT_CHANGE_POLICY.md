# PROJECT_CHANGE_POLICY.md
# Política de cambios - SOC Threat Intel Pipeline

**Última actualización:** 2026-04-09

---

## PRINCIPIOS

1. **IaC primero:** Todo cambio de infraestructura que persista en producción debe reflejarse en el template CloudFormation.
2. **Trazabilidad:** Todo cambio material debe quedar documentado con: qué se cambió, por qué, cuándo, por quién.
3. **Reversibilidad:** Todo cambio debe tener un plan de rollback antes de ejecutarse.
4. **Mínimo blast radius:** Cambiar solo lo necesario. No aprovechar un despliegue para hacer cambios no relacionados.
5. **Evidencia antes de cambio:** Verificar el estado real antes de modificar, no asumir.

---

## TIPOS DE CAMBIOS Y PROCESO REQUERIDO

### Tipo A - Cambio de infraestructura (CloudFormation)

**Qué incluye:** Modificación de recursos ECS, RDS, Redis, ALB, WAF, SG, IAM, S3, Route53 definidos en el stack.

**Proceso obligatorio:**
1. Editar `infra/cloudformation/n8n_stack_template.json`
2. Validar template
3. Crear change set y revisar impacto
4. Documentar en `CHANGELOG.md`
5. Ejecutar despliegue con postchecks
6. Si implica decisión de arquitectura: registrar en `docs/PROJECT_ADR.md`

**Prohibido:** Modificar recursos del stack directamente en consola AWS sin reflejar en template.

---

### Tipo B - Cambio de código Lambda (soc-mail-guard / soc-mail-normalizer)

**Qué incluye:** Modificación de `src/lambdas/*/lambda_function.py`.

**Proceso obligatorio:**
1. Modificar código en `src/lambdas/*/`
2. Actualizar `CHANGELOG.md`
3. Empaquetar y desplegar via AWS CLI
4. Verificar logs Lambda post-despliegue
5. Si cambia el contrato de entrada/salida: actualizar `docs/DOCUMENTACION_PROYECTO.txt`

---

### Tipo C - Cambio de workflows n8n

**Qué incluye:** Modificación de `Workflows/*.json`.

**Proceso obligatorio:**
1. Exportar workflow actual desde n8n antes de modificar (backup)
2. Modificar y probar en n8n
3. Exportar workflow actualizado y sobrescribir en `Workflows/`
4. Actualizar `CHANGELOG.md`
5. Ejecutar `scripts/validate_boletines_post_import.ps1`
6. Si cambia un nodo de integración (SQS, Bedrock, webhook): verificar contrato de datos

---

### Tipo D - Cambio de configuración de flujo de correo

**Qué incluye:** Modificación de SES receipt rules, variables de entorno Lambda, SG, IAM policies del flujo correo.

**Proceso obligatorio:**
1. Actualizar artefacto correspondiente en `infra/mail_ingest/` o `infra/policies/`
2. Aplicar cambio via AWS CLI
3. Documentar en `CHANGELOG.md`
4. Verificar funcionamiento del flujo con un correo de prueba
5. Registrar en `docs/DOCUMENTACION_PROYECTO.txt`

---

### Tipo E - Cambio de template HTML de correo

**Qué incluye:** Modificación de `templates/mailing/soc_ioc_notification_v1.html`.

**Proceso obligatorio:**
1. Modificar template
2. Si cambian variables template (`template_data`): actualizar contrato en `docs/DISENO_MAILING_HTML_DESACOPLADO.md`
3. Verificar que send-worker reconoce el template_id correcto
4. Actualizar `CHANGELOG.md`

---

## CUÁNDO USAR CLOUDFORMATION

**Siempre** para modificar recursos del stack `stack-ecs-n8n`.

**No aplica** para:
- Lambdas soc-mail-guard y soc-mail-normalizer (gestionadas manualmente)
- Stack stack-mailing-plataformas (stack separado, gestión propia)
- Recursos de la VPC compartida (gestión separada)

---

## CUÁNDO NO TOCAR LA CONSOLA

**No hacer cambios persistentes via consola en:**
- Task definitions de ECS (usar CloudFormation)
- Security Groups del stack (usar CloudFormation)
- Target groups del ALB (usar CloudFormation)
- Parámetros de RDS que estén en el template (usar CloudFormation)
- Reglas WAF definidas en el template (usar CloudFormation)
- IAM Roles definidos en el template (usar CloudFormation)

**Sí se puede usar la consola para:**
- Leer logs, describir recursos, ejecutar queries de estado
- Operaciones de emergencia documentadas
- Visualización de métricas y dashboards

---

## CÓMO DOCUMENTAR UN CAMBIO

Para todo cambio material, registrar en `CHANGELOG.md`:

```markdown
## [Fecha] - Descripción breve del cambio

### Tipo de cambio
- Infraestructura / Código / Workflow / Configuración

### Qué se cambió
- Descripción técnica exacta

### Por qué se cambió
- Motivación o problema que resuelve

### Archivos modificados
- Lista de archivos

### Estado post-cambio
- Resultado de postchecks
```

---

## MANEJO DE DRIFT

El drift es la diferencia entre el template CloudFormation y el estado real de los recursos en AWS.

### Detección de drift
```bash
aws cloudformation detect-stack-drift \
  --stack-name stack-ecs-n8n \
  --profile mfa-session

aws cloudformation describe-stack-drift-detection-status \
  --stack-drift-detection-id <ID> \
  --profile mfa-session

aws cloudformation describe-stack-resource-drifts \
  --stack-name stack-ecs-n8n \
  --profile mfa-session
```

### Política ante drift detectado

1. **Identificar qué recurso drifteó y qué cambió**
2. **Evaluar si el cambio manual fue intencional o accidental:**
   - Si fue accidental: revertir via CloudFormation update
   - Si fue intencional y debe mantenerse: incorporar el cambio al template y redeplegar
3. **No dejar drift sin resolución documentada**
4. **Registrar decisión en `docs/PROJECT_ADR.md`**

---

## CORRECCIÓN DE ERRORES DE STACK

Si el stack queda en estado `UPDATE_ROLLBACK_FAILED`:

```bash
# Opción 1: Continuar rollback
aws cloudformation continue-update-rollback \
  --stack-name stack-ecs-n8n \
  --profile mfa-session

# Opción 2: Continuar rollback ignorando recursos problemáticos
aws cloudformation continue-update-rollback \
  --stack-name stack-ecs-n8n \
  --resources-to-skip <RECURSO_LÓGICO> \
  --profile mfa-session
```

Diagnóstico previo:
```bash
aws cloudformation describe-stack-events \
  --stack-name stack-ecs-n8n \
  --profile mfa-session \
  --query "StackEvents[?ResourceStatus=='UPDATE_FAILED']"
```

---

## CONTROL DE VERSIONES

- El template CloudFormation se versiona en el repositorio (no crear versiones numeradas)
- Git es la fuente de historial de cambios del template
- Los workflows n8n exportados en `Workflows/` representan la versión más reciente importada
- No crear archivos duplicados del template (`n8n_stack_template_v2.json`, etc.)

---

## CONSISTENCIA DE TAGGING EN CAMBIOS

Todo recurso creado o modificado por un cambio debe llevar:

```json
{ "Key": "Proyecto", "Value": "n8n" }
```

En CloudFormation, los tags se declaran en la sección `Tags` de cada recurso y/o en los tags del stack:

```json
"Tags": [
  { "Key": "Proyecto", "Value": "n8n" }
]
```

Después de un despliegue, verificar cobertura de tags:
```bash
aws resourcegroupstaggingapi get-resources \
  --tag-filters Key=Proyecto,Values=n8n \
  --profile mfa-session
```
