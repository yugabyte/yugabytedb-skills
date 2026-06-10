# Stage 1 — install YBA on a VM & register the first customer

These two resources run against the **unauthenticated** provider (no token exists yet). They produce the `api_token` that configures the authenticated provider used in Stage 2. The VM must already exist and meet the host requirements in `prereqs-and-network.md` (4 vCPU / 8 GB / **250 GB root volume** / x86_64 / supported Linux / Python 3.10–3.12 / sudo SSH user).

## `yba_installer` — install YBA over SSH

Installs (and upgrades) YBA via the YBA Installer (`yba-ctl`). The host needs `curl` and a sudo-capable SSH user.

```hcl
provider "yba" {
  alias = "unauthenticated"
  host  = var.yba_host
}

resource "yba_installer" "install" {
  provider                  = yba.unauthenticated
  ssh_host_ip               = var.yba_host
  ssh_user                  = "ybadmin"                       # sudo access; NOT named "yugabyte"
  ssh_private_key_file_path = "~/.ssh/yba_host.pem"
  yba_license_file          = "${path.module}/yba.lic"        # from Yugabyte sales
  yba_version               = "2024.2.0.0-b100"               # full version WITH build number

  # Optional:
  # application_settings_file = "${path.module}/application_settings.conf"  # custom yba-ctl.yml
  # host_architecture         = "x86_64"   # default
  # host_os                   = "linux"     # default
  # reconfigure               = true        # set when application_settings_file content changes
  # skip_preflight_checks     = ["ports"]
  # tls_certificate_file      = "${path.module}/server.crt"   # custom HTTPS cert
  # tls_key_file              = "${path.module}/server.key"
}
```

**Required:** `ssh_host_ip`, `ssh_user`, `ssh_private_key_file_path`, `yba_license_file`, `yba_version`.

**Upgrade:** bump `yba_version` and re-`apply`. If an upgrade fails mid-way, `terraform taint` the resource and re-apply.

## `yba_customer_resource` — register the first customer

A fresh YBA has no users. This registers the first customer/admin and **outputs the API token + customer UUID**. `terraform destroy` is a **no-op** (YBA has no delete-customer API) — it only drops the resource from state.

```hcl
variable "customer_password" {
  type      = string
  sensitive = true
}

resource "yba_customer_resource" "customer" {
  provider   = yba.unauthenticated
  depends_on = [yba_installer.install]

  code     = "prod"               # dev | demo | stage | prod (default "dev")
  name     = "Acme Platform"
  email    = "admin@acme.example"
  password = var.customer_password   # must satisfy YBA password rules
}

# Hand these to Stage 2 (e.g. via an output consumed by the next root module,
# a tfvars file, or terraform_remote_state).
output "yba_api_token" {
  value     = yba_customer_resource.customer.api_token
  sensitive = true
}
output "yba_customer_uuid" {
  value = yba_customer_resource.customer.cuuid
}
```

**Required:** `name`, `email`, `password`. **Optional:** `code`. **Read-only:** `api_token` (sensitive), `cuuid`, `id`.

Import an existing customer: `terraform import yba_customer_resource.customer <customer-uuid>`.

## Passing the token to Stage 2

The authenticated provider's `api_token` must be **known before** that provider initializes, so do **not** configure it from a resource attribute in the same apply. Instead:

- **Separate root modules** (recommended): Stage 1 outputs `yba_api_token`; Stage 2 reads it via `terraform_remote_state` or a written `*.auto.tfvars`.
- Or a single config applied in two passes with `-target` (install + customer first, then the rest).

```hcl
# Stage 2 root module
provider "yba" {
  host      = var.yba_host
  api_token = var.yba_api_token   # sourced from Stage 1 output
}
```

State from Stage 1 contains the token and password — use an **encrypted remote backend**.
