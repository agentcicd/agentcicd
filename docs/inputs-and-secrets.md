# Inputs And Secrets

Recipes declare the values they need. Local projects provide those values with YAML files or legacy scalar `.properties` files.

## Inputs

Declare inputs in SQL:

```sql
DECLARE INPUT target_url STRING;
DECLARE INPUT provider_key SECRET;
DECLARE INPUT threshold FLOAT DEFAULT 0.8;
```

Supply values in `inputs.yaml`:

```yaml
target_url: https://target.example.test
provider_key: secret.OPENAI_API_KEY
threshold: 0.85
```

If an input has no SQL default, the local project must provide it. Unknown input keys fail validation.

## Secrets

Reference secrets by id from `inputs.yaml`:

```yaml
provider_key: secret.OPENAI_API_KEY
```

Define local secret records in `secrets.yaml`:

```yaml
OPENAI_API_KEY:
  type: api_key
  value: replace-with-local-value
```

Secret keys in `secrets.yaml` are plain keys, not `secret.<KEY>` ids. The loader turns `OPENAI_API_KEY` into `secret.OPENAI_API_KEY`.

Supported local secret record types are `raw`, `api_key`, and `bearer`.

## Legacy Properties

Scalar projects may use:

```text
input.properties
secret.properties
```

Each non-comment line must be `KEY=VALUE`. Do not use both YAML and `.properties` files for the same purpose in one project.

## Redaction

AgentCICD redacts known local secret values from local inspection artifacts such as progress, logs, reports, and debug files. Keep `secrets.yaml` and `secret.properties` out of version control anyway; they are still credential files.
