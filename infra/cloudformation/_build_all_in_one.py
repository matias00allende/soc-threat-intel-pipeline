"""
Build all_in_one_stack.json by merging:
  - n8n_stack_template.json (n8n core stack)
  - mail_ingest_stack.json  (SES inbound + S3 raw/normalized + Lambdas)
  - new resources for the simplified SOC mail sender (SQS + Lambda + EventSource)

Run from any cwd. Writes:
  - all_in_one_stack.json

This script is the source of truth for assembling the all-in-one template.
Re-run after editing any of the source templates.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
N8N_TPL = os.path.join(HERE, "n8n_stack_template.json")
ING_TPL = os.path.join(HERE, "mail_ingest_stack.json")
OUT_TPL = os.path.join(HERE, "all_in_one_stack.json")


def load(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    n8n = load(N8N_TPL)
    ing = load(ING_TPL)

    out = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": (
            "All-in-one SOC boletines stack. Combina n8n (ECS Fargate + ALB + RDS + Redis + WAF), "
            "ingesta de correo (SES inbound + S3 raw/normalized + Lambdas guard/normalizer) y "
            "sender simplificado (SQS + Lambda SES) en un unico stack independiente."
        ),
    }

    # --------------------------------------------------------------------- #
    # Parameters: union of n8n + ingest + new sender params                 #
    # --------------------------------------------------------------------- #
    params = {}
    params.update(n8n.get("Parameters", {}))
    params.update(ing.get("Parameters", {}))

    # New parameters for the simplified SOC mail sender (monolith mode).
    sender_params = {
        "SesFromAddress": {
            "Type": "String",
            "Description": "Direccion de correo del remitente SOC (ej: soc-bot@mailing.ejemplo.cl)."
        },
        "SesIdentityArn": {
            "Type": "String",
            "Description": "ARN de la identidad SES verificada para el remitente. Ej: arn:aws:ses:REGION:ACCOUNT:identity/dominio.cl"
        },
        "NotificationToAddresses": {
            "Type": "String",
            "Description": "Destinatarios principales del correo SOC, separados por coma."
        },
        "NotificationCcAddresses": {
            "Type": "String",
            "Default": "",
            "Description": "Destinatarios en copia (CC), separados por coma. Puede dejarse vacio."
        },
        "MunicipalityName": {
            "Type": "String",
            "Description": "Nombre de la institucion (usado en el cuerpo del correo)."
        },
        "MunicipalityLogoUrl": {
            "Type": "String",
            "Default": "",
            "Description": "URL del logo para el template HTML."
        },
        "MunicipalityUrl": {
            "Type": "String",
            "Default": "",
            "Description": "URL del sitio web."
        },
        "MailTemplateBucketName": {
            "Type": "String",
            "Description": "Nombre del bucket S3 donde se almacena el template HTML del correo SOC. Puede ser igual a un bucket ya existente del stack o uno separado."
        },
        "MailTemplateKey": {
            "Type": "String",
            "Default": "templates/soc_ioc_notification_v1.html",
            "Description": "Ruta (key) del template HTML dentro del bucket MailTemplateBucketName."
        }
    }
    params.update(sender_params)

    out["Parameters"] = params

    # --------------------------------------------------------------------- #
    # Conditions: union (no name collisions)                                #
    # --------------------------------------------------------------------- #
    conditions = {}
    conditions.update(n8n.get("Conditions", {}))
    conditions.update(ing.get("Conditions", {}))
    out["Conditions"] = conditions

    # --------------------------------------------------------------------- #
    # Metadata: keep n8n's parameter groups and add a new section for       #
    # ingest + sender so the AWS console renders a coherent form.          #
    # --------------------------------------------------------------------- #
    meta = json.loads(json.dumps(n8n.get("Metadata", {})))  # deep copy
    iface = meta.setdefault("AWS::CloudFormation::Interface", {})
    groups = iface.setdefault("ParameterGroups", [])
    groups.append({
        "Label": {"default": "Mail Ingest (SES inbound + S3 + Lambdas)"},
        "Parameters": [
            "IngestEmailAddress",
            "AllowedSenderDomains",
            "RawBucketName",
            "NormalizedBucketName",
            "WebhookUrl",
            "WebhookBasicUser",
            "WebhookBasicPassword",
            "WebhookRequired",
            "DispatchPrefix",
            "WebhookTimeoutSeconds",
            "VpcSubnetIds",
            "LambdaSecurityGroupId",
            "ExistingReceiptRuleSet"
        ]
    })
    groups.append({
        "Label": {"default": "SOC Mail Sender (SQS + Lambda SES)"},
        "Parameters": [
            "SesFromAddress",
            "SesIdentityArn",
            "NotificationToAddresses",
            "NotificationCcAddresses",
            "MunicipalityName",
            "MunicipalityLogoUrl",
            "MunicipalityUrl",
            "MailTemplateBucketName",
            "MailTemplateKey"
        ]
    })
    out["Metadata"] = meta

    # --------------------------------------------------------------------- #
    # Resources: union + new sender resources                               #
    # --------------------------------------------------------------------- #
    resources = {}
    resources.update(n8n.get("Resources", {}))
    resources.update(ing.get("Resources", {}))

    # SQS queue interna para el flujo de notificacion SOC.
    resources["SocMailSendQueue"] = {
        "Type": "AWS::SQS::Queue",
        "Properties": {
            "QueueName": {"Fn::Sub": "${AWS::StackName}-soc-mail-queue"},
            "VisibilityTimeout": 300,
            "MessageRetentionPeriod": 86400,
            "KmsMasterKeyId": "alias/aws/sqs"
        }
    }

    # IAM role para la Lambda sender.
    resources["SocMailSenderLambdaRole"] = {
        "Type": "AWS::IAM::Role",
        "Properties": {
            "RoleName": {"Fn::Sub": "${AWS::StackName}-soc-mail-sender-role"},
            "AssumeRolePolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "lambda.amazonaws.com"},
                        "Action": "sts:AssumeRole"
                    }
                ]
            },
            "ManagedPolicyArns": [
                "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
            ],
            "Policies": [
                {
                    "PolicyName": "SocMailSenderInline",
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "ReadSqs",
                                "Effect": "Allow",
                                "Action": [
                                    "sqs:ReceiveMessage",
                                    "sqs:DeleteMessage",
                                    "sqs:GetQueueAttributes"
                                ],
                                "Resource": {"Fn::GetAtt": ["SocMailSendQueue", "Arn"]}
                            },
                            {
                                "Sid": "SendEmailViaSes",
                                "Effect": "Allow",
                                "Action": [
                                    "ses:SendEmail",
                                    "ses:SendRawEmail"
                                ],
                                "Resource": {"Ref": "SesIdentityArn"}
                            },
                            {
                                "Sid": "ReadMailTemplate",
                                "Effect": "Allow",
                                "Action": [
                                    "s3:GetObject"
                                ],
                                "Resource": {
                                    "Fn::Sub": "arn:aws:s3:::${MailTemplateBucketName}/${MailTemplateKey}"
                                }
                            }
                        ]
                    }
                }
            ]
        }
    }

    # Lambda sender simplificada (codigo inline en ZipFile).
    sender_code = (
        "import json\n"
        "import os\n"
        "import re\n"
        "import boto3\n"
        "\n"
        "ses = boto3.client(\"ses\")\n"
        "s3 = boto3.client(\"s3\")\n"
        "\n"
        "FROM_ADDRESS = os.environ[\"SES_FROM_ADDRESS\"]\n"
        "TO_ADDRESSES = [x.strip() for x in os.environ.get(\"NOTIFICATION_TO\", \"\").split(\",\") if x.strip()]\n"
        "CC_ADDRESSES = [x.strip() for x in os.environ.get(\"NOTIFICATION_CC\", \"\").split(\",\") if x.strip()]\n"
        "MUNICIPALITY_NAME = os.environ.get(\"MUNICIPALITY_NAME\", \"\")\n"
        "MUNICIPALITY_LOGO_URL = os.environ.get(\"MUNICIPALITY_LOGO_URL\", \"\")\n"
        "MUNICIPALITY_URL = os.environ.get(\"MUNICIPALITY_URL\", \"\")\n"
        "TEMPLATE_BUCKET = os.environ.get(\"MAIL_TEMPLATE_BUCKET\", \"\")\n"
        "TEMPLATE_KEY = os.environ.get(\"MAIL_TEMPLATE_KEY\", \"templates/soc_ioc_notification_v1.html\")\n"
        "\n"
        "_template_cache = None\n"
        "\n"
        "def get_template():\n"
        "    global _template_cache\n"
        "    if _template_cache is None and TEMPLATE_BUCKET:\n"
        "        obj = s3.get_object(Bucket=TEMPLATE_BUCKET, Key=TEMPLATE_KEY)\n"
        "        _template_cache = obj[\"Body\"].read().decode(\"utf-8\")\n"
        "    return _template_cache or \"\"\n"
        "\n"
        "def render(template, data):\n"
        "    def replacer(m):\n"
        "        key = m.group(1).strip()\n"
        "        parts = key.split(\"|\")\n"
        "        var = parts[0].strip()\n"
        "        val = data.get(var, \"\")\n"
        "        for filt in parts[1:]:\n"
        "            filt = filt.strip()\n"
        "            if filt.startswith(\"default(\"):\n"
        "                if not val:\n"
        "                    val = filt[8:-1].strip(\"'\\\"\")\n"
        "            elif filt.startswith(\"replace(\"):\n"
        "                args = filt[8:-1].split(\",\")\n"
        "                if len(args) == 2:\n"
        "                    val = val.replace(args[0].strip(\"'\\\" \"), args[1].strip(\"'\\\" \"))\n"
        "        return str(val)\n"
        "    return re.sub(r\"\\{\\{\\s*(.*?)\\s*\\}\\}\", replacer, template)\n"
        "\n"
        "def handler(event, context):\n"
        "    for record in event.get(\"Records\", []):\n"
        "        body = json.loads(record[\"body\"])\n"
        "        subject = body.get(\"subject\", \"Notificacion SOC\")\n"
        "        text = body.get(\"text\", \"\")\n"
        "        to = body.get(\"to\") or TO_ADDRESSES\n"
        "        cc = body.get(\"cc\") or CC_ADDRESSES\n"
        "        td = body.get(\"template_data\", {})\n"
        "        td.setdefault(\"municipality_name\", MUNICIPALITY_NAME)\n"
        "        td.setdefault(\"municipality_logo_url\", MUNICIPALITY_LOGO_URL)\n"
        "        td.setdefault(\"municipality_url\", MUNICIPALITY_URL)\n"
        "        html_template = get_template()\n"
        "        html_body = render(html_template, td) if html_template else \"\"\n"
        "        msg = {\n"
        "            \"Source\": FROM_ADDRESS,\n"
        "            \"Destination\": {\"ToAddresses\": to if isinstance(to, list) else [to]},\n"
        "            \"Message\": {\n"
        "                \"Subject\": {\"Data\": subject, \"Charset\": \"UTF-8\"},\n"
        "                \"Body\": {\"Text\": {\"Data\": text, \"Charset\": \"UTF-8\"}}\n"
        "            }\n"
        "        }\n"
        "        if cc:\n"
        "            msg[\"Destination\"][\"CcAddresses\"] = cc if isinstance(cc, list) else [cc]\n"
        "        if html_body:\n"
        "            msg[\"Message\"][\"Body\"][\"Html\"] = {\"Data\": html_body, \"Charset\": \"UTF-8\"}\n"
        "        ses.send_email(**msg)\n"
        "    return {\"statusCode\": 200}\n"
    )

    resources["SocMailSenderLambda"] = {
        "Type": "AWS::Lambda::Function",
        "Properties": {
            "FunctionName": {"Fn::Sub": "${AWS::StackName}-soc-mail-sender"},
            "Runtime": "python3.12",
            "Handler": "index.handler",
            "Role": {"Fn::GetAtt": ["SocMailSenderLambdaRole", "Arn"]},
            "Timeout": 60,
            "MemorySize": 256,
            "Environment": {
                "Variables": {
                    "SES_FROM_ADDRESS": {"Ref": "SesFromAddress"},
                    "NOTIFICATION_TO": {"Ref": "NotificationToAddresses"},
                    "NOTIFICATION_CC": {"Ref": "NotificationCcAddresses"},
                    "MUNICIPALITY_NAME": {"Ref": "MunicipalityName"},
                    "MUNICIPALITY_LOGO_URL": {"Ref": "MunicipalityLogoUrl"},
                    "MUNICIPALITY_URL": {"Ref": "MunicipalityUrl"},
                    "MAIL_TEMPLATE_BUCKET": {"Ref": "MailTemplateBucketName"},
                    "MAIL_TEMPLATE_KEY": {"Ref": "MailTemplateKey"}
                }
            },
            "Code": {
                "ZipFile": sender_code
            }
        }
    }

    resources["SocMailSenderEventSource"] = {
        "Type": "AWS::Lambda::EventSourceMapping",
        "Properties": {
            "EventSourceArn": {"Fn::GetAtt": ["SocMailSendQueue", "Arn"]},
            "FunctionName": {"Fn::GetAtt": ["SocMailSenderLambda", "Arn"]},
            "BatchSize": 10,
            "Enabled": True
        }
    }

    out["Resources"] = resources

    # --------------------------------------------------------------------- #
    # Outputs: union + nuevos outputs para la cola SQS interna              #
    # --------------------------------------------------------------------- #
    outputs = {}
    outputs.update(n8n.get("Outputs", {}))
    outputs.update(ing.get("Outputs", {}))

    outputs["SocMailSendQueueUrl"] = {
        "Description": "URL de la cola SQS interna usada por n8n para encolar correos SOC.",
        "Value": {"Ref": "SocMailSendQueue"},
        "Export": {"Name": {"Fn::Sub": "${AWS::StackName}-SocMailSendQueueUrl"}}
    }
    outputs["SocMailSendQueueArn"] = {
        "Description": "ARN de la cola SQS interna del sender SOC.",
        "Value": {"Fn::GetAtt": ["SocMailSendQueue", "Arn"]},
        "Export": {"Name": {"Fn::Sub": "${AWS::StackName}-SocMailSendQueueArn"}}
    }
    outputs["SocMailSenderLambdaArn"] = {
        "Description": "ARN de la Lambda sender simplificada que consume desde SQS y envia via SES.",
        "Value": {"Fn::GetAtt": ["SocMailSenderLambda", "Arn"]},
        "Export": {"Name": {"Fn::Sub": "${AWS::StackName}-SocMailSenderLambdaArn"}}
    }

    out["Outputs"] = outputs

    # --------------------------------------------------------------------- #
    # Validacion final + escritura                                          #
    # --------------------------------------------------------------------- #
    text = json.dumps(out, indent=2, ensure_ascii=False)
    json.loads(text)  # parse-back sanity check

    with open(OUT_TPL, "w", encoding="utf-8") as f:
        f.write(text)

    print(f"OK: {OUT_TPL}")
    print(f"  Parameters: {len(out['Parameters'])}")
    print(f"  Conditions: {len(out['Conditions'])}")
    print(f"  Resources:  {len(out['Resources'])}")
    print(f"  Outputs:    {len(out['Outputs'])}")
    print(f"  Size (chars): {len(text)}")


if __name__ == "__main__":
    main()
