#!/usr/bin/env python3
"""
parametrize_workflow.py - Adapta un workflow n8n exportado a una nueva institucion.

Uso:
  python parametrize_workflow.py --input Workflows/Boletines-CSOC.json \
                                 --output Workflows/Boletines-CSOC.adapted.json \
                                 --config config.env

El script reemplaza en el JSON del workflow:
  - URLs SQS con cuenta AWS especifica
  - Nombres de buckets S3 con cuenta/region especifica
  - URLs de blocklist (hashfw.*)
  - Direcciones de correo (from, to, cc)
  - Nombre de la institucion en texto plano
  - Credential IDs n8n (reemplazados por PLACEHOLDER para configuracion manual post-import)

Variables requeridas en config.env:
  AWS_ACCOUNT_ID          - ID de cuenta AWS de destino (12 digitos)
  AWS_REGION              - Region AWS (ej: us-east-1)
  S3_BUCKET_IOC           - Nombre del bucket S3 para IoC
  S3_BUCKET_NORMALIZED    - Nombre del bucket S3 para boletines normalizados
  S3_BUCKET_RAW           - Nombre del bucket S3 para boletines raw
  SQS_SEND_QUEUE_URL      - URL completa de la cola SQS de envio de correos
  SOC_EMAIL_FROM          - Direccion de correo remitente del SOC
  SOC_EMAIL_TO            - Direccion de correo destinatario principal
  SOC_EMAIL_CC            - Direccion de correo CC (una sola, puede contener varias separadas por coma)
  INSTITUTION_NAME        - Nombre de la institucion (ej: "Municipalidad de Ejemplo")
  INSTITUTION_BASE_DOMAIN - Dominio base (ej: ejemplo.cl)

Variables opcionales:
  BLOCKLIST_HOSTNAME      - Hostname para la URL de blocklist (ej: hashfw.ejemplo.cl)
                            Si no se define, se usa la URL S3 directa.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Strings fuente de la instancia de referencia (valores que se reemplazan
# cuando el input es el JSON original exportado desde esa instancia, no
# un archivo .template.json). Si el input ya es un .template.json, estos
# valores no aparecen: el script usa los strings PLACEHOLDER_* definidos
# mas abajo.
# Completar con los valores reales de la instancia fuente antes de usar.
# ---------------------------------------------------------------------------
_SOURCE_ACCOUNT_ID     = "111122223333"
_SOURCE_IOC_BUCKET     = "boletines-csoc-ioc-111122223333-us-east-1"
_SOURCE_NORM_BUCKET    = "boletines-csoc-normalized-111122223333-us-east-1"
_SOURCE_RAW_BUCKET     = "boletines-csoc-raw-111122223333-us-east-1"
_SOURCE_SQS_URL        = "https://sqs.us-east-1.amazonaws.com/111122223333/stack-mailing-send-queue"
_SOURCE_SQS_QUEUE_NAME = "stack-mailing-send-queue"
_SOURCE_BLOCKLIST_URL  = "https://blocklist.example.cl/hash_sha256.txt"
_SOURCE_EMAIL_FROM     = "soc-bot@mailing.example.cl"
_SOURCE_EMAIL_TO       = "infraestructura@example.cl"
_SOURCE_EMAIL_CC       = "ciberseguridad@example.cl"
_SOURCE_INSTITUTION    = "Institucion de Ejemplo"
_SOURCE_DOMAIN         = "example.cl"

# Credential IDs n8n de la instancia fuente (visibles en la URL al editar la
# credencial en n8n). Completar con los IDs reales antes de usar con el JSON
# original exportado.
_SOURCE_WEBHOOK_CRED   = "AAAAAAAAAAAAAAAA"
_SOURCE_AWS_CRED       = "BBBBBBBBBBBBBBBB"

# Strings PLACEHOLDER usados en los archivos .template.json.
# El script tambien reemplaza estos, de modo que funciona tanto con el JSON
# original de la instancia fuente como con el template ya neutralizado.
PLACEHOLDER_WEBHOOK_CRED = "CONFIGURE_AFTER_IMPORT_WEBHOOK_CRED"
PLACEHOLDER_AWS_CRED     = "CONFIGURE_AFTER_IMPORT_AWS_CRED"
PLACEHOLDER_SQS_URL      = "PLACEHOLDER_SQS_SEND_QUEUE_URL"
PLACEHOLDER_IOC_BUCKET   = "PLACEHOLDER_IOC_BUCKET_NAME"
PLACEHOLDER_NORM_BUCKET  = "PLACEHOLDER_NORMALIZED_BUCKET_NAME"
PLACEHOLDER_RAW_BUCKET   = "PLACEHOLDER_RAW_BUCKET_NAME"
PLACEHOLDER_BLOCKLIST    = "PLACEHOLDER_BLOCKLIST_URL"
PLACEHOLDER_EMAIL_FROM   = "PLACEHOLDER_SOC_EMAIL_FROM"
PLACEHOLDER_EMAIL_TO     = "PLACEHOLDER_SOC_EMAIL_TO"
PLACEHOLDER_EMAIL_CC     = "PLACEHOLDER_SOC_EMAIL_CC"
PLACEHOLDER_INSTITUTION  = "PLACEHOLDER_INSTITUTION_NAME"

# Variables requeridas en el archivo de configuracion
REQUIRED_VARS = [
    "AWS_ACCOUNT_ID",
    "AWS_REGION",
    "S3_BUCKET_IOC",
    "S3_BUCKET_NORMALIZED",
    "S3_BUCKET_RAW",
    "SQS_SEND_QUEUE_URL",
    "SOC_EMAIL_FROM",
    "SOC_EMAIL_TO",
    "SOC_EMAIL_CC",
    "INSTITUTION_NAME",
    "INSTITUTION_BASE_DOMAIN",
]


# ---------------------------------------------------------------------------
# Lectura del archivo .env
# ---------------------------------------------------------------------------

def parse_env_file(path: str) -> dict[str, str]:
    """
    Parsea un archivo .env linea a linea.
    Ignora comentarios (#) y lineas vacias.
    Respeta comillas simples y dobles en los valores.
    """
    config: dict[str, str] = {}
    env_path = Path(path)

    if not env_path.exists():
        print(f"[ERROR] Archivo de configuracion no encontrado: {path}")
        sys.exit(1)

    with open(env_path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.rstrip("\n").rstrip("\r")
            stripped = line.strip()

            # Ignorar comentarios y lineas vacias
            if not stripped or stripped.startswith("#"):
                continue

            # Parsear KEY=VALUE
            if "=" not in stripped:
                print(f"[AVISO] Linea {lineno} sin '=', ignorada: {stripped[:50]}")
                continue

            key, _, raw_value = stripped.partition("=")
            key = key.strip()
            raw_value = raw_value.strip()

            # Remover comillas envolventes (simples o dobles)
            if len(raw_value) >= 2:
                if (raw_value.startswith('"') and raw_value.endswith('"')) or \
                   (raw_value.startswith("'") and raw_value.endswith("'")):
                    raw_value = raw_value[1:-1]

            if key:
                config[key] = raw_value

    return config


# ---------------------------------------------------------------------------
# Validacion del config
# ---------------------------------------------------------------------------

def validate_config(config: dict[str, str]) -> list[str]:
    """Retorna lista de variables requeridas que faltan."""
    missing = []
    for var in REQUIRED_VARS:
        if not config.get(var, "").strip():
            missing.append(var)
    return missing


# ---------------------------------------------------------------------------
# Logica de reemplazo
# ---------------------------------------------------------------------------

def extract_queue_name_from_url(sqs_url: str) -> str:
    """Extrae el nombre de la cola desde una URL SQS completa."""
    parsed = urlparse(sqs_url)
    parts = parsed.path.strip("/").split("/")
    return parts[-1] if parts else ""


def build_blocklist_url(config: dict[str, str]) -> str:
    """
    Construye la URL de blocklist para la nueva institucion.
    Si BLOCKLIST_HOSTNAME esta definido, usa https://{hostname}/hash_sha256.txt
    Si no, usa la URL S3 directa.
    """
    hostname = config.get("BLOCKLIST_HOSTNAME", "").strip()
    if hostname:
        return f"https://{hostname}/hash_sha256.txt"

    bucket = config["S3_BUCKET_IOC"]
    region = config["AWS_REGION"]
    return f"https://{bucket}.s3.{region}.amazonaws.com/hash_sha256.txt"


def apply_replacements(content: str, config: dict[str, str]) -> tuple[str, dict[str, int]]:
    """
    Aplica todos los reemplazos en el string JSON del workflow.
    Retorna (contenido_modificado, contadores_por_tipo).
    """
    counts: dict[str, int] = {}

    def replace_and_count(text: str, old: str, new: str, label: str) -> str:
        if old not in text:
            return text
        count = text.count(old)
        counts[label] = counts.get(label, 0) + count
        return text.replace(old, new)

    # ------------------------------------------------------------------
    # 1. Credential IDs n8n
    #    Aplica tanto al JSON fuente como al .template.json:
    #    en el fuente estan como IDs reales; en el template ya son
    #    CONFIGURE_AFTER_IMPORT_* (no se reemplazan dos veces).
    # ------------------------------------------------------------------
    content = replace_and_count(content, _SOURCE_WEBHOOK_CRED,
                                 PLACEHOLDER_WEBHOOK_CRED, "credential_id_webhook")
    content = replace_and_count(content, _SOURCE_AWS_CRED,
                                 PLACEHOLDER_AWS_CRED, "credential_id_aws")

    new_sqs_url       = config["SQS_SEND_QUEUE_URL"]
    new_ioc_bucket    = config["S3_BUCKET_IOC"]
    new_norm_bucket   = config["S3_BUCKET_NORMALIZED"]
    new_raw_bucket    = config["S3_BUCKET_RAW"]
    new_blocklist_url = build_blocklist_url(config)
    new_email_from    = config["SOC_EMAIL_FROM"]
    new_email_to      = config["SOC_EMAIL_TO"]
    new_email_cc      = config["SOC_EMAIL_CC"].split(",")[0].strip()
    new_institution   = config["INSTITUTION_NAME"]
    new_domain        = config["INSTITUTION_BASE_DOMAIN"]
    new_queue_name    = extract_queue_name_from_url(new_sqs_url)

    # ------------------------------------------------------------------
    # 2. Reemplazos desde JSON fuente (instancia de referencia)
    # ------------------------------------------------------------------
    content = replace_and_count(content, _SOURCE_SQS_URL,   new_sqs_url,    "sqs_url")
    if new_queue_name and new_queue_name != _SOURCE_SQS_QUEUE_NAME:
        content = replace_and_count(content, _SOURCE_SQS_QUEUE_NAME,
                                     new_queue_name, "sqs_queue_name")
    content = replace_and_count(content, _SOURCE_IOC_BUCKET,  new_ioc_bucket,    "s3_bucket_ioc")
    content = replace_and_count(content, _SOURCE_NORM_BUCKET, new_norm_bucket,   "s3_bucket_normalized")
    content = replace_and_count(content, _SOURCE_RAW_BUCKET,  new_raw_bucket,    "s3_bucket_raw")
    content = replace_and_count(content, _SOURCE_BLOCKLIST_URL, new_blocklist_url, "blocklist_url")
    content = replace_and_count(content, _SOURCE_EMAIL_FROM,  new_email_from,    "email_from")
    content = replace_and_count(content, _SOURCE_EMAIL_TO,    new_email_to,      "email_to")
    content = replace_and_count(content, _SOURCE_EMAIL_CC,    new_email_cc,      "email_cc")
    content = replace_and_count(content, _SOURCE_INSTITUTION, new_institution,   "institution_name")
    if new_domain != _SOURCE_DOMAIN:
        content = replace_and_count(content, f".{_SOURCE_DOMAIN}",
                                     f".{new_domain}", "institution_domain")

    # ------------------------------------------------------------------
    # 3. Reemplazos desde .template.json (PLACEHOLDERs neutralizados)
    # ------------------------------------------------------------------
    content = replace_and_count(content, PLACEHOLDER_SQS_URL,     new_sqs_url,      "sqs_url_ph")
    content = replace_and_count(content, PLACEHOLDER_IOC_BUCKET,  new_ioc_bucket,   "s3_ioc_ph")
    content = replace_and_count(content, PLACEHOLDER_NORM_BUCKET, new_norm_bucket,  "s3_norm_ph")
    content = replace_and_count(content, PLACEHOLDER_RAW_BUCKET,  new_raw_bucket,   "s3_raw_ph")
    content = replace_and_count(content, PLACEHOLDER_BLOCKLIST,   new_blocklist_url,"blocklist_ph")
    content = replace_and_count(content, PLACEHOLDER_EMAIL_FROM,  new_email_from,   "email_from_ph")
    content = replace_and_count(content, PLACEHOLDER_EMAIL_TO,    new_email_to,     "email_to_ph")
    content = replace_and_count(content, PLACEHOLDER_EMAIL_CC,    new_email_cc,     "email_cc_ph")
    content = replace_and_count(content, PLACEHOLDER_INSTITUTION, new_institution,  "institution_ph")

    return content, counts


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Adapta un workflow n8n exportado a una nueva institucion"
    )
    parser.add_argument("--input",  required=True,
                        help="Ruta al workflow JSON de entrada")
    parser.add_argument("--output", required=True,
                        help="Ruta al workflow JSON de salida")
    parser.add_argument("--config", default="config.env",
                        help="Ruta al archivo de configuracion .env (default: config.env)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Mostrar que se reemplazaria sin escribir el archivo de salida")
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # 1. Leer y validar config
    # ------------------------------------------------------------------
    print(f"Leyendo configuracion desde: {args.config}")
    config = parse_env_file(args.config)

    missing = validate_config(config)
    if missing:
        print("\n[ERROR] Faltan las siguientes variables requeridas en el config:")
        for var in missing:
            print(f"  - {var}")
        print(f"\nAgrega estas variables a {args.config} y vuelve a ejecutar.")
        sys.exit(1)

    print(f"  Institucion:    {config['INSTITUTION_NAME']}")
    print(f"  Cuenta AWS:     {config['AWS_ACCOUNT_ID']}")
    print(f"  Region:         {config['AWS_REGION']}")
    print(f"  S3 IOC bucket:  {config['S3_BUCKET_IOC']}")
    print(f"  SQS URL:        {config['SQS_SEND_QUEUE_URL']}")
    blocklist_url = build_blocklist_url(config)
    print(f"  Blocklist URL:  {blocklist_url}")

    # ------------------------------------------------------------------
    # 2. Leer workflow de entrada
    # ------------------------------------------------------------------
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"\n[ERROR] No se encuentra el archivo de entrada: {args.input}")
        sys.exit(1)

    print(f"\nLeyendo workflow: {args.input}")
    content = input_path.read_text(encoding="utf-8")

    # Validar que es JSON valido
    try:
        original_obj = json.loads(content)
    except json.JSONDecodeError as exc:
        print(f"[ERROR] El archivo de entrada no es JSON valido: {exc}")
        sys.exit(1)

    print(f"  Workflow: {original_obj.get('name', 'sin nombre')}")
    print(f"  Nodos:    {len(original_obj.get('nodes', []))}")

    # ------------------------------------------------------------------
    # 3. Aplicar reemplazos en el string JSON
    # ------------------------------------------------------------------
    print("\nAplicando reemplazos...")
    modified_content, counts = apply_replacements(content, config)

    # ------------------------------------------------------------------
    # 4. Validar que el resultado es JSON valido
    # ------------------------------------------------------------------
    try:
        modified_obj = json.loads(modified_content)
    except json.JSONDecodeError as exc:
        print(f"\n[ERROR] El resultado no es JSON valido tras los reemplazos: {exc}")
        print("Esto puede indicar un reemplazo parcial en una cadena JSON. Revisar config.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # 5. Imprimir resumen de reemplazos
    # ------------------------------------------------------------------
    print("\nResumen de reemplazos:")
    if counts:
        for label, count in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"  {label:35s}: {count} ocurrencia(s)")
    else:
        print("  Sin reemplazos realizados.")
        print("\n  [AVISO] El workflow puede no contener los valores fuente esperados.")

    # ------------------------------------------------------------------
    # 6. Escribir resultado (o solo mostrar si dry-run)
    # ------------------------------------------------------------------
    if args.dry_run:
        print(f"\n[DRY-RUN] No se escribe el archivo de salida: {args.output}")
        print("  Ejecutar sin --dry-run para aplicar los cambios.")
        return

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(modified_obj, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nWorkflow adaptado escrito en: {args.output}")
    print("IMPORTANTE: Tras importar en n8n, configurar manualmente las credenciales:")
    print(f"  - CONFIGURE_AFTER_IMPORT_WEBHOOK_CRED: credencial basicAuth del webhook")
    print(f"  - CONFIGURE_AFTER_IMPORT_AWS_CRED:     credencial AWS IAM")


if __name__ == "__main__":
    main()
